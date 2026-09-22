"""Durable recovery of native edit buffers; never blindly replay a failed save.

QGIS owns editing and commits. This module journals local deltas and uses the
same provider to save never-submitted work after connection checks. Native saves
are not idempotent: any attempted save without acknowledgement requires review.
"""

from __future__ import annotations

import json
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsVariantUtils,
)
from qgis.PyQt.QtCore import QDate, QDateTime, Qt, QTime, QTimer, QVariant
from qgis.PyQt.QtWidgets import QAction, QDialog, QLabel, QMessageBox, QPushButton, QVBoxLayout

from . import client, layers
from .core.errors import LabelClientError
from .core.fields import DEFAULT_FIELDS
from .core.pending import JournalError, JournalStore, decode_value, encode_value
from .core.urls import normalise_base_url

JOURNAL_PROPERTY = "cvi/pending_journal"
STATE_PROPERTY = "cvi/pending_state"
NAME_PROPERTY = "cvi/pending_original_name"


def _encode_field(field):
    """Keep a new local column's definition even when every value is NULL."""
    return {
        "name": field.name(),
        "type": int(field.type()),
        "type_name": field.typeName(),
        "sub_type": int(field.subType()),
        "length": field.length(),
        "precision": field.precision(),
        "comment": field.comment(),
    }


def _decode_field(value):
    if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not value["name"]:
        raise ValueError("A saved field has no valid name")
    for key in ("type", "sub_type", "length", "precision"):
        if type(value.get(key)) is not int:
            raise ValueError(f"A saved field has an invalid {key}")
    for key in ("type_name", "comment"):
        if not isinstance(value.get(key), str):
            raise ValueError(f"A saved field has an invalid {key}")
    return QgsField(
        value["name"],
        QVariant.Type(value["type"]),
        value["type_name"],
        value["length"],
        value["precision"],
        value["comment"],
        QVariant.Type(value["sub_type"]),
    )


def _encode(value):
    if value is None or QgsVariantUtils.isNull(value):
        return encode_value(None)
    if isinstance(value, QDateTime):
        return {"qt": "datetime", "value": value.toString(Qt.DateFormat.ISODateWithMs)}
    if isinstance(value, QDate):
        return {"qt": "date", "value": value.toString(Qt.DateFormat.ISODate)}
    if isinstance(value, QTime):
        return {"qt": "time", "value": value.toString(Qt.DateFormat.ISODateWithMs)}
    return encode_value(value)


def _decode(value):
    if isinstance(value, dict) and "qt" in value:
        kind = value["qt"]
        if kind == "datetime":
            return QDateTime.fromString(value["value"], Qt.DateFormat.ISODateWithMs)
        if kind == "date":
            return QDate.fromString(value["value"], Qt.DateFormat.ISODate)
        if kind == "time":
            return QTime.fromString(value["value"], Qt.DateFormat.ISODateWithMs)
        raise ValueError("Unknown saved Qt attribute type")
    return decode_value(value)


def _version(value):
    if isinstance(value, QDateTime):
        value = value.toString(Qt.DateFormat.ISODateWithMs)
    if value is None or not str(value):
        return ""
    text = str(value)
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(timezone.utc)
        # Native QDateTime retains milliseconds, not PostgreSQL microseconds.
        return stamp.isoformat(timespec="milliseconds")
    except ValueError:
        return text


