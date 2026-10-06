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
from qgis.PyQt.QtCore import QDate, QDateTime, Qt, QThread, QTime, QTimer, QVariant
from qgis.PyQt.QtWidgets import (
    QAction,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from . import client, diagnostics, layers
from .core import refusals
from .core.errors import LabelClientError
from .core.fields import DEFAULT_FIELDS
from .core.pending import JournalError, JournalStore, decode_value, encode_value
from .core.urls import normalise_base_url

JOURNAL_PROPERTY = "cvi/pending_journal"
STATE_PROPERTY = "cvi/pending_state"
NAME_PROPERTY = "cvi/pending_original_name"
# Versions 0.2.0-0.3.11 kept a copy of discarded edits with this note.
CANCELLED_NOTE = "Editing was cancelled in QGIS."


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
        # layer id -> was its copy "pending" when the current Save began?
        self.attempted_from_pending = {}
        self.claimed = set()
        self.scheduled = set()
        self.saving = set()
        self.closed = False
        self.syncing = False
        self.warning = None
        self.warning_trace = ""
        self.warning_copy = None
        self.review_dialog = None

    def install(self, menu):
        self.menu = menu
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
            # Journaling is a silent safety net, like autosave. QGIS already shows
            # that the layer has unsaved edits; only a failure to protect them speaks.
            self.snapshot(layer)
        except (JournalError, ValueError, TypeError, RuntimeError) as exc:
            self._error(
                f"Could not keep a local copy of your unsaved edits: {exc}. Keep QGIS open "
                "and export the edited layer, so the edits are not lost."
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
        if (
            document.get("state") == "pending"
            and previous
            and (
                previous["operations"] != operations
                or previous.get("added_fields", []) != added_fields
            )
        ):
            # A "Not saved: ..." note is about the edits that were refused. Once they
            # change, it may describe a problem already fixed, so it goes.
            document.pop("note", None)
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
            if layer.providerType() == layers.CLASS_PROVIDER:
                # A provider created by an older plugin version, still alive after an
                # in-session upgrade, does not have this. Saving must still work.
                begin = getattr(layer.dataProvider(), "begin_save", None)
                if begin is not None:
                    begin()
            document = self.snapshot(layer)
            if document:
                # Only a copy that was "not saved yet" when this Save began may go back to
                # that. One already held -- an earlier attempt reached the server in part,
                # or never answered -- stays held whatever this attempt is told. Recorded
                # once per Save: the upload on Connect calls this itself and then again
                # through commitChanges, when the copy is already marked uncertain.
                if layer.id() not in self.saving:
                    self.attempted_from_pending[layer.id()] = document["state"] == "pending"
                if document["operations"]:
                    document["state"] = "uncertain"
                    document["note"] = (
                        "A save was attempted. Check the server result before retrying."
                    )
                self._persist(layer, document)
                self.commits[layer.id()] = document["id"]
                level = QThread.currentThread().loopLevel()
                QTimer.singleShot(0, lambda: self._after_commit_returns(layer, level))
            return True
        except (JournalError, ValueError, TypeError) as exc:
            # setAllowCommit is not exposed to Python. Do not claim the native save was stopped.
            self._error(
                f"Local recovery could not be saved: {exc}. Check the result of Save Layer Edits."
            )
            return False

    def _after_commit_returns(self, layer, level):
        # The class provider awaits each HTTP request in a nested event loop, which
        # also runs zero-delay timers. Judge the save only after QGIS's commit returns.
        if self.closed:
            return
        if QThread.currentThread().loopLevel() > level:
            QTimer.singleShot(100, lambda: self._after_commit_returns(layer, level))
            return
        with suppress(RuntimeError):
            self._warn_failed_save(layer)

    def _warn_failed_save(self, layer):
        document = self.documents.get(layer.id())
        if self.closed or not document or document["state"] != "uncertain":
            return
        name = layer.customProperty(NAME_PROPERTY, "") or layer.name()
        provider = layer.dataProvider() if layer.providerType() == layers.CLASS_PROVIDER else None
        fresh = self.attempted_from_pending.pop(layer.id(), False)
        # Absent on a provider from an older plugin version: then nothing is known.
        refusal = getattr(provider, "save_refusal", lambda: None)()
        held = (
            "QGIS will not send these edits again automatically, because that could "
            "create duplicates. Your edits are kept here. Check the layer against the "
            "server, then use Unpushed edits… to keep or discard the local copy."
        )
        if refusal is not None and not fresh:
            # Refused before writing anything -- but the copy was already held, because
            # an earlier attempt may have reached the server. This answer changes nothing.
            reason = refusals.explain(refusal.status, refusal.payload, str(refusal))
            self._warn(
                f"{name} was not saved. {reason} An earlier save of these edits may "
                f"already have reached the server, so the copy stays held. {held}"
            )
            return
        if refusal is not None:
            # The server refused before anything was written, so nothing is in doubt:
            # these edits are simply not saved yet, and saving them again once fixed
            # cannot create a duplicate. Say why, and do not hold them for review.
            reason = refusals.explain(refusal.status, refusal.payload, str(refusal))
            if self._release_refused(layer, document, f"Not saved: {reason}"):
                self.plugin._message(
                    f"{name} was not saved. {reason} Your edits are still in the layer.",
                    Qgis.MessageLevel.Critical,
                )
                return
            # Refused, but the local copy could not be returned to "not saved yet" --
            # it changed on disk, or could not be read. Say exactly that.
            self._warn(
                f"{name} was not saved. {reason} Its local copy could not be updated to "
                "match, so it is kept for review: use Unpushed edits… to keep or discard it."
            )
            return
        seen = getattr(provider, "last_refusal", lambda: None)()
        if seen is not None:
            reason = refusals.explain(seen.status, seen.payload, str(seen))
            self._warn(
                f"{name}: part of this save reached the server before it refused the rest. "
                f"{reason} {held}"
            )
            return
        # Nothing here was classified, so claim no cause: say what stopped the save,
        # when it is known, and that what reached the server is unknown.
        failure = getattr(provider, "last_failure", lambda: None)()
        stopped = f" It stopped with: {failure}" if failure is not None else ""
        if stopped and not stopped.endswith((".", "!", "?")):
            stopped += "."
        self._warn(
            f"{name}: the save did not complete, and it is not known which edits reached "
            f"the server.{stopped} {held}"
        )

    def _release_refused(self, layer, document, note):
        """Return a refused save's copy to pending, if it is still the one this Save wrote."""
        try:
            stored = self.store.load(document["id"])
        except JournalError:
            return False
        if stored.get("state") != "uncertain" or stored.get("operations") != document["operations"]:
            return False
        key = document["id"]
        held = key in self.claimed
        self.claimed.add(key)
        try:
            self._persist(layer, dict(document, state="pending", note=note))
        except JournalError:
            return False
        finally:
            if not held:
                self.claimed.discard(key)
        return True

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
        self.attempted_from_pending.pop(layer.id(), None)
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
        bound = layer.id() in self.bound_buffers
        self.bound_buffers.discard(layer.id())
        document = self.documents.get(layer.id())
        # Discard means discard: QGIS has already asked. A copy recovered from an
        # earlier session was never in this buffer, and an attempted (uncertain) or
        # conflicting save may be partly on the server, so those copies stay.
        if document and bound and document["state"] == "pending":
            self.saved(layer)

    def _snapshot_live(self):
        self.watch_layers(layers.plugin_layers())
        for layer in layers.live_layers():
            if layer.id() in self.watches and layer.isModified():
                self.snapshot(layer)

    def _can_upload(self, layer):
        document = self.documents.get(layer.id())
        return bool(
            document
            and document["state"] == "pending"
            and self._matches_layer(document, layer)
            and document["email"] == self.plugin.settings.oauth_email.casefold()
            and self.plugin._current_write_access() is True
            and self.plugin.settings.authcfg_by_track.get(document["track"], "")
        )

    def uploadable(self):
        """Layers whose never-submitted edits this account may upload after Connect."""
        self._snapshot_live()
        return [layer for layer in layers.live_layers() if self._can_upload(layer)]

    def report_attention(self, skip=()):
        """One message-bar line for recovery copies that were not uploaded."""
        names = sorted(
            {
                str(
                    self.watches[layer_id][0].customProperty(NAME_PROPERTY, "")
                    or document.get("layer_name", document["collection"])
                )
                for layer_id, document in self.documents.items()
                if layer_id in self.watches and layer_id not in skip
            }
        )
        if names:
            bar = self.plugin.iface.messageBar()
            notes = {
                str(document.get("note") or "")
                for layer_id, document in self.documents.items()
                if layer_id in self.watches and layer_id not in skip
            } - {""}
            # A reason only when one layer is listed: beside several it would read as
            # the reason for all of them.
            reason = f" {notes.pop()}" if len(names) == 1 and len(notes) == 1 else ""
            text = (
                f"{', '.join(names)}: edits kept on this computer are not saved on the "
                f"server yet.{reason}"
            )
            item = bar.createMessage("CVI Label Client", text)
            button = QPushButton("Review…", item)
            button.clicked.connect(lambda _checked=False: self.review())
            item.layout().addWidget(button)
            trace = diagnostics.trace_for(text, context_of=self.plugin._trace_context)
            item.layout().addWidget(diagnostics.copy_button(trace, item))
            bar.pushWidget(item, Qgis.MessageLevel.Warning, -1)

    def on_connected(self, finished):
        if self.syncing or self.closed:
            return
        try:
            self._snapshot_live()
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
                if not self._can_upload(layer):
                    continue
                self._check_and_push(layer, document, advance)
                return
            self.syncing = False
            finished()
            self.report_attention()

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
                self._error(f"Your local edits were not sent and are kept for review: {exc}")
            finally:
                self.saving.discard(layer_id)
                if stop:
                    finished(stop=True)
                else:
                    finished()

        def failed(message):
            self._error(
                "Your unsaved edits are kept on this computer, but they could not be sent: "
                f"checking the server before sending them failed. {message}"
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

    def _target(self, document):
        return next(
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

    def _holds_open_edits(self, target, document):
        """A copy of edits still open in QGIS would be recreated by the next edit."""
        return (
            target is not None
            and target.isModified()
            and self.documents.get(target.id(), {}).get("id") == document["id"]
        )

    def _describe(self, document):
        """Plain-language title and details of one recovery copy."""
        name = document.get("layer_name", document["collection"]).split(" [Unpushed:", 1)[0]
        fields = len(document.get("added_fields", []))
        title = f"{name}: {len(document['operations'])} edit(s)" + (
            f", {fields} new field(s)" if fields else ""
        )
        note = document.get("note", "")
        if document["state"] == "pending":
            status = "Not uploaded yet. Connect offers to upload it."
        elif document["state"] == "uncertain":
            status = (
                "A save was attempted but the server did not confirm it. Some features may "
                "already be on the server; check the layer before redoing this work."
            )
        elif note.startswith(CANCELLED_NOTE):
            status = "You discarded these edits in QGIS; an earlier plugin version kept a copy."
        else:
            status = note or "Needs review before it can be uploaded."
        saved = ""
        with suppress(OSError):
            stamp = (self.store.directory / (document["id"] + ".json")).stat().st_mtime
            saved = f" · saved {datetime.fromtimestamp(stamp):%Y-%m-%d %H:%M}"
        return title, f"{status}\n{document['email']} · {document['track']}{saved}"

    def review(self):
        if self.review_dialog is not None:
            self.review_dialog.close()
        try:
            documents = self.store.list()
        except JournalError as exc:
            self._error(str(exc))
            return
        dialog = QDialog(self.plugin.iface.mainWindow())
        self.review_dialog = dialog
        dialog.setWindowTitle("Unpushed edits")
        layout = QVBoxLayout(dialog)
        intro = QLabel(
            "Copies of edits the server has not confirmed. Tick the ones you no longer "
            "need, then Delete selected."
            if documents
            else "No saved unpushed edits.",
            dialog,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        rows = []
        for document in documents:
            target = self._target(document)
            title, details = self._describe(document)
            box = QCheckBox(title, dialog)
            box.setToolTip(f"Recovery file: {self.store.directory / (document['id'] + '.json')}")
            if self._holds_open_edits(target, document):
                box.setEnabled(False)
                box.setToolTip("Save or discard this layer's open edits first.")
            layout.addWidget(box)
            label = QLabel(details, dialog)
            label.setWordWrap(True)
            layout.addWidget(label)
            restore = QPushButton("Restore locally for review", dialog)
            restore.setEnabled(target is not None and document["email"] == self._owner())
            restore.clicked.connect(
                lambda _checked=False, row=document, item=target: self._review_restore(item, row)
            )
            layout.addWidget(restore)
            rows.append((box, document["id"]))

        def select_all(_checked=False):
            for box, _key in rows:
                if box.isEnabled():
                    box.setChecked(True)

        buttons = QHBoxLayout()
        select = QPushButton("Select all", dialog)
        select.clicked.connect(select_all)
        delete = QPushButton("Delete selected…", dialog)
        delete.clicked.connect(
            lambda _checked=False: self._delete_selected(
                [key for box, key in rows if box.isChecked()]
            )
        )
        close = QPushButton("Close", dialog)
        close.clicked.connect(lambda _checked=False: dialog.close())
        for button in (select, delete, close):
            buttons.addWidget(button)
        select.setEnabled(bool(rows))
        delete.setEnabled(bool(rows))
        layout.addLayout(buttons)
        dialog.show()

    def _delete_selected(self, ids):
        if not ids:
            self.plugin._message("Tick the copies to delete first.")
            return
        count = f"{len(ids)} recovery cop{'y' if len(ids) == 1 else 'ies'}"
        answer = QMessageBox.question(
            self.review_dialog,
            "Delete unpushed edits?",
            f"Delete {count}? Their edits will not be uploaded. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.delete_copies(ids)
        self.review()

    def delete_copies(self, ids):
        """Delete recovery copies by ID. Returns the IDs kept because their edits are open."""
        try:
            documents = {row["id"]: row for row in self.store.list()}
        except JournalError as exc:
            self._error(str(exc))
            return list(ids)
        kept = []
        for key in ids:
            document = documents.get(key)
            if document is None:
                continue
            target = self._target(document)
            if self._holds_open_edits(target, document):
                kept.append(key)
                continue
            try:
                if target is not None and self.documents.get(target.id(), {}).get("id") == key:
                    self.saved(target)
                else:
                    self.store.delete(key)
            except JournalError as exc:
                self._error(str(exc))
                kept.append(key)
        if kept:
            count = f"{len(kept)} recovery cop{'y was' if len(kept) == 1 else 'ies were'} kept"
            self.plugin._message(
                f"{count}: save or discard the open edits in their layers first.",
                Qgis.MessageLevel.Warning,
            )
        return kept

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

    def _warn(self, text):
        if self.closed:
            return
        self.warning_trace = diagnostics.trace_for(text, context_of=self.plugin._trace_context)
        if self.warning is None:
            self.warning = QMessageBox(self.plugin.iface.mainWindow())
            self.warning.setWindowTitle("Save not confirmed")
            self.warning.setIcon(QMessageBox.Icon.Warning)
            self.warning.setStandardButtons(QMessageBox.StandardButton.Ok)
            self.warning.setModal(False)
            self.warning_copy = diagnostics.add_copy_button(
                self.warning, lambda: self.warning_trace
            )
        self.warning_copy.setText(diagnostics.COPY_LABEL)
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
