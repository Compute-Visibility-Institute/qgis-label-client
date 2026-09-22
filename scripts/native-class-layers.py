#!/usr/bin/env python3
"""Native QGIS OAPIF acceptance using loopback fixtures and real API documents.

Run with QGIS's Python, for example:
  /Applications/QGIS.app/Contents/MacOS/python scripts/native-class-layers.py

The sibling labeling-platform checkout supplies the API contract through its
existing api/.venv. No cloud credentials, deployed services or real labels are
used. This proves provider compatibility, not database authorization or storage;
the API/DB suites own those checks.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

REPO = Path(__file__).resolve().parents[1]
API_SOURCE = REPO.parent / "labeling-platform" / "api"
CHINESE = "中国联通中卫云数据中心"
GEOMETRIES = {
    "point": [
        {"type": "Point", "coordinates": [105.31, 37.63]},
        {"type": "MultiPoint", "coordinates": [[105.32, 37.64], [105.33, 37.65]]},
    ],
    "line": [
        {"type": "LineString", "coordinates": [[105.31, 37.63], [105.32, 37.64]]},
        {
            "type": "MultiLineString",
            "coordinates": [
                [[105.31, 37.63], [105.32, 37.64]],
                [[105.33, 37.65], [105.34, 37.66]],
            ],
        },
    ],
    "polygon": [
        {
            "type": "Polygon",
            "coordinates": [
                [[105.31, 37.63], [105.32, 37.63], [105.32, 37.64], [105.31, 37.63]],
            ],
        },
        {
            "type": "MultiPolygon",
            "coordinates": [
                [
                    [[105.31, 37.63], [105.32, 37.63], [105.32, 37.64], [105.31, 37.63]],
                ],
                [
                    [[105.33, 37.65], [105.34, 37.65], [105.34, 37.66], [105.33, 37.65]],
                ],
            ],
        },
    ],
}


def export_contract(api_source: Path) -> None:
    """Use actual production builders; keep fixture data outside application code."""
    sys.path.insert(0, str(api_source))
    from edge import class_layers

    definitions = [
        {"name": key, "title": key, "type": kind, "read_only": key in class_layers.READ_ONLY}
        for key, kind in class_layers.CORE.items()
    ]
    for origin, key, kind in [
        ("src", "Name_Ch", "string"),
        ("src", "Company", "string"),
        ("src", "Year", "integer"),
        ("attr", "Area_sqm", "number"),
        ("attr", "confirmed", "boolean"),
    ]:
        definitions.append(
            {
                "name": class_layers.field_name(origin, key),
                "title": key,
                "source_name": key,
                "origin": origin,
                "type": kind,
                "read_only": False,
            }
        )
    documents = {}
    for family, geometries in GEOMETRIES.items():
        row = {
            "class_id": "native_" + family,
            "label_en": "Native " + family,
            "geom_type": "Any",
            "attr_schema": {},
        }
        spec = class_layers.descriptor(row, family)
        features = []
        for index, geometry in enumerate(geometries):
            feature_row = {
                "id": index + 1,
                "class_id": row["class_id"],
                "label_id": f"00000000-0000-4000-8000-{index + 1:012d}",
                "track_id": "00000000-0000-4000-8000-000000000099",
                "updated_at": "2026-09-22T00:00:00Z",
                "geometry": geometry,
                "names": {"en": "Native feature", "zh": CHINESE},
                "attrs": {
                    "Area_sqm": 388749.13,
                    "confirmed": False,
                    "source_attributes": {
                        "Name_Ch": CHINESE,
                        "Company": "China Unicom",
                        "Year": None,
                    },
                },
            }
            features.append(class_layers.feature(feature_row, definitions))
        documents[spec["id"]] = {
            "spec": spec,
            "fields": definitions,
            "schema": class_layers.schema_document(spec, definitions),
            "features": features,
        }
    print(
        json.dumps(
            {"collections": documents, "conformance": class_layers.conformance_document()},
            ensure_ascii=False,
        )
    )


class Fixture:
    def __init__(self, contract: dict):
        self.contract = contract
        self.records = {
            key: {feature["id"]: copy.deepcopy(feature) for feature in value["features"]}
            for key, value in contract["collections"].items()
        }
        self.requests = []
        self.root = ""
        self.created = 100
        self.history = copy.deepcopy(self.records)
        self.version = 1
        self.malformed_filters = []

    def description(self, collection: str) -> dict:
        spec = self.contract["collections"][collection]["spec"]
        url = self.root + "/collections/" + collection
        return {
            "id": collection,
            "title": spec["title"],
            "itemType": "feature",
            "links": [
                {"rel": "self", "href": url, "type": "application/json"},
                {"rel": "items", "href": url + "/items", "type": "application/geo+json"},
                {
                    "rel": "http://www.opengis.net/def/rel/ogc/1.0/schema",
                    "href": url + "/schema",
                    "type": "application/schema+json",
                },
            ],
        }

    def handle(self, method: str, path: str, body: dict | None) -> tuple[int, dict, dict]:
        parts = unquote(urlsplit(path).path).strip("/").split("/")
        self.requests.append({"method": method, "path": path, "body": body})
        if parts == ["v1", "class-layers"]:
            collections = []
            for key, value in self.contract["collections"].items():
                if not key.startswith("cl_"):
                    continue
                definitions = copy.deepcopy(value["fields"])
                known = {field["name"] for field in definitions}
                rows = list(self.records[key].values())
                for row in rows:
                    for name, item in row["properties"].items():
                        if name in known or not name.startswith("src_"):
                            continue
                        source = bytes.fromhex(name[4:]).decode("utf-8")
                        definitions.append(
                            {
                                "name": name,
                                "title": source,
                                "source_name": source,
                                "origin": "src",
                                "read_only": False,
                                "type": (
                                    "boolean"
                                    if isinstance(item, bool)
                                    else "integer"
                                    if isinstance(item, int)
                                    else "number"
                                    if isinstance(item, float)
                                    else "string"
                                ),
                            }
                        )
                        known.add(name)
                for field in definitions:
                    field["has_values"] = any(
                        row["properties"].get(field["name"]) is not None for row in rows
                    )
                collections.append(
                    {
                        **value["spec"],
                        "fields": definitions,
                        "native_add_field": True,
                    }
                )
            return 200, {"enabled": True, "version": 1, "collections": collections}, {}
        if parts == ["class-layers"]:
            return (
                200,
                {
                    "links": [
                        {
                            "rel": "data",
                            "href": self.root + "/collections",
                            "type": "application/json",
                        },
                        {
                            "rel": "conformance",
                            "href": self.root + "/conformance",
                            "type": "application/json",
                        },
                        {
                            "rel": "service-desc",
                            "href": self.root + "/api",
                            "type": "application/vnd.oai.openapi+json;version=3.0",
                        },
                    ]
                },
                {},
            )
        if parts == ["class-layers", "conformance"]:
            return 200, self.contract["conformance"], {}
        if parts == ["class-layers", "api"]:
            return (
                200,
                {
                    "openapi": "3.0.3",
                    "info": {"title": "Native fixture", "version": "1"},
                    # Part 1 simple-queryable discovery uses concrete paths,
                    # not the generic {collectionId} OpenAPI path parameter.
                    "paths": {
                        f"/collections/{collection}/items": {
                            "get": {
                                "parameters": [
                                    {
                                        "name": name,
                                        "in": "query",
                                        "style": "form",
                                        "explode": False,
                                        "schema": {"type": "string"},
                                    }
                                    for name in ("track_id", "class_id")
                                ],
                                "responses": {"200": {"description": "Features"}},
                            }
                        }
                        for collection in self.records
                    },
                },
                {},
            )
        if parts == ["class-layers", "collections"]:
            return 200, {"collections": [self.description(key) for key in self.records]}, {}
        if len(parts) < 3 or parts[2] not in self.records:
            return 404, {"detail": "Unknown fixture path"}, {}
        collection = parts[2]
        records = self.records[collection]
        if len(parts) == 3:
            return 200, self.description(collection), {}
        if parts[3] in {"schema", "queryables"}:
            return (
                200,
                self.contract["collections"][collection]["schema"],
                {"Content-Type": "application/schema+json"},
            )
        if parts[3] != "items":
            return 404, {"detail": "Unknown fixture path"}, {}
        if method == "OPTIONS":
            return (
                204,
                {},
                {"Allow": "GET, OPTIONS, POST" if len(parts) == 4 else "GET, OPTIONS, PUT, DELETE"},
            )
        if len(parts) == 4:
            if method == "GET":
                query = parse_qs(urlsplit(path).query)
                if "filter" in query and "(d=" in query["filter"][0]:
                    self.malformed_filters.append(query["filter"][0])
                    return 400, {"detail": "Malformed QGIS Part 1 filter combination"}, {}
                features = list(records.values())
                for name in ("track_id", "class_id"):
                    if name in query:
                        features = [
                            feature
                            for feature in features
                            if feature["properties"].get(name) == query[name][0]
                        ]
                return (
                    200,
                    {
                        "type": "FeatureCollection",
                        "features": features,
                        "numberMatched": len(features),
                        "numberReturned": len(features),
                        "links": [],
                    },
                    {},
                )
            if method == "POST":
                self.created += 1
                feature = copy.deepcopy(body)
                feature["id"] = f"{self.created}.{'a' * 24}"
                feature["properties"]["revision"] = "a" * 24
                feature["properties"].update(
                    label_id=f"00000000-0000-4000-8000-{self.created:012d}",
                    class_id=self.contract["collections"][collection]["spec"]["class_id"],
                    track_id="00000000-0000-4000-8000-000000000099",
                    updated_at="2026-09-22T00:00:00Z",
                )
                records[feature["id"]] = feature
                return (
                    201,
                    feature,
                    {
                        "Location": self.root
                        + "/collections/"
                        + collection
                        + "/items/"
                        + feature["id"]
                    },
                )
        identifier = parts[4]
        baseline = self.history[collection].get(identifier) or records.get(identifier)
        serial = identifier.split(".")[0]
        current_key = next((key for key in records if key.split(".")[0] == serial), None)
        if baseline is None or current_key is None:
            return 409, {"detail": "Feature revision changed; reload before editing"}, {}
        if method == "GET":
            return 200, baseline, {}
        if method == "DELETE":
            if current_key != identifier:
                return 409, {"detail": "Feature revision changed; reload before deleting"}, {}
            del records[identifier]
            return 204, {}, {}
        if method == "PUT":
            current = records[current_key]
            feature = copy.deepcopy(current)
            # Storage is deliberately a fixture. API/DB tests separately prove
            # the real archived-baseline merge and authorization semantics.
            for key, value in body["properties"].items():
                if key in {"id", "label_id", "track_id", "class_id", "revision", "updated_at"}:
                    continue
                previous = baseline["properties"].get(key)
                actual = current["properties"].get(key)
                if key not in baseline["properties"] or value != previous:
                    if actual != previous and actual != value:
                        return 409, {"detail": "Concurrent field change"}, {}
                    feature["properties"][key] = value
            if body.get("geometry") != baseline.get("geometry"):
                if current.get("geometry") not in (baseline.get("geometry"), body.get("geometry")):
                    return 409, {"detail": "Concurrent geometry change"}, {}
                feature["geometry"] = body["geometry"]
            self.version += 1
            revision = f"{self.version:024x}"
            feature["id"] = f"{serial}.{revision}"
            feature["properties"]["revision"] = revision
            self.history[collection][current_key] = copy.deepcopy(current)
            del records[current_key]
            records[feature["id"]] = feature
            return (
                200,
                feature,
                {"Location": self.root + "/collections/" + collection + "/items/" + feature["id"]},
            )
        return 405, {"detail": "Unsupported fixture operation"}, {}


def exercise_native_fields(fixture, layers, checks, profile):
    """Exercise the actual native Add Field/edit-buffer lifecycle, not mocks."""
    from qgis.core import (
        Qgis,
        QgsDataSourceUri,
        QgsFeature,
        QgsField,
        QgsGeometry,
        QgsProject,
        QgsVectorLayer,
    )
    from qgis.gui import QgsCollapsibleGroupBox
    from qgis.PyQt.QtCore import QVariant

    from qgis_label_client.classprovider import PROVIDER_KEY, register_provider
    from qgis_label_client.core.uri import build_oapif_uri
    from qgis_label_client.dockwidget import LabelClientDock
    from qgis_label_client.layers import configure_class_columns
    from qgis_label_client.plugin import LabelClientPlugin

    register_provider()
    dock = LabelClientDock()
    groups = dock.findChildren(QgsCollapsibleGroupBox)
    assert [group.title() for group in groups][-2:] == ["Bootstrap", "Environment"]
    assert dock.remove_unused_fields_checkbox.isChecked()
    checks.append(
        "Native Qt panel places Bootstrap above Environment and defaults unused-field pruning on"
    )
    dock.close()
    original = copy.deepcopy(fixture.records)
    original_history = copy.deepcopy(fixture.history)
    try:
        for collection, doc in fixture.contract["collections"].items():
            if not collection.startswith("cl_"):
                continue
            print("Native Add Field: " + collection, file=sys.stderr, flush=True)
            # One logical label can have multiple valid-time rows. Both must load.
            duplicate = copy.deepcopy(doc["features"][0])
            duplicate["id"] = "700." + "b" * 24
            duplicate["properties"]["valid_from"] = "2026-01-01T00:00:00Z"
            fixture.records[collection][duplicate["id"]] = duplicate
            base = build_oapif_uri(
                landing_url=fixture.root + "?track=dev",
                collection_id=collection,
                restrict_to_request_bbox=False,
            )
            uri = QgsDataSourceUri(base)
            uri.setParam("removeUnusedFields", "1")
            layer = QgsVectorLayer(uri.uri(False), "Native " + collection, PROVIDER_KEY)
            layers.append(layer)
            assert layer.isValid(), (
                collection,
                layer.error().summary(),
                layer.dataProvider().errors(),
            )
            data = layer.dataProvider()
            configure_class_columns(layer, {**doc["spec"], "fields": data.field_definitions()})
            features = list(layer.getFeatures())
            assert len(features) == 3, (collection, len(features), data.errors())
            label_ids = [feature["label_id"] for feature in features]
            assert len(set(label_ids)) == 2, (
                label_ids,
                [feature.attributes() for feature in features],
            )
            assert len({feature.id() for feature in features}) == 3
            year = "src_" + b"Year".hex()
            assert layer.fields().indexOf(year) < 0
            assert layer.fields().indexOf("attr_" + b"confirmed".hex()) >= 0
            checks.append(
                collection
                + ": native provider loads distinct physical rows and prunes only null fields"
            )

            assert data.capabilities() & Qgis.VectorProviderCapability.AddAttributes
            before = len(fixture.requests)
            assert layer.startEditing()
            assert layer.addAttribute(QgsField("Undo this", QVariant.String, "text"))
            assert layer.fields().indexOf("Undo this") >= 0
            layer.undoStack().undo()
            assert layer.fields().indexOf("Undo this") < 0
            assert layer.rollBack()
            assert len(fixture.requests) == before, fixture.requests[before:]
            checks.append(collection + ": native Add Field undo/cancel sends no server request")

            column = "安装数量"
            encoded = "src_" + column.encode("utf-8").hex()
            assert layer.startEditing()
            assert layer.addAttribute(QgsField(column, QVariant.Int, "integer"))
            index = layer.fields().indexOf(column)
            assert layer.changeAttributeValue(features[0].id(), index, 7)
            moved = QgsGeometry(features[0].geometry())
            moved.translate(0.0001, 0.0001)
            assert layer.changeGeometry(features[0].id(), moved)
            before = len(fixture.requests)
            assert layer.commitChanges(), (collection, layer.commitErrors())
            writes = [
                request
                for request in fixture.requests[before:]
                if request["method"] in {"PUT", "POST"}
            ]
            assert len(writes) == 1, writes
            assert writes[0]["body"]["properties"][encoded] == 7
            assert "/fields" not in writes[0]["path"]
            saved = next(
                feature for feature in layer.getFeatures() if feature.id() == features[0].id()
            )
            assert saved[column] == 7
            data.reloadData()
            assert not data.last_refresh_error, data.last_refresh_error
            layer.updateFields()
            assert layer.fields().indexOf(column) == index
            assert layer.fields()[index].type() == QVariant.Int
            assert any(feature[column] == 7 for feature in layer.getFeatures())
            checks.append(
                collection
                + ": native field plus geometry save uses one JSON PUT and survives reload"
            )

            assert layer.startEditing()
            assert layer.addAttribute(QgsField("Empty integer", QVariant.Int, "integer"))
            before = len(fixture.requests)
            assert layer.commitChanges(), layer.commitErrors()
            assert len(fixture.requests) == before
            reconnect = QgsVectorLayer(data.dataSourceUri(), "Reconnect", PROVIDER_KEY)
            layers.append(reconnect)
            assert reconnect.isValid(), reconnect.dataProvider().errors()
            assert reconnect.fields().indexOf("Empty integer") >= 0, (
                data.dataSourceUri(),
                data.local_fields(),
                reconnect.fields().names(),
                reconnect.dataProvider().local_fields(),
            )
            assert reconnect.fields().field("Empty integer").type() == QVariant.Int
            assert reconnect.fields().field(column).type() == QVariant.Int
            checks.append(
                collection
                + ": local all-null field type survives reconnect without a schema API call"
            )

            uri.removeParam("removeUnusedFields")
            uri.setParam("removeUnusedFields", "0")
            uri.setParam("cviReadOnly", "1")
            readonly = QgsVectorLayer(uri.uri(False), "Read only full columns", PROVIDER_KEY)
            layers.append(readonly)
            assert readonly.isValid(), readonly.dataProvider().errors()
            assert readonly.fields().indexOf(year) >= 0
            assert not readonly.startEditing()
            assert not readonly.dataProvider().addAttributes(
                [QgsField("Forbidden", QVariant.String)]
            )
            checks.append(
                collection
                + ": disabling pruning restores null columns; read-only provider rejects native editing"
            )

            assert layer.startEditing()
            added = QgsFeature(layer.fields())
            added.setGeometry(QgsGeometry(features[0].geometry()))
            added["valid_from"] = "2026-02-03T00:00:00Z"
            added[column] = 11
            assert layer.addFeature(added)
            assert layer.commitChanges(), layer.commitErrors()
            post = next(item for item in reversed(fixture.requests) if item["method"] == "POST")
            assert post["body"]["properties"][encoded] == 11
            assert post["body"]["properties"]["valid_from"] == "2026-02-03T00:00:00Z"
            created = next(feature for feature in layer.getFeatures() if feature[column] == 11)
            assert layer.startEditing()
            assert layer.deleteFeature(created.id())
            assert layer.commitChanges(), layer.commitErrors()
            checks.append(
                collection
                + ": native create preserves date/typed field and native delete acknowledges revision"
            )

            project = QgsProject()
            project_layer = QgsVectorLayer(
                data.dataSourceUri(), "Saved native fields", PROVIDER_KEY
            )
            project.addMapLayer(project_layer)
            project.writeMapLayer.connect(
                lambda saved_layer, element, document: LabelClientPlugin._write_class_layer_source(
                    None, saved_layer, element, document
                )
            )
            project_path = str(Path(profile) / (collection + ".qgs"))
            assert project.write(project_path)
            reopened = QgsProject()
            assert reopened.read(project_path)
            restored = next(iter(reopened.mapLayers().values()))
            assert restored.isValid(), restored.dataProvider().errors()
            assert restored.fields().field("Empty integer").type() == QVariant.Int
            assert restored.fields().field(column).type() == QVariant.Int
            checks.append(collection + ": saved QGIS project restores committed local field schema")
            reopened.clear()
            project.clear()
    finally:
        fixture.records = original
        fixture.history = original_history


def run_native(contract: dict) -> dict:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qgis.core import (
        NULL,
        Qgis,
        QgsApplication,
        QgsFeature,
        QgsFeatureRequest,
        QgsGeometry,
        QgsVectorLayer,
        QgsWkbTypes,
    )
    from qgis.PyQt.QtCore import QVariant

    sys.path.insert(0, str(REPO))
    from qgis_label_client.core.tracks import Track, canary_filter
    from qgis_label_client.core.uri import build_oapif_uri
    from qgis_label_client.layers import configure_class_columns, refresh_class_layer_after_commit

    fixture = Fixture(contract)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def serve(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else None
            status, payload, headers = fixture.handle(self.command, self.path, body)
            encoded = b"" if status == 204 else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", headers.pop("Content-Type", "application/geo+json"))
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = serve
        do_OPTIONS = serve
        do_POST = serve
        do_PUT = serve
        do_DELETE = serve

    checks = []
    with tempfile.TemporaryDirectory(prefix="cvi-native-class-layers-") as profile:
        os.environ["QGIS_CUSTOM_CONFIG_PATH"] = profile
        app = QgsApplication([], False, profile)
        app.initQgis()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        fixture.root = f"http://127.0.0.1:{server.server_port}/class-layers"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        layers = []
        try:
            exercise_native_fields(fixture, layers, checks, profile)
            for collection, doc in contract["collections"].items():
                uri = build_oapif_uri(
                    landing_url=fixture.root + "?track=dev",
                    collection_id=collection,
                    restrict_to_request_bbox=False,
                )
                layer = QgsVectorLayer(uri, collection, "OAPIF")
                layers.append(layer)
                assert layer.isValid(), (collection, layer.error().summary(), fixture.requests)
                configure_class_columns(layer, {**doc["spec"], "fields": doc["fields"]})
                features = list(layer.getFeatures())
                assert len(features) == 2, (collection, len(features))
                assert QgsWkbTypes.isMultiType(layer.wkbType()), (collection, layer.wkbType())
                assert all(feature.geometry().isMultipart() for feature in features)
                names = {
                    field["source_name"]: field["name"]
                    for field in doc["fields"]
                    if "source_name" in field
                }
                for name, expected in (
                    ("Year", QVariant.LongLong),
                    ("Area_sqm", QVariant.Double),
                    ("confirmed", QVariant.Bool),
                ):
                    assert layer.fields().field(names[name]).type() == expected, name
                for feature in features:
                    assert feature[names["Name_Ch"]] == CHINESE
                    assert feature[names["Year"]] == NULL
                    assert feature[names["Area_sqm"]] == 388749.13
                    assert feature[names["confirmed"]] is False
                assert not layer.editFormConfig().readOnly(layer.fields().indexOf(names["Company"]))
                for key in ("class_id", "revision", "label_id", "track_id"):
                    assert layer.editFormConfig().readOnly(layer.fields().indexOf(key)), key
                checks.append(
                    collection
                    + ": multipart geometry, typed scalar values, Unicode/nulls and protected identity"
                )

                readonly = QgsVectorLayer(uri, collection + " read only", "OAPIF")
                layers.append(readonly)
                assert readonly.isValid(), readonly.error().summary()
                configure_class_columns(readonly, {**doc["spec"], "fields": doc["fields"]})
                # Match _load_collections' separate read-only class layer path.
                readonly.setCustomProperty("cvi/read_only_view", True)
                readonly.setReadOnly(True)
                assert readonly.readOnly() and not readonly.startEditing()
                readonly_features = list(readonly.getFeatures())
                assert len(readonly_features) == 2
                assert all(feature[names["Name_Ch"]] == CHINESE for feature in readonly_features)
                assert (
                    readonly.attributeAlias(readonly.fields().indexOf(names["Company"]))
                    == "Company"
                )
                assert readonly.fields().indexOf(names["Area_sqm"]) >= 0
                checks.append(
                    collection
                    + ": read-only class layer keeps readable scalar columns and refuses editing"
                )

                # A single Save invokes separate native attribute and geometry
                # PUT requests, both carrying the same cached opaque URL ID.
                provider = layer.dataProvider()
                first = features[0]
                old_id = doc["features"][0]["id"]
                company_index = layer.fields().indexOf(names["Company"])
                assert layer.startEditing()
                assert layer.changeAttributeValue(first.id(), company_index, "中国联通 updated")
                moved = QgsGeometry(first.geometry())
                moved.translate(0.001, 0.001)
                assert layer.changeGeometry(first.id(), moved)
                before = len(fixture.requests)
                assert layer.commitChanges(), layer.commitErrors()
                updates = [
                    request for request in fixture.requests[before:] if request["method"] == "PUT"
                ]
                assert len(updates) == 2, updates
                assert all(
                    urlsplit(update["path"]).path.endswith("/" + old_id) for update in updates
                )
                assert all(
                    update["body"]["properties"][names["Year"]] is None for update in updates
                )
                assert any(
                    update["body"]["properties"][names["Company"]] == "中国联通 updated"
                    for update in updates
                )
                checks.append(
                    collection
                    + ": combined native attribute/geometry commit uses baseline revision IDs"
                )

                # This is the same refresh helper the plugin schedules after a
                # fully acknowledged commit; reload is part of the implementation.
                assert refresh_class_layer_after_commit(layer)
                changed = next(
                    feature
                    for feature in layer.getFeatures()
                    if feature[names["Company"]] == "中国联通 updated"
                )
                assert layer.startEditing()
                assert layer.changeAttributeValue(changed.id(), company_index, "Second update")
                assert layer.commitChanges(), layer.commitErrors()
                assert refresh_class_layer_after_commit(layer)
                changed = next(
                    feature
                    for feature in layer.getFeatures()
                    if feature[names["Company"]] == "Second update"
                )
                assert layer.startEditing()
                assert layer.deleteFeature(changed.id())
                assert layer.commitChanges(), layer.commitErrors()
                checks.append(
                    collection + ": repeated edit/delete succeeds after plugin commit refresh"
                )

                addition = QgsFeature(layer.fields())
                addition.setGeometry(
                    QgsGeometry.fromWkt(
                        {
                            "point": "MultiPoint ((105.35 37.65))",
                            "line": "MultiLineString ((105.35 37.65, 105.36 37.66))",
                            "polygon": "MultiPolygon (((105.35 37.65, 105.36 37.65, 105.36 37.66, 105.35 37.65)))",
                        }[doc["spec"]["family"]]
                    )
                )
                addition.setAttribute("class_id", doc["spec"]["class_id"])
                addition.setAttribute(names["Company"], "新建公司")
                ok, created = provider.addFeatures([addition])
                assert ok and len(created) == 1, provider.errors()
                post = next(
                    request for request in reversed(fixture.requests) if request["method"] == "POST"
                )
                assert post["body"]["properties"][names["Company"]] == "新建公司"
                checks.append(collection + ": native POST sends Unicode scalar attributes")

                fixture.records[collection].clear()
                empty = QgsVectorLayer(uri, collection + " empty", "OAPIF")
                layers.append(empty)
                assert empty.isValid(), empty.error().summary()
                assert list(empty.getFeatures()) == []
                assert QgsWkbTypes.isMultiType(empty.wkbType())
                for name in (names["Year"], names["Area_sqm"], names["confirmed"]):
                    assert empty.fields().field(name).type() == layer.fields().field(name).type(), (
                        name
                    )
                assert (
                    empty.dataProvider().capabilities() & Qgis.VectorProviderCapability.AddFeatures
                )
                checks.append(
                    collection
                    + ": empty schema retains scalar/geometry types and create capability"
                )

            # Legacy OAPIF collections advertise Part 1 property parameters.
            # Reproduce the QGIS 3.44 subset/request combination failure using
            # the original canary, then exercise the production workaround.
            legacy_doc = copy.deepcopy(contract["collections"]["cl_native_polygon__polygon"])
            legacy_doc["spec"]["id"] = "label_polygon"
            fixture.contract["collections"]["label_polygon"] = legacy_doc
            expected_track = "00000000-0000-4000-8000-000000000099"
            wrong_track = "00000000-0000-4000-8000-000000000100"
            legacy_features = []
            for index, (class_id, track_id) in enumerate(
                [
                    ("compound", expected_track),
                    ("other_class", expected_track),
                    ("compound", wrong_track),
                    ("compound", None),
                ]
            ):
                item = copy.deepcopy(legacy_doc["features"][0])
                item["id"] = f"{900 + index}.{'a' * 24}"
                item["properties"].update(class_id=class_id, track_id=track_id)
                legacy_features.append(item)
            fixture.records["label_polygon"] = {item["id"]: item for item in legacy_features}
            old_uri = build_oapif_uri(
                landing_url=fixture.root + "?track=dev",
                collection_id="label_polygon",
                restrict_to_request_bbox=False,
                cql_filter=f"\"track_id\" = '{expected_track}'",
            )
            broken = QgsVectorLayer(old_uri, "Legacy broken canary", "OAPIF")
            layers.append(broken)
            assert broken.isValid(), broken.error().summary()
            request = QgsFeatureRequest().setFilterExpression("\"class_id\" = 'compound'")
            assert list(broken.getFeatures(request)) == []
            assert fixture.malformed_filters, (
                "Old canary did not reproduce the malformed Part 1 URL"
            )
            assert any(
                "(d=" + expected_track in value and "(d=compound)" in value
                for value in fixture.malformed_filters
            ), fixture.malformed_filters
            checks.append(
                "legacy Part 1 raw track canary reproduces malformed combined CQL and HTTP 400"
            )

            fixed_uri = build_oapif_uri(
                landing_url=fixture.root + "?track=dev",
                collection_id="label_polygon",
                restrict_to_request_bbox=False,
                cql_filter=canary_filter(Track(name="dev", track_id=expected_track)),
            )
            fixed = QgsVectorLayer(fixed_uri, "Legacy safe canary", "OAPIF")
            layers.append(fixed)
            assert fixed.isValid(), fixed.error().summary()
            malformed_before = len(fixture.malformed_filters)
            requests_before = len(fixture.requests)
            selected = list(fixed.getFeatures(request))
            assert len(selected) == 1, [
                (feature["class_id"], feature["track_id"]) for feature in selected
            ]
            assert selected[0]["class_id"] == "compound"
            assert selected[0]["track_id"] == expected_track
            assert len(fixture.malformed_filters) == malformed_before
            assert all(
                "filter" not in parse_qs(urlsplit(item["path"]).query)
                for item in fixture.requests[requests_before:]
            )
            checks.append(
                "coalesce track canary plus class filter returns correct class/track without malformed CQL"
            )
            all_allowed = list(fixed.getFeatures())
            assert len(all_allowed) == 2
            assert all(feature["track_id"] == expected_track for feature in all_allowed)
            checks.append(
                "local track canary rejects wrong-track and null-track features independently of server filtering"
            )

            return {
                "qgis": Qgis.QGIS_VERSION,
                "checks": checks,
                "requests": fixture.requests,
                "reproduced_malformed_filters": fixture.malformed_filters,
                "scope": "Real QGIS provider with actual API schema/feature builders and local fixture storage only",
            }
        finally:
            layers.clear()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            # QgsVectorLayer/provider locals are destroyed when this function
            # returns. Tearing down the provider registry before that segfaults.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-source", type=Path, default=API_SOURCE)
    parser.add_argument("--api-python", type=Path)
    parser.add_argument("--export-contract", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.export_contract:
        export_contract(args.api_source)
        return
    api_python = args.api_python or args.api_source / ".venv" / "bin" / "python"
    api_env = dict(os.environ)
    # The QGIS launcher pins its bundled interpreter. The API's existing venv
    # needs its own Python standard library and compiled extension paths.
    for key in ("PYTHONHOME", "PYTHONPATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
        api_env.pop(key, None)
    exported = subprocess.run(
        [
            str(api_python),
            str(Path(__file__).resolve()),
            "--export-contract",
            "--api-source",
            str(args.api_source),
        ],
        check=False,
        text=True,
        capture_output=True,
        env=api_env,
    )
    if exported.returncode:
        raise RuntimeError("API contract export failed: " + exported.stderr)
    report = run_native(json.loads(exported.stdout))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "qgis": report["qgis"],
                    "checks": report["checks"],
                    "output": str(args.output),
                    "scope": report["scope"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(rendered)


if __name__ == "__main__":
    main()