class PendingEdits:
    def __init__(self, plugin):
        self.plugin = plugin
        self.store = JournalStore(Path(QgsApplication.qgisSettingsDirPath()) / "cvi-unpushed")
        self.watches = {}
        self.documents = {}
        self.baselines = {}
        self.owners = {}
        self.revisions = {}
        self.bound_buffers = set()
        self.commits = {}
        self.claimed = set()
        self.scheduled = set()
        self.saving = set()
        self.closed = False
        self.syncing = False
        self.warning = None
        self.review_dialog = None

    def install(self, menu):
        self.menu = menu
        self.plugin.dock.unpushedWarningsChanged.connect(self._toggle_warnings)
        self.review_action = QAction("Unpushed edits…", self.plugin.iface.mainWindow())
        self.review_action.triggered.connect(self.review)
        self.plugin.iface.addPluginToMenu(menu, self.review_action)
        self.plugin.teardown.add(
            "menu: Unpushed edits…",
            lambda: self.plugin.iface.removePluginMenu(menu, self.review_action),
        )
        project = QgsProject.instance()
        project.layersAdded.connect(self.watch_layers)
        project.layersWillBeRemoved.connect(self._removing)
        project.readProject.connect(self._project_read)
        self.plugin.teardown.add(
            "pending layer additions", lambda: project.layersAdded.disconnect(self.watch_layers)
        )
        self.plugin.teardown.add(
            "pending layer removals", lambda: project.layersWillBeRemoved.disconnect(self._removing)
        )
        self.plugin.teardown.add(
            "pending project read", lambda: project.readProject.disconnect(self._project_read)
        )
        self.watch_layers(layers.plugin_layers())

    def _toggle_warnings(self, enabled):
        self.plugin.settings.set("show_unpushed_warnings", enabled)
        if not enabled and self.warning is not None:
            self.warning.close()

    def _project_read(self, *_args):
        self.watch_layers(layers.plugin_layers())

    def _owner(self):
        email = self.plugin.settings.oauth_email
        if not email:
            with suppress(ValueError, TypeError):
                email = json.loads(self.plugin.settings.get("signed_out_connection")).get(
                    "email", ""
                )
        return email.casefold()

    def _removing(self, identifiers):
        for identifier in identifiers:
            watched = self.watches.pop(identifier, None)
            if watched is None:
                continue
            layer, bindings = watched
            try:
                if layer.isEditable() and layer.isModified():
                    self.snapshot(layer)
            except (JournalError, ValueError, TypeError, RuntimeError) as exc:
                self._error(str(exc))
            for signal, callback in bindings:
                with suppress(RuntimeError, TypeError):
                    signal.disconnect(callback)
            self.documents.pop(identifier, None)
            self.bound_buffers.discard(identifier)

    def watch_layers(self, candidates):
        if self.closed:
            return
        for layer in candidates:
            if (
                not layers.is_plugin_layer(layer)
                or layers.is_historical(layer)
                or layers.is_date_view(layer)
                or layer.id() in self.watches
            ):
                continue
            if not layers.belongs_to_backend(layer, self.plugin.settings.api_base_url):
                continue
            self.owners[layer.id()] = self._owner()
            bindings = []
            events = (
                ("editingStarted", lambda target=layer: self._editing_started(target)),
                ("featureAdded", lambda fid, target=layer: self.changed(target, fid)),
                ("featureDeleted", lambda fid, target=layer: self.changed(target, fid)),
                ("attributeAdded", lambda *_args, target=layer: self.changed(target)),
                ("attributeDeleted", lambda *_args, target=layer: self.changed(target)),
                ("geometryChanged", lambda fid, *_args, target=layer: self.changed(target, fid)),
                (
                    "attributeValueChanged",
                    lambda fid, *_args, target=layer: self.changed(target, fid),
                ),
                ("beforeCommitChanges", lambda *_args, target=layer: self.before_commit(target)),
                ("afterCommitChanges", lambda target=layer: self.committed(target)),
                ("afterRollBack", lambda target=layer: self.rolled_back(target)),
            )
            for name, callback in events:
                signal = getattr(layer, name)
                signal.connect(callback)
                bindings.append((signal, callback))
            self.watches[layer.id()] = (layer, bindings)
            self._find_document(layer)
            if layer.isEditable() and layer.isModified():
                self.changed(layer)

    def _editing_started(self, layer):
        self.owners[layer.id()] = self._owner()
        self.baselines.setdefault(layer.id(), {})

    def _find_document(self, layer):
        key = str(layer.customProperty(JOURNAL_PROPERTY, ""))
        try:
            candidates = self.store.list()
            candidates.sort(key=lambda row: row["id"] != key)
            for document in candidates:
                same_layer = document["id"] == key or (
                    document.get("layer_id") == layer.id()
                    and document["project"] == QgsProject.instance().fileName()
                )
                if same_layer and self._matches_layer(document, layer):
                    if any(
                        row["id"] == document["id"]
                        for other, row in self.documents.items()
                        if other != layer.id()
                    ):
                        layer.setCustomProperty(STATE_PROPERTY, "conflict")
                        self._error(
                            "A copied layer references another layer's recovery journal. "
                            "Reconnect the original layer to avoid duplicate uploads."
                        )
                        return None
                    self.documents[layer.id()] = document
                    layer.setCustomProperty(JOURNAL_PROPERTY, document["id"])
                    self._mark(layer, document)
                    return document
        except JournalError as exc:
            self._error(str(exc))
        return None

    def _matches_layer(self, document, layer):
        return (
            document["backend"] == normalise_base_url(self.plugin.settings.api_base_url)
            and document["track"] == layers.track_of(layer)
            and document["collection"] == layers.collection_of(layer)
            and layers.belongs_to_backend(layer, document["backend"])
        )

    def _baseline(self, layer, fid):
        baselines = self.baselines.setdefault(layer.id(), {})
        if fid in baselines:
            return
        request = QgsFeatureRequest().setFilterFid(fid)
        original = next(layer.dataProvider().getFeatures(request), None)
        if original is None or not original.isValid():
            baselines[fid] = None
            return
        fields = self.plugin.registry.fields if self.plugin.registry else DEFAULT_FIELDS
        names = [field.name() for field in layer.fields()]
        identity_field = next(
            (name for name in (fields.label_id, fields.extent_id) if name in names), ""
        )
        baselines[fid] = {
            "identity_field": identity_field,
            "identity": str(original[identity_field]) if identity_field else "",
            "version_field": fields.updated_at,
            "version": _version(original[fields.updated_at]) if fields.updated_at in names else "",
        }

    def changed(self, layer, fid=None):
        if self.closed or layer.id() in self.saving:
            return
        self.revisions[layer.id()] = self.revisions.get(layer.id(), 0) + 1
        if fid is not None and fid >= 0:
            self._baseline(layer, fid)
        if layer.id() not in self.scheduled:
            self.scheduled.add(layer.id())
            QTimer.singleShot(0, lambda: self._flush(layer))

    def _flush(self, layer):
        if self.closed:
            return
        try:
            if layer.id() not in self.watches:
                return
            self.scheduled.discard(layer.id())
            document = self.snapshot(layer)
            if document:
                self._warn(
                    f"{layer.name()}: edits are unpushed. A local recovery copy is saved. "
                    "Save Layer Edits to push now, or Connect to upload pending work."
                )
        except (JournalError, ValueError, TypeError, RuntimeError) as exc:
            self._error(
                f"Could not protect your unpushed edits: {exc}. Keep QGIS open and export the edited layer."
            )

    def snapshot(self, layer):
        buffer = layer.editBuffer()
        if buffer is None:
            return self.documents.get(layer.id())
        added = buffer.addedFeatures()
        changed = buffer.changedAttributeValues()
        geometries = buffer.changedGeometries()
        deleted = buffer.deletedFeatureIds()
        names = [field.name() for field in layer.fields()]
        added_fields = buffer.addedAttributes()
        if buffer.deletedAttributeIds() or (
            added_fields and layer.providerType() != layers.CLASS_PROVIDER
        ):
            raise ValueError("Field/schema changes must be saved or exported explicitly")
        added_fields = [_encode_field(field) for field in added_fields]
        operations = []
        for feature in added.values():
            operations.append(
                {
                    "kind": "create",
                    "attributes": {name: _encode(feature[name]) for name in names},
                    "geometry": bytes(feature.geometry().asWkb()).hex(),
                }
            )
        for fid in sorted((set(changed) | set(geometries) | set(deleted)) - set(added)):
            self._baseline(layer, fid)
            operations.append(
                {
                    "kind": "delete" if fid in deleted else "update",
                    "baseline": self.baselines[layer.id()][fid],
                    "attributes": {
                        names[index]: _encode(value)
                        for index, value in changed.get(fid, {}).items()
                    },
                    "geometry": bytes(geometries[fid].asWkb()).hex() if fid in geometries else None,
                }
            )
        previous = self.documents.get(layer.id())
        if previous and operations and layer.providerType() == layers.CLASS_PROVIDER:
            # QGIS may commit AddAttributes before a later feature PUT fails. The
            # field then leaves addedAttributes(), while its values remain unpushed.
            # Keep the definition until the complete save is acknowledged, so an
            # older saved project can still restore the field after reopening.
            remembered_names = {definition["name"] for definition in added_fields}
            for definition in previous.get("added_fields", []):
                name = definition["name"]
                if name in names and name not in remembered_names:
                    added_fields.append(definition)
                    remembered_names.add(name)
        if not operations and not added_fields:
            if previous and layer.id() not in self.bound_buffers:
                return previous
            if previous and previous["state"] == "pending":
                self.saved(layer)
            return previous if previous and previous["state"] != "pending" else None
        inherited_conflict = bool(layer.customProperty(STATE_PROPERTY, "") == "conflict")
        if previous and layer.id() not in self.bound_buffers:
            if (
                previous["operations"] != operations
                or previous.get("added_fields", []) != added_fields
            ):
                # The recovery copy has not been loaded into this new edit buffer.
                # Keep both rather than replacing yesterday's work with today's edit.
                previous = dict(
                    previous,
                    state="conflict",
                    note="New edits were made before this recovery copy was restored. Review both copies.",
                )
                self.store.save(previous)
                previous = None
                inherited_conflict = True
            self.bound_buffers.add(layer.id())
        document = (
            dict(previous)
            if previous
            else {
                "id": str(uuid4()),
                "backend": normalise_base_url(self.plugin.settings.api_base_url),
                "email": self.owners.get(layer.id(), self._owner()),
                "track": layers.track_of(layer),
                "collection": layers.collection_of(layer),
                "project": QgsProject.instance().fileName(),
                "layer_id": layer.id(),
                "layer_name": layer.name(),
                "state": "conflict" if inherited_conflict else "pending",
                "crs": layer.crs().toWkt(),
            }
        )
        if not document["email"] or not document["track"]:
            raise ValueError(
                "Sign in and connect this layer's track before editing; its owner cannot be identified"
            )
        document["operations"] = operations
        document["added_fields"] = added_fields
        self._persist(layer, document)
        self.bound_buffers.add(layer.id())
        return self.documents[layer.id()]

    def _persist(self, layer, document):
        key = document["id"]
        if key not in self.claimed:
            exists = (self.store.directory / (key + ".json")).exists()
            if not exists and any(row["id"] == key for row in self.documents.values()):
                raise JournalError(
                    "This recovery copy was removed by another session; check the server before retrying"
                )
            if (self.store.directory / (key + ".lock")).exists():
                raise JournalError(
                    "Another or interrupted QGIS session holds this recovery copy; review it before retrying"
                )
            if document["state"] == "pending" and exists:
                previous = self.store.load(key)
                if previous["state"] != "pending":
                    raise JournalError(
                        "This recovery copy was already submitted or held for review by another session"
                    )
        document = self.store.save(document)
        self.documents[layer.id()] = document
        layer.setCustomProperty(JOURNAL_PROPERTY, document["id"])
        self._mark(layer, document)

    def _mark(self, layer, document):
        label = (
            "needs review"
            if document["state"] != "pending"
            else str(len(document["operations"]) + len(document.get("added_fields", [])))
        )
        suffix = f" [Unpushed: {label}]"
        name = layer.name().split(" [Unpushed:", 1)[0]
        layer.setCustomProperty(NAME_PROPERTY, name)
        layer.setCustomProperty(STATE_PROPERTY, document["state"])
        layer.setName(name + suffix)

    def before_commit(self, layer):
        if not layer.isModified():
            return True
        try:
            document = self.snapshot(layer)
            if document:
                if document["operations"]:
                    document["state"] = "uncertain"
                    document["note"] = (
                        "A save was attempted. Check the server result before retrying."
                    )
                self._persist(layer, document)
                self.commits[layer.id()] = document["id"]
                QTimer.singleShot(0, lambda: self._warn_failed_save(layer))
            return True
        except (JournalError, ValueError, TypeError) as exc:
            # setAllowCommit is not exposed to Python. Do not claim the native save was stopped.
            self._error(
                f"Local recovery could not be saved: {exc}. Check the result of Save Layer Edits."
            )
            return False

    def _warn_failed_save(self, layer):
        document = self.documents.get(layer.id())
        if not self.closed and document and document["state"] == "uncertain":
            self._warn(
                f"{layer.name()}: the server has not confirmed the save. "
                "Your local recovery copy remains; automatic retry is paused to prevent duplicates."
            )

    def saved(self, layer):
        document = self.documents.get(layer.id())
        if document:
            try:
                self.store.delete(document["id"])
            except JournalError as exc:
                document["state"] = "uncertain"
                document["note"] = (
                    "Recovery cleanup failed after save/undo; verify server state before restoring."
                )
                with suppress(JournalError):
                    self._persist(layer, document)
                self._error(f"Save completed, but the recovery copy could not be removed: {exc}")
                return
        self.documents.pop(layer.id(), None)
        self.baselines.pop(layer.id(), None)
        self.bound_buffers.discard(layer.id())
        layer.removeCustomProperty(STATE_PROPERTY)
        layer.removeCustomProperty(JOURNAL_PROPERTY)
        layer.removeCustomProperty(NAME_PROPERTY)
        layer.setName(layer.name().split(" [Unpushed:", 1)[0])

    def committed(self, layer):
        if layer.providerType() == layers.CLASS_PROVIDER:
            layer.dataProvider().reset_write_session()
        # An empty Save or a different recovery copy is not acknowledgement of this journal.
        journal_id = self.commits.pop(layer.id(), None)
        if journal_id and self.documents.get(layer.id(), {}).get("id") == journal_id:
            self.saved(layer)
        QTimer.singleShot(0, lambda: self._refresh_committed_class_layer(layer))

    def _refresh_committed_class_layer(self, layer):
        if self.closed:
            return
        try:
            layers.refresh_class_layer_after_commit(layer)
        except LabelClientError as exc:
            self._error(f"Edits were saved, but the layer could not refresh: {exc}")
        except RuntimeError:
            # The project may have removed the Qt layer before the queued callback.
            return

    def rolled_back(self, layer):
        if layer.providerType() == layers.CLASS_PROVIDER:
            layer.dataProvider().reset_write_session()
        self.bound_buffers.discard(layer.id())
        document = self.documents.get(layer.id())
        if document:
            if (
                document["state"] == "pending"
                and document.get("added_fields")
                and not document["operations"]
            ):
                self.saved(layer)
                return
            document["state"] = "conflict"
            document["note"] = (
                "Editing was cancelled in QGIS. The recovery copy is held for review, not automatic upload."
            )
            try:
                self._persist(layer, document)
                self._warn(
                    "A local recovery copy remains under CVI Label Client → Unpushed edits. "
                    "Restore or discard it there; cancelled edits will not upload automatically."
                )
            except JournalError as exc:
                self._error(str(exc))

    def on_connected(self, finished):
        if self.syncing or self.closed:
            return
        self.watch_layers(layers.plugin_layers())
        for layer in layers.live_layers():
            if layer.id() in self.watches and layer.isModified():
                try:
                    self.snapshot(layer)
                except (JournalError, ValueError, TypeError) as exc:
                    self._error(str(exc))
                    return
        candidates = list(layers.live_layers())
        self.syncing = True

        def advance(stop=False):
            if self.closed or stop:
                self.syncing = False
                return
            while candidates:
                layer = candidates.pop(0)
                document = self.documents.get(layer.id())
                if not document:
                    continue
                self._mark(layer, document)
                if (
                    document["state"] != "pending"
                    or not self._matches_layer(document, layer)
                    or document["email"] != self.plugin.settings.oauth_email.casefold()
                    or self.plugin._current_write_access() is not True
                ):
                    continue
                self._check_and_push(layer, document, advance)
                return
            self.syncing = False
            finished()
            if self.documents:
                self._warn(
                    "Some edits remain unpushed. Open CVI Label Client → Unpushed edits for their status."
                )

        advance()

    def _check_and_push(self, layer, document, finished):
        # Snapshot all task inputs. A callback must not send edits made during its read.
        frozen = json.loads(json.dumps(document))
        context = self.plugin._permission_context()
        layer_id = layer.id()
        revision = self.revisions.get(layer_id, 0)
        handle = {}
        authcfg = self.plugin.settings.authcfg_by_track.get(document["track"], "")
        if not authcfg or not document["track"]:
            finished()
            return

        def check(feedback):
            for operation in frozen["operations"]:
                if feedback is not None and feedback.isCanceled():
                    raise JournalError(
                        "Checking pending edits was cancelled; no automatic save was started"
                    )
                if operation["kind"] == "create":
                    continue
                base = operation.get("baseline")
                if not base or not base["identity"] or not base["version"]:
                    return (
                        "The original feature/version could not be recorded. Review before saving."
                    )
                result = client.fetch_features(
                    frozen["backend"],
                    frozen["collection"],
                    authcfg,
                    {base["identity_field"]: base["identity"], "limit": 2},
                    feedback,
                    frozen["track"],
                )
                features = result.get("features", [])
                if len(features) != 1:
                    return "A changed feature was deleted or could not be uniquely found on the server."
                properties = features[0].get("properties", {})
                if (
                    str(properties.get(base["identity_field"], "")) != base["identity"]
                    or _version(properties.get(base["version_field"])) != base["version"]
                ):
                    return "A feature changed on the server since your edit. Review both versions before saving."
            return ""

        def done(problem):
            stop = False
            try:
                if (
                    self.closed
                    or layer_id not in self.watches
                    or context != self.plugin._permission_context()
                ):
                    stop = True
                    return
                task = handle.get("task")
                if task is not None and task.isCanceled():
                    stop = True
                    return
                if not self._matches_layer(frozen, layer):
                    stop = True
                    return
                if (
                    self.documents.get(layer_id) != frozen
                    or self.revisions.get(layer_id, 0) != revision
                ):
                    return
                if problem:
                    frozen.update(state="conflict", note=problem)
                    self._persist(layer, frozen)
                    return
                # Connect may have refused to repair an active layer's old auth ID.
                # Never let the auto-save use a different track/account credential.
                layers.repair_track_auth(layer, authcfg, frozen["backend"])
                with self.store.claim(frozen["id"], frozen):
                    self.claimed.add(frozen["id"])
                    try:
                        if not layer.isModified():
                            self._restore(layer, frozen)
                        # Persist uncertainty BEFORE the first native request is sent.
                        if not self.before_commit(layer):
                            return
                        self.saving.add(layer.id())
                        if layer.commitChanges(False):
                            self.saved(layer)
                        else:
                            self._warn_failed_save(layer)
                    finally:
                        self.claimed.discard(frozen["id"])
            except (LabelClientError, ValueError, TypeError, RuntimeError) as exc:
                self._error(f"Unpushed edits were kept for review: {exc}")
            finally:
                self.saving.discard(layer_id)
                if stop:
                    finished(stop=True)
                else:
                    finished()

        def failed(message):
            self._error(
                f"Unpushed edits remain saved locally. Reconnect could not check the server: {message}"
            )
            finished(stop=True)

        handle["task"] = self.plugin.tasks.run(
            "Check unpushed edits before reconnect upload",
            check,
            done,
            failed,
            deliver_when_cancelled=True,
        )

    def _restore(self, layer, document):
        if layer.isModified():
            raise ValueError("The layer already has unsaved edits; recovery will not replace them")
        if layer.crs().toWkt() != document["crs"]:
            raise ValueError("The layer coordinate system changed")
        targets = []
        layer.dataProvider().reloadData()
        if layer.providerType() == layers.CLASS_PROVIDER:
            layers.check_class_refresh(layer)
            layer.updateFields()
        names = [field.name() for field in layer.fields()]
        added_fields = document.get("added_fields", [])
        if not isinstance(added_fields, list):
            raise ValueError("The recovery copy has invalid field definitions")
        if added_fields and layer.providerType() != layers.CLASS_PROVIDER:
            raise ValueError("This layer cannot restore locally added fields")
        field_mapping = {}
        missing_fields = []
        descriptors = layer.dataProvider().field_definitions() if added_fields else []
        for definition in added_fields:
            field = _decode_field(definition)
            name = field.name()
            if name in field_mapping:
                raise ValueError("The recovery copy has duplicate field definitions")
            matches = [
                descriptor["name"]
                for descriptor in descriptors
                if descriptor.get("origin") == "src"
                and descriptor.get("source_name") == name
                and descriptor.get("name") in names
            ]
            if len(matches) > 1:
                raise ValueError(
                    "The server returned duplicate source fields; review before restoring"
                )
            if matches:
                # A populated native field may be inferred by the server before a
                # project is reopened. Use its canonical source key, never add both.
                field_mapping[name] = matches[0]
            elif name in names:
                raise ValueError(f"The saved field {name!r} now names a different server attribute")
            else:
                field_mapping[name] = name
                missing_fields.append(field)
        recoverable_names = set(names) | set(field_mapping)
        for operation in document["operations"]:
            if not set(operation["attributes"]).issubset(recoverable_names):
                raise ValueError("The server fields changed; export/review the recovery copy")
            fid = None
            if operation["kind"] != "create":
                base = operation.get("baseline")
                if not base or not base["identity_field"] or not base["identity"]:
                    raise ValueError("Missing stable feature identity")
                # Fetch by immutable UUID, never a provider/session-local numeric FID.
                field = base["identity_field"].replace('"', '""')
                identity = base["identity"].replace("'", "''")
                request = QgsFeatureRequest().setFilterExpression(f"\"{field}\" = '{identity}'")
                found = list(layer.dataProvider().getFeatures(request))
                if len(found) != 1:
                    raise ValueError("A saved feature is missing from this layer or its filters")
                fid = found[0].id()
                self.baselines.setdefault(layer.id(), {})[fid] = base
            targets.append((operation, fid))
        if not layer.isEditable() and not layer.startEditing():
            raise ValueError("The layer cannot enter editing mode; check write permissions")
        self.saving.add(layer.id())
        layer.beginEditCommand("Restore unpushed edits")
        try:
            for field in missing_fields:
                if not layer.addAttribute(field):
                    raise ValueError(f"Could not restore the added field {field.name()!r}")
            for operation, fid in targets:
                geometry = None
                if operation["geometry"] is not None:
                    geometry = QgsGeometry()
                    geometry.fromWkb(bytes.fromhex(operation["geometry"]))
                    if geometry.isNull():
                        raise ValueError("Invalid saved geometry")
                if operation["kind"] == "create":
                    feature = QgsFeature(layer.fields())
                    for name, value in operation["attributes"].items():
                        feature.setAttribute(field_mapping.get(name, name), _decode(value))
                    if geometry is not None:
                        feature.setGeometry(geometry)
                    if not layer.addFeature(feature):
                        raise ValueError("Could not restore a created feature")
                elif operation["kind"] == "delete":
                    if not layer.deleteFeature(fid):
                        raise ValueError("Could not restore a deletion")
                else:
                    for name, value in operation["attributes"].items():
                        if not layer.changeAttributeValue(
                            fid,
                            layer.fields().indexFromName(field_mapping.get(name, name)),
                            _decode(value),
                        ):
                            raise ValueError("Could not restore an attribute edit")
                    if geometry is not None and not layer.changeGeometry(fid, geometry):
                        raise ValueError("Could not restore a geometry edit")
            layer.endEditCommand()
        except Exception:
            layer.destroyEditCommand()
            raise
        finally:
            self.saving.discard(layer.id())
        layer.triggerRepaint()
        self.bound_buffers.add(layer.id())

    def review(self):
        if self.review_dialog is not None:
            self.review_dialog.close()
        dialog = QDialog(self.plugin.iface.mainWindow())
        self.review_dialog = dialog
        dialog.setWindowTitle("Unpushed edits")
        layout = QVBoxLayout(dialog)
        try:
            documents = self.store.list()
        except JournalError as exc:
            self._error(str(exc))
            return
        if not documents:
            layout.addWidget(QLabel("No saved unpushed edits.", dialog))
        for document in documents:
            text = (
                f"{document.get('layer_name', document['collection'])}: {len(document['operations'])} edit(s) — "
                f"{document['state']}\n{document['email']} · {document['track']}\n"
                f"{len(document.get('added_fields', []))} added field(s)\n"
                f"{document.get('note', 'Will upload after reconnect and server checks.')}\n"
                f"Recovery file: {self.store.directory / (document['id'] + '.json')}"
            )
            label = QLabel(text, dialog)
            label.setWordWrap(True)
            layout.addWidget(label)
            target = next(
                (
                    item
                    for item in layers.live_layers()
                    if item.customProperty(JOURNAL_PROPERTY, "") == document["id"]
                    or (
                        item.id() == document.get("layer_id")
                        and document["project"] == QgsProject.instance().fileName()
                    )
                ),
                None,
            )
            restore = QPushButton("Restore locally for review", dialog)
            restore.setEnabled(target is not None and document["email"] == self._owner())
            restore.clicked.connect(
                lambda _checked=False, row=document, item=target: self._review_restore(item, row)
            )
            layout.addWidget(restore)
            discard = QPushButton("Discard recovery copy…", dialog)
            discard.clicked.connect(
                lambda _checked=False, row=document, item=target: self._discard(item, row)
            )
            layout.addWidget(discard)
        dialog.show()

    def _review_restore(self, layer, document):
        try:
            if layer is None or not self._matches_layer(document, layer):
                raise ValueError("Open the original project and connect to its server first")
            if document["email"] != self.plugin.settings.oauth_email.casefold():
                raise ValueError("Sign in as the account which made these edits before restoring")
            self._restore(layer, document)
            self._persist(layer, document)
            self._warn(
                "Recovery edits are loaded locally. Compare with the server before Save Layer Edits; "
                "an earlier failed save may already have created some features."
            )
        except (JournalError, ValueError, TypeError, RuntimeError) as exc:
            self._error(str(exc))

    def _discard(self, layer, document):
        if layer is not None and layer.isModified():
            self._error(
                "Finish or discard the layer's active editing buffer first. "
                "Its recovery state cannot be reset while those edits remain open."
            )
            return
        answer = QMessageBox.question(
            self.plugin.iface.mainWindow(),
            "Discard recovery copy?",
            "Delete this local recovery copy? Any edits still open in QGIS remain in its edit buffer.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            if layer is not None and self.documents.get(layer.id(), {}).get("id") == document["id"]:
                self.saved(layer)
            else:
                self.store.delete(document["id"])
            self.review()
        except JournalError as exc:
            self._error(str(exc))

    def _warn(self, text):
        if self.closed or not self.plugin.settings.get("show_unpushed_warnings"):
            return
        if self.warning is None:
            self.warning = QMessageBox(self.plugin.iface.mainWindow())
            self.warning.setWindowTitle("Unpushed edits")
            self.warning.setIcon(QMessageBox.Icon.Warning)
            self.warning.setStandardButtons(QMessageBox.StandardButton.Ok)
            self.warning.setModal(False)
        self.warning.setText(text)
        self.warning.show()

    def _error(self, text):
        self.plugin._message(text, Qgis.MessageLevel.Critical)

    def close(self):
        # Persist pending timer work synchronously before the plugin is detached.
        for layer, bindings in self.watches.values():
            with suppress(RuntimeError):
                if layer.isEditable() and layer.isModified():
                    try:
                        self.snapshot(layer)
                    except (JournalError, ValueError, TypeError) as exc:
                        self._error(str(exc))
            for signal, callback in bindings:
                with suppress(RuntimeError, TypeError):
                    signal.disconnect(callback)
        self.closed = True
        for dialog in (self.warning, self.review_dialog):
            if dialog is not None:
                dialog.close()
        self.watches.clear()
