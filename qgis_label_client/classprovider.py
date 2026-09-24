"""Editable class layers with QGIS's native Add Field support.

The cache is a memory provider, but the layer itself is a remote provider: native
edit buffers, Save, Undo and Cancel remain QGIS's responsibility. Only a committed
feature write sends data to the API. Added columns are ordinary source-attribute
JSON keys, never database columns or changes to the shared class definition.
"""

# Qt provider overrides must retain the names and argument names defined by QGIS.
# ruff: noqa: N802, N803

from __future__ import annotations

import copy
import json
import re
import unicodedata
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from qgis.core import (
    QgsDataProvider,
    QgsDataSourceUri,
    QgsFeatureRequest,
    QgsField,
    QgsJsonUtils,
    QgsProviderMetadata,
    QgsProviderRegistry,
    QgsVariantUtils,
    QgsVectorDataProvider,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QByteArray, QDate, QDateTime, Qt, QVariant

from . import network
from .core import recorded
from .core.errors import BackendError
from .log import log_warning

PROVIDER_KEY = "cvi_class"
CORE_FIELDS = {
    "label_id",
    "track_id",
    "class_id",
    "revision",
    "updated_at",
    "valid_from",
    "valid_to",
    "capture_id",
    "name_en",
    "name_zh",
    "recorded_at",
}
REQUIRED_FIELDS = CORE_FIELDS - {"capture_id", "name_en", "name_zh"}
TYPES = {
    "string": QVariant.String,
    "integer": QVariant.LongLong,
    "number": QVariant.Double,
    "boolean": QVariant.Bool,
    "date": QVariant.Date,
    "date-time": QVariant.DateTime,
}
TYPE_NAMES = {int(value): key for key, value in TYPES.items()}
TYPE_NAMES[int(QVariant.Int)] = "integer"
_METADATA = None


def _row_identity(document, *, snapshot=False):
    identity = str(document.get("id") or "")
    pattern = r"(?:[1-9][0-9]*|[0-9a-f]{32})\.[0-9a-f]+" if snapshot else r"[1-9][0-9]*\.[0-9a-f]+"
    if not re.fullmatch(pattern, identity):
        raise ValueError("Server feature has no valid versioned row identity")
    return identity.split(".", 1)[0]


def _omit_unused(definition, *, enabled, local_wires, populated):
    wire = definition["name"]
    return (
        enabled
        and wire not in REQUIRED_FIELDS
        and wire not in local_wires
        and wire not in populated
        and definition.get("has_values") is False
    )


def _local_field(definition):
    return QgsField(
        definition["name"],
        QVariant.Type(definition.get("variant_type", int(TYPES[definition["type"]]))),
        definition.get("type_name", ""),
        definition.get("length", 0),
        definition.get("precision", 0),
        definition.get("comment", ""),
    )


def _json_value(value):
    if value is None or QgsVariantUtils.isNull(value):
        return None
    if isinstance(value, (QDate, QDateTime)):
        return value.toString(Qt.DateFormat.ISODate)
    return value


def _request(method, url, *, authcfg, track, payload=None):
    """Keep QgsBlockingNetworkRequest off the GUI thread, including native Save."""

    def run():
        if method == "GET":
            return network.request_json(url, authcfg=authcfg, track=track)
        request, fetcher = network._prepare(
            url, "application/json", authcfg, track, "QGIS class layer edit"
        )
        if method == "DELETE":
            error = fetcher.deleteResource(request)
        else:
            request.setRawHeader(b"Content-Type", b"application/geo+json")
            body = QByteArray(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            error = getattr(fetcher, method.lower())(request, body)
        response = network._read(fetcher, error, url)
        return response.json() if response.body.strip() else None

    # QGIS network timers require a genuine QThread. Keep main-thread auth
    # callbacks running while awaiting the blocking network request.
    from qgis.PyQt.QtCore import QCoreApplication, QEventLoop, QThread

    class RequestWorker(QThread):
        result = None
        failure = None

        def run(self):
            try:
                self.result = run()
            except Exception as exc:  # noqa: BLE001 - relay failure to provider boundary
                self.failure = exc

    worker = RequestWorker()
    app = QCoreApplication.instance()
    if app is not None and QThread.currentThread() == app.thread():
        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        worker.start()
        loop.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
    else:
        worker.start()
    worker.wait()
    if worker.failure is not None:
        raise worker.failure
    return worker.result


class ClassLayerProvider(QgsVectorDataProvider):
    """Opt-in provider; older servers keep native OAPIF unchanged."""

    def __init__(self, uri, options=None, flags=None):
        options = options or QgsDataProvider.ProviderOptions()
        if flags is None:
            super().__init__(uri, options)
        else:
            super().__init__(uri, options, flags)
        self._uri = QgsDataSourceUri(uri)
        self._valid = False
        self.last_refresh_error = ""
        self._cache = QgsVectorLayer("Point?crs=EPSG:4326", "CVI cache", "memory")
        self._memory = self._cache.dataProvider()
        self._definitions = {}
        self._wire_by_name = {}
        self._rows = {}
        self._fid_by_rowid = {}
        self._created = {}
        self._created_payloads = {}
        self._deleted = set()
        self._local_fields = []
        self._subset = self._uri.param("filter")
        self._track = ""
        self._authcfg = self._uri.authConfigId()
        self._read_only = self._uri.param("cviReadOnly") == "1"
        self._uncertain_create = self._uri.param("uncertainCreate") == "1"
        self._initial = True
        try:
            parsed = urlsplit(self._uri.param("url"))
            landing_query = parse_qs(parsed.query)
            self._track = landing_query.get("track", [""])[0]
            self._snapshot_view = landing_query.get("view", [""])[0]
            if self._snapshot_view not in {"", "ground", "recorded"}:
                raise ValueError("Unknown class-layer snapshot view")
            if self._snapshot_view:
                axis = "datetime" if self._snapshot_view == "ground" else "recorded_at"
                other_axis = "recorded_at" if self._snapshot_view == "ground" else "datetime"
                if not self._read_only or not landing_query.get(axis, [""])[0]:
                    raise ValueError(
                        "Class snapshots require a read-only layer and an explicit date"
                    )
                if recorded.parse_instant(landing_query[axis][0]) is None:
                    raise ValueError("Class snapshots require a UTC date and time")
                if other_axis in landing_query:
                    raise ValueError("Class snapshots must select exactly one time axis")
            elif "recorded_at" in landing_query:
                raise ValueError("Historical snapshots require an explicit read-only view")
            self._item_query = {"track": self._track, "limit": 1000}
            for key in ("datetime", "recorded_at", "view"):
                if key in landing_query:
                    self._item_query[key] = landing_query[key][0]
            if parsed.scheme not in {"https", "http"} or not parsed.netloc:
                raise ValueError(
                    "The class layer has no valid HTTP(S) API URL; reconnect and add it again"
                )
            if not parsed.path.rstrip("/").endswith("/class-layers"):
                raise ValueError(
                    "The layer URL does not point to the class-layer API; reconnect and add it again"
                )
            if not self._track.strip():
                raise ValueError(
                    "No environment was included in the class-layer URL. "
                    "Select an available Environment in the panel and add the layer again."
                )
            # The server authorizes the selected track. Production uses `default`;
            # track names are discovered, not restricted to the preview's `dev`.
            self._root = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
            self._collection = self._uri.param("typename")
            if not self._collection.startswith("cl_"):
                raise ValueError("Not a class-layer collection")
            self._collection_url = self._root + "/collections/" + quote(self._collection, safe="")
            self._local_fields = json.loads(self._uri.param("localFields") or "[]")
            self._load()
        except Exception as exc:  # noqa: BLE001 - Never propagate through Qt's provider factory.
            self.last_refresh_error = str(exc)
            self.pushError(self.last_refresh_error)
            log_warning("Could not open CVI class layer: " + self.last_refresh_error)

    def name(self):
        return PROVIDER_KEY

    def description(self):
        return "CVI editable class attributes"

    def storageType(self):
        return "CVI remote JSON attributes"

    def isValid(self):
        return self._valid

    def fields(self):
        return self._memory.fields()

    def crs(self):
        return self._memory.crs()

    def wkbType(self):
        return self._memory.wkbType()

    def extent(self):
        return self._memory.extent()

    def featureCount(self):
        return self._memory.featureCount()

    def getFeatures(self, request=None):
        return self._memory.getFeatures(request if request is not None else QgsFeatureRequest())

    def featureSource(self):
        return self._memory.featureSource()

    def capabilities(self):
        if self._read_only:
            return QgsVectorDataProvider.SelectAtId
        return (
            QgsVectorDataProvider.AddFeatures
            | QgsVectorDataProvider.DeleteFeatures
            | QgsVectorDataProvider.ChangeAttributeValues
            | QgsVectorDataProvider.ChangeGeometries
            | QgsVectorDataProvider.AddAttributes
            | QgsVectorDataProvider.ChangeFeatures
            | QgsVectorDataProvider.SelectAtId
        )

    def doesStrictFeatureTypeCheck(self):
        return False

    def supportsSubsetString(self):
        return True

    def subsetString(self):
        return self._subset

    def setSubsetString(self, subset, updateFeatureCount=True):
        if not self._memory.setSubsetString(subset, updateFeatureCount):
            return False
        self._subset = subset
        self._uri.removeParam("filter")
        self._uri.setParam("filter", subset)
        self.setDataSourceUri(self._uri.uri(False))
        return True

    def _http(self, method, path, payload=None):
        return _request(method, path, payload=payload, authcfg=self._authcfg, track=self._track)

    def _manifest(self):
        url = (
            self._root.removesuffix("/class-layers")
            + "/v1/class-layers?"
            + urlencode({key: value for key, value in self._item_query.items() if key != "limit"})
        )
        document = self._http("GET", url)
        if self._snapshot_view:
            axis = "datetime" if self._snapshot_view == "ground" else "recorded_at"
            if (
                document.get("temporal_views") is not True
                or document.get("snapshot_view") != self._snapshot_view
                or recorded.echo_mismatch(self._item_query[axis], document.get("snapshot_instant"))
            ):
                raise ValueError("Server did not confirm the requested class-layer snapshot")
        spec = next(
            (
                item
                for item in document.get("collections", [])
                if item.get("id") == self._collection
            ),
            None,
        )
        if not spec or spec.get("native_add_field") is not True:
            raise ValueError("Server does not support native Add Field for this layer; reconnect")
        if self._snapshot_view and spec.get("read_only") is not True:
            raise ValueError("Server did not advertise a read-only class-layer snapshot")
        if self._snapshot_view == "recorded" and not any(
            field.get("name") == "recorded_at" for field in spec.get("fields", [])
        ):
            raise ValueError("Server snapshot schema has no recorded-time echo")
        return spec

    def _load(self):
        """Fetch everything before changing the cache; failed refresh retains data."""
        spec = self._manifest()
        documents = []
        url = self._collection_url + "/items?" + urlencode(self._item_query)
        seen = set()
        while url:
            if url in seen:
                raise ValueError("Server returned a repeated feature page")
            seen.add(url)
            page = self._http("GET", url)
            if page.get("type") != "FeatureCollection" or not isinstance(
                page.get("features"), list
            ):
                raise ValueError("Invalid class-layer feature response")
            documents.extend(page["features"])
            url = next(
                (link["href"] for link in page.get("links", []) if link.get("rel") == "next"),
                "",
            )
            if url:
                parsed = urlsplit(url)
                target = urlsplit(self._collection_url + "/items")
                if (parsed.scheme, parsed.netloc, parsed.path) != (
                    target.scheme,
                    target.netloc,
                    target.path,
                ):
                    raise ValueError("Refusing a feature pagination link outside this collection")
                query = parse_qs(parsed.query)
                if query.get("track", [""])[0] != self._track:
                    raise ValueError("Feature pagination changed the selected track")
                for key in ("datetime", "recorded_at", "view"):
                    if query.get(key, [""])[0] != self._item_query.get(key, ""):
                        raise ValueError("Feature pagination changed the selected snapshot or date")
        if self._snapshot_view == "recorded":
            for row in documents:
                problem = recorded.echo_mismatch(
                    self._item_query["recorded_at"], row.get("properties", {}).get("recorded_at")
                )
                if problem:
                    raise ValueError("Class-layer snapshot: " + problem)
        rowids = [_row_identity(row, snapshot=bool(self._snapshot_view)) for row in documents]
        if len(set(rowids)) != len(rowids):
            raise ValueError("Feature pages contain duplicate row identities; retry refresh")
        # Build and decode against a temporary schema first. Malformed geometry,
        # types or filters must not leave half of a refresh in the live cache.
        shape = spec.get("storage_geometry_type") or spec["geometry_type"]
        staged_layer = QgsVectorLayer(shape + "?crs=EPSG:4326", "CVI cache", "memory")
        staged = staged_layer.dataProvider()
        existing_fields = list(self.fields()) if not self._initial else []
        if existing_fields and not staged.addAttributes(existing_fields):
            raise ValueError("Could not stage existing class columns")
        definitions = copy.deepcopy(self._definitions)
        mapping = dict(self._wire_by_name)
        new_fields = []
        local_by_wire = {definition["wire"]: definition for definition in self._local_fields}
        populated = {
            key
            for row in documents
            for key, value in row["properties"].items()
            if value is not None
        }
        for definition in spec["fields"]:
            wire = definition["name"]
            definitions[wire] = definition
            if wire in mapping.values():
                continue
            if _omit_unused(
                definition,
                enabled=self._uri.param("removeUnusedFields") == "1",
                local_wires=local_by_wire,
                populated=populated,
            ):
                continue
            local = local_by_wire.get(wire)
            name = local["name"] if local else wire
            field = (
                _local_field(local)
                if local
                else QgsField(name, TYPES.get(definition["type"], QVariant.String))
            )
            field.setAlias(definition.get("title", wire))
            new_fields.append(field)
            mapping[name] = wire
        if self._initial:
            for definition in self._local_fields:
                local_name = definition["name"]
                wire = definition["wire"]
                if wire in mapping.values():
                    continue
                field = _local_field(definition)
                new_fields.append(field)
                mapping[local_name] = wire
        if new_fields and not staged.addAttributes(new_fields):
            raise ValueError("Could not construct class attribute columns")
        if not staged.setSubsetString(self._subset):
            raise ValueError("Invalid class-layer subset expression")
        decoded = [self._feature(row, staged.fields(), mapping) for row in documents]
        if self._initial:
            self._cache, self._memory = staged_layer, staged
            self.setNativeTypes(
                [native for native in self._memory.nativeTypes() if int(native.mType) in TYPE_NAMES]
            )
        elif new_fields and not self._memory.addAttributes(new_fields):
            raise ValueError("Could not append refreshed class columns")
        self._definitions, self._wire_by_name = definitions, mapping
        # Preserve FIDs across successful refreshes so pending edit identities remain stable.
        for row, feature in zip(documents, decoded, strict=True):
            self._cache_row(row, feature)
        gone = set(self._fid_by_rowid) - set(rowids)
        for rowid in gone:
            fid = self._fid_by_rowid.pop(rowid)
            self._memory.deleteFeatures([fid])
            self._rows.pop(fid, None)
        self._initial = False
        self._valid = True

    def reloadData(self):
        try:
            self._load()
            self.last_refresh_error = ""
            self.dataChanged.emit()
        except Exception as exc:  # noqa: BLE001 - Qt virtual callback reports provider errors.
            self.last_refresh_error = str(exc)
            self.pushError("Refresh failed; existing data retained. " + self.last_refresh_error)

    def _feature(self, document, fields=None, mapping=None):
        fields = self.fields() if fields is None else fields
        mapping = self._wire_by_name if mapping is None else mapping
        adapted = copy.deepcopy(document)
        props = adapted["properties"]
        # GDAL adds an opaque GeoJSON id as a synthetic attribute and can
        # reformat date-like text. Decode geometry only, then map columns by name.
        adapted.pop("id", None)
        adapted["properties"] = {}
        features = QgsJsonUtils.stringToFeatureList(json.dumps(adapted, ensure_ascii=False))
        if len(features) != 1 or features[0].geometry().isNull():
            raise ValueError("Could not read class feature geometry")
        feature = features[0]
        values = []
        for field in fields:
            value = props.get(mapping[field.name()])
            if value is not None and field.type() == QVariant.Date:
                value = QDate.fromString(value, Qt.DateFormat.ISODate)
                if not value.isValid():
                    raise ValueError("Invalid date in " + field.name())
            elif value is not None and field.type() == QVariant.DateTime:
                value = QDateTime.fromString(value, Qt.DateFormat.ISODate)
                if not value.isValid():
                    raise ValueError("Invalid date-time in " + field.name())
            values.append(value)
        feature.setFields(fields)
        feature.setAttributes(values)
        return feature

    def _cache_row(self, row, feature=None):
        rowid = _row_identity(row, snapshot=bool(self._snapshot_view))
        feature = self._feature(row) if feature is None else feature
        fid = self._fid_by_rowid.get(rowid)
        if fid is None:
            ok, inserted = self._memory.addFeatures([feature])
            if not ok or len(inserted) != 1:
                raise ValueError("Could not cache saved feature")
            fid = inserted[0].id()
            self._fid_by_rowid[rowid] = fid
        else:
            self._memory.changeAttributeValues({fid: dict(enumerate(feature.attributes()))})
            self._memory.changeGeometryValues({fid: feature.geometry()})
        self._rows[fid] = copy.deepcopy(row)
        feature.setId(fid)
        return feature

    def local_fields(self):
        return copy.deepcopy(self._local_fields)

    def reset_write_session(self):
        """Called after native rollback/success; temporary FIDs can be reused later."""
        self._created.clear()
        self._created_payloads.clear()
        self._deleted.clear()

    def _mark_uncertain_create(self):
        self._uncertain_create = True
        self._uri.removeParam("uncertainCreate")
        self._uri.setParam("uncertainCreate", "1")
        self.setDataSourceUri(self._uri.uri(False))

    def field_definitions(self):
        """Descriptors keyed by actual QGIS names, including unpushed native fields."""
        local = {item["wire"]: item for item in self._local_fields}
        result = []
        for field in self.fields():
            name = field.name()
            wire = self._wire_by_name[name]
            definition = copy.deepcopy(self._definitions.get(wire) or {})
            if wire in local:
                definition.update(
                    origin="src",
                    source_name=local[wire]["name"],
                    type=local[wire]["type"],
                    title=local[wire]["name"],
                    read_only=False,
                )
            definition.update(name=name, wire_name=wire)
            result.append(definition)
        return result

    def _require_writable(self):
        if self._read_only or not self._valid:
            raise ValueError("This class layer is not editable")

    def addAttributes(self, attributes):
        """Native edit-buffer commit; no HTTP request until feature values are saved."""
        try:
            self._require_writable()
            descriptors = []
            names = set(self.fields().names())
            wires = set(self._wire_by_name.values())
            for field in attributes:
                name = field.name()
                if (
                    not name.strip()
                    or name != name.strip()
                    or name in names
                    or len(name) > 256
                    or len(name.encode("utf-8")) > 1024
                    or any(unicodedata.category(char).startswith("C") for char in name)
                ):
                    raise ValueError(
                        "Attribute names must be unique, nonempty, at most 256 "
                        "characters, and have no control characters"
                    )
                if name in CORE_FIELDS or name.startswith(("src_", "attr_")):
                    raise ValueError("This name is reserved for platform fields")
                kind = TYPE_NAMES.get(int(field.type()))
                if kind is None:
                    raise ValueError(
                        "Use text, integer, decimal, boolean, date or date-time fields"
                    )
                wire = "src_" + name.encode("utf-8").hex()
                if wire in wires:
                    raise ValueError("An attribute with this original name already exists")
                descriptors.append(
                    {
                        "name": name,
                        "wire": wire,
                        "type": kind,
                        "variant_type": int(field.type()),
                        "type_name": field.typeName(),
                        "length": field.length(),
                        "precision": field.precision(),
                        "comment": field.comment(),
                    }
                )
                names.add(name)
                wires.add(wire)
            if not self._memory.addAttributes(attributes):
                raise ValueError("Could not add native attribute fields")
            for definition in descriptors:
                self._wire_by_name[definition["name"]] = definition["wire"]
            self._local_fields.extend(descriptors)
            self._uri.removeParam("localFields")
            self._uri.setParam("localFields", json.dumps(self._local_fields, ensure_ascii=False))
            self.setDataSourceUri(self._uri.uri(False))
            return True
        except Exception as exc:  # noqa: BLE001 - Qt virtual callback reports provider errors.
            self.pushError(str(exc))
            return False

    def _properties(self, feature):
        props = {}
        for index, field in enumerate(self.fields()):
            wire = self._wire_by_name[field.name()]
            if self._definitions.get(wire, {}).get("read_only") and wire not in {
                "valid_from",
                "valid_to",
            }:
                continue
            props[wire] = _json_value(feature[index])
        return props

    def addFeatures(self, features, flags=None):
        completed = []
        try:
            self._require_writable()
            if self._uncertain_create:
                raise ValueError(
                    "A previous create has an unknown save outcome. Inspect the server "
                    "and recover the pending edits before retrying; automatic creates are blocked."
                )
            for position, feature in enumerate(features):
                payload = {
                    "type": "Feature",
                    "properties": self._properties(feature),
                    "geometry": json.loads(feature.geometry().asJson()),
                }
                # A partial native batch retry must not recreate successful earlier rows.
                retry_key = feature.id() if feature.id() < 0 else (feature.id(), position)
                if retry_key in self._created:
                    previous = self._created[retry_key]
                    if payload != self._created_payloads[retry_key]:
                        retry_payload = copy.deepcopy(payload)
                        # Validity defaults apply to create; the row is now existing.
                        retry_payload["properties"].pop("valid_from", None)
                        retry_payload["properties"].pop("valid_to", None)
                        previous = self._http(
                            "PUT",
                            self._collection_url + "/items/" + quote(str(previous["id"]), safe=""),
                            retry_payload,
                        )
                        self._created[retry_key] = previous
                        self._created_payloads[retry_key] = copy.deepcopy(payload)
                        self._cache_row(previous)
                    completed.append(self._feature(previous))
                    completed[-1].setId(self._fid_by_rowid[_row_identity(previous)])
                    continue
                try:
                    saved = self._http("POST", self._collection_url + "/items", payload)
                    if not isinstance(saved, dict):
                        raise ValueError("Create returned no feature identity")
                    _row_identity(saved)
                except Exception as exc:
                    known_rejection = (
                        isinstance(exc, BackendError)
                        and exc.status is not None
                        and 400 <= exc.status < 500
                        and exc.status != 408
                    )
                    if not known_rejection:
                        self._mark_uncertain_create()
                    raise
                self._created[retry_key] = saved
                self._created_payloads[retry_key] = copy.deepcopy(payload)
                completed.append(self._cache_row(saved))
            self._created.clear()
            self._created_payloads.clear()
            return True, completed
        except Exception as exc:  # noqa: BLE001 - Qt virtual callback reports provider errors.
            self.pushError(str(exc))
            return False, completed

    def _change(self, attributes, geometries):
        try:
            self._require_writable()
            for fid in set(attributes) | set(geometries):
                before = self._rows[fid]
                props = {}
                for index, value in attributes.get(fid, {}).items():
                    wire = self._wire_by_name[self.fields()[index].name()]
                    converted = _json_value(value)
                    if self._definitions.get(wire, {}).get("read_only") and converted != before[
                        "properties"
                    ].get(wire):
                        raise ValueError("This attribute is read-only")
                    props[wire] = converted
                payload = {
                    "type": "Feature",
                    "properties": props,
                    "geometry": (
                        json.loads(geometries[fid].asJson())
                        if fid in geometries
                        else before["geometry"]
                    ),
                }
                saved = self._http(
                    "PUT",
                    self._collection_url + "/items/" + quote(str(before["id"]), safe=""),
                    payload,
                )
                self._cache_row(saved)
            return True
        except Exception as exc:  # noqa: BLE001 - Qt virtual callback reports provider errors.
            self.pushError(str(exc))
            return False

    def changeAttributeValues(self, changes):
        return self._change(changes, {})

    def changeGeometryValues(self, changes):
        return self._change({}, changes)

    def changeFeatures(self, attributes, geometries):
        return self._change(attributes, geometries)

    def deleteFeatures(self, fids):
        try:
            self._require_writable()
            for fid in fids:
                if fid in self._deleted:
                    continue
                row = self._rows[fid]
                self._http(
                    "DELETE",
                    self._collection_url + "/items/" + quote(str(row["id"]), safe=""),
                )
                self._memory.deleteFeatures([fid])
                self._fid_by_rowid.pop(_row_identity(row), None)
                self._rows.pop(fid, None)
                self._deleted.add(fid)
            self._deleted.clear()
            return True
        except Exception as exc:  # noqa: BLE001 - Qt virtual callback reports provider errors.
            self.pushError(str(exc))
            return False


class ClassProviderMetadata(QgsProviderMetadata):
    def __init__(self):
        super().__init__(PROVIDER_KEY, "CVI editable class attributes")

    def createProvider(self, uri, options, flags=None):
        return ClassLayerProvider(uri, options, flags)


def register_provider():
    """Refresh the factory without unregistering providers used by open layers.

    QGIS retains this Python metadata object across plugin unload/reinstall. Its
    factory can therefore still hold the previous module's globals even after
    QGIS imports the upgraded plugin. Rebind only the constructor for NEW layers;
    existing providers, caches and edit buffers must remain untouched.
    """
    global _METADATA
    registry = QgsProviderRegistry.instance()
    existing = registry.providerMetadata(PROVIDER_KEY)
    if existing is not None:
        namespace = getattr(existing.createProvider, "__globals__", None)
        if not isinstance(namespace, dict) or namespace.get("__name__") != __name__:
            raise RuntimeError(
                "The registered CVI provider factory cannot be refreshed safely. "
                "Save your project and restart QGIS."
            )
        namespace["ClassLayerProvider"] = ClassLayerProvider
        _METADATA = existing
        return
    _METADATA = ClassProviderMetadata()
    if not registry.registerProvider(_METADATA):
        raise RuntimeError("Could not register the CVI class-layer provider")
