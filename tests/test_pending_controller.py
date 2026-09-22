"""Recovery guards must preserve local work and never repeat an ambiguous save.

These tests exercise controller decisions using captured task callbacks. They do
not simulate an OAPIF provider or substitute for native QGIS validation.
"""

from contextlib import suppress
from types import SimpleNamespace

import pytest
from qgis_stubs import Signal

from qgis_label_client import pending
from qgis_label_client.core.pending import JournalError, JournalStore


class Settings:
    def __init__(self):
        self.api_base_url = "https://api.example.org"
        self.oauth_email = "analyst@example.org"
        self.authcfg_by_track = {"default": "auth001"}
        self.track = "default"
        self.values = {"signed_out_connection": "", "show_unpushed_warnings": False}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


class Tasks:
    def __init__(self):
        self.calls = []

    def run(self, description, work, done, failed, **_kwargs):
        self.calls.append(
            SimpleNamespace(description=description, work=work, done=done, failed=failed)
        )


class Feature:
    def __init__(self, name):
        self.attributes = {"name": name}

    def __getitem__(self, name):
        return self.attributes[name]

    def geometry(self):
        return SimpleNamespace(
            asWkb=lambda: bytes.fromhex("0101000000000000000000f03f0000000000000040")
        )


class EditBuffer:
    def __init__(self, name=None):
        self.added = {-1: Feature(name)} if name is not None else {}

    def addedFeatures(self):  # noqa: N802
        return self.added

    def changedAttributeValues(self):  # noqa: N802
        return {}

    def changedGeometries(self):  # noqa: N802
        return {}

    def deletedFeatureIds(self):  # noqa: N802
        return set()

    def addedAttributes(self):  # noqa: N802
        return []

    def deletedAttributeIds(self):  # noqa: N802
        return []


class Layer:
    def __init__(self, layer_id="layer-original", name="original edit"):
        self.layer_id = layer_id
        self.title = "Labels"
        self.properties = {}
        self.buffer = EditBuffer(name)
        self.commits = 0
        self.editable = True
        for signal in (
            "editingStarted",
            "featureAdded",
            "featureDeleted",
            "geometryChanged",
            "attributeValueChanged",
            "beforeCommitChanges",
            "afterCommitChanges",
            "afterRollBack",
            "beforeRollBack",
            "willBeDeleted",
            "destroyed",
        ):
            setattr(self, signal, Signal(signal))

    def id(self):
        return self.layer_id

    def name(self):
        return self.title

    def setName(self, title):  # noqa: N802
        self.title = title

    def customProperty(self, key, default=""):  # noqa: N802
        return self.properties.get(key, default)

    def setCustomProperty(self, key, value):  # noqa: N802
        self.properties[key] = value

    def removeCustomProperty(self, key):  # noqa: N802
        self.properties.pop(key, None)

    def isEditable(self):  # noqa: N802
        return self.editable

    def isModified(self):  # noqa: N802
        return bool(self.buffer.added)

    def editBuffer(self):  # noqa: N802
        return self.buffer

    def fields(self):
        return [SimpleNamespace(name=lambda: "name")]

    def crs(self):
        return SimpleNamespace(authid=lambda: "EPSG:4326", toWkt=lambda: "WGS 84")

    def commitChanges(self, stop_editing=True):  # noqa: N802
        self.commits += 1
        self.beforeCommitChanges.emit(stop_editing)
        self.afterCommitChanges.emit()
        self.buffer.added.clear()
        return True


@pytest.fixture
def controller(tmp_path, monkeypatch, fake_iface):
    settings = Settings()
    plugin = SimpleNamespace(
        settings=settings,
        iface=fake_iface,
        registry=None,
        tasks=Tasks(),
        teardown=SimpleNamespace(add=lambda *_args: None),
        _session=SimpleNamespace(generation=1),
        _track_change_serial=1,
    )
    plugin._permission_context = lambda: (
        settings.api_base_url,
        settings.oauth_email,
        tuple(settings.authcfg_by_track.items()),
    )
    plugin._current_write_access = lambda: True
    plugin._message = lambda text, *_args: fake_iface.messages.append(text)
    instance = pending.PendingEdits(plugin)
    instance.store = JournalStore(tmp_path / "journals")
    timers = []
    monkeypatch.setattr(
        pending.QTimer, "singleShot", lambda delay, callback: timers.append(callback)
    )
    monkeypatch.setattr(pending.layers, "is_plugin_layer", lambda _layer: True)
    monkeypatch.setattr(pending.layers, "is_historical", lambda _layer: False)
    monkeypatch.setattr(pending.layers, "belongs_to_backend", lambda _layer, _url: True)
    monkeypatch.setattr(pending.layers, "repair_track_auth", lambda *_args: False)
    monkeypatch.setattr(pending.layers, "track_of", lambda _layer: "default")
    monkeypatch.setattr(pending.layers, "collection_of", lambda _layer: "label_point")
    instance.test_timers = timers
    return instance


def test_new_edit_invalidates_check_before_deferred_journal_flush(controller):
    layer = Layer()
    controller.watch_layers([layer])
    document = controller.snapshot(layer)
    controller._check_and_push(layer, document, lambda: None)
    task = controller.plugin.tasks.calls[-1]
    layer.buffer.added[-2] = Feature("edit made while check was in flight")
    controller.changed(layer, -2)
    # Deliberately do not run QTimer callbacks: the invalidation must be immediate.
    task.done("")
    assert layer.commits == 0
    assert layer.buffer.added[-2]["name"] == "edit made while check was in flight"


def test_successful_native_commit_queues_class_revision_refresh_after_buffer_clear(
    controller, monkeypatch
):
    layer = Layer()
    controller.watch_layers([layer])
    refreshed = []
    monkeypatch.setattr(
        pending.layers,
        "refresh_class_layer_after_commit",
        lambda target: refreshed.append(target.isModified()),
    )
    layer.commitChanges(False)
    assert refreshed == []
    for callback in controller.test_timers:
        callback()
    assert refreshed == [False]


def test_queued_class_refresh_stops_after_plugin_unload(controller, monkeypatch):
    layer = Layer()
    controller.watch_layers([layer])
    refreshed = []
    monkeypatch.setattr(
        pending.layers, "refresh_class_layer_after_commit", lambda target: refreshed.append(target)
    )
    layer.afterCommitChanges.emit()
    controller.closed = True
    for callback in controller.test_timers:
        callback()
    assert refreshed == []


def test_automatic_save_requires_durable_uncertain_marker(controller, monkeypatch):
    layer = Layer()
    controller.watch_layers([layer])
    document = controller.snapshot(layer)
    controller._check_and_push(layer, document, lambda: None)

    def cannot_save(_document):
        raise JournalError("disk full")

    monkeypatch.setattr(controller.store, "save", cannot_save)
    controller.plugin.tasks.calls[-1].done("")
    assert layer.commits == 0
    assert layer.buffer.added[-1]["name"] == "original edit"
    assert controller.store.load(document["id"])["operations"] == document["operations"]


def test_discarding_recovery_never_reclassifies_uncertain_buffer_as_pending(
    controller, monkeypatch
):
    layer = Layer()
    controller.watch_layers([layer])
    document = controller.snapshot(layer)
    document["state"] = "uncertain"
    controller._persist(layer, document)
    monkeypatch.setattr(
        pending.QMessageBox, "question", lambda *_args: pending.QMessageBox.StandardButton.Yes
    )
    monkeypatch.setattr(controller, "review", lambda: None)
    controller._discard(layer, document)
    assert layer.buffer.added[-1]["name"] == "original edit"
    assert controller.store.load(document["id"])["state"] != "pending"
    assert controller.documents[layer.id()]["state"] != "pending"


def test_review_rechecks_account_when_button_is_clicked(controller, monkeypatch):
    layer = Layer()
    controller.watch_layers([layer])
    document = controller.snapshot(layer)
    restored = []
    monkeypatch.setattr(controller, "_restore", lambda *_args: restored.append(True))
    controller.plugin.settings.oauth_email = "different@example.org"
    controller._review_restore(layer, document)
    assert restored == []
    assert controller.store.load(document["id"])["email"] == "analyst@example.org"


def test_duplicate_layer_cannot_claim_same_recovery_journal(controller):
    original = Layer()
    controller.watch_layers([original])
    document = controller.snapshot(original)
    clone = Layer("layer-copy", name=None)
    clone.properties = dict(original.properties)
    controller.watch_layers([clone])
    claims = [row for row in controller.documents.values() if row["id"] == document["id"]]
    assert len(claims) == 1
    assert controller.store.load(document["id"])["operations"] == document["operations"]


def test_new_buffer_cannot_overwrite_unrestored_recovery(controller):
    original = Layer()
    controller.watch_layers([original])
    document = controller.snapshot(original)
    original_operations = document["operations"]
    reopened = Layer(original.id(), name=None)
    reopened.properties = dict(original.properties)
    recovered = pending.PendingEdits(controller.plugin)
    recovered.store = controller.store
    recovered.watch_layers([reopened])
    reopened.buffer.added[-1] = Feature("new session edit")
    recovered.changed(reopened, -1)
    # A visible refusal is safe; silently replacing the old work is not.
    with suppress(JournalError, ValueError):
        recovered.snapshot(reopened)
    assert any(row["operations"] == original_operations for row in recovered.store.list())
    assert reopened.buffer.added[-1]["name"] == "new session edit"


def test_qt_version_precision_and_timezone_match_wire_timestamp():
    assert pending._version("2026-09-22T15:00:01.123+03:00") == pending._version(
        "2026-09-22T12:00:01.123456Z"
    )
    assert pending._version("2026-09-22T12:00:01.124Z") != pending._version(
        "2026-09-22T12:00:01.123456Z"
    )


def test_restore_accepts_qgis_from_wkb_void_return(controller, monkeypatch):
    original = Layer()
    controller.watch_layers([original])
    document = controller.snapshot(original)
    clean = Layer("clean-recovery", name=None)
    restored = []

    class Geometry:
        def fromWkb(self, value):  # noqa: N802
            self.value = value
            return None  # QGIS's actual API returns void, never a success boolean.

        def isNull(self):  # noqa: N802
            return not self.value

    class RestoredFeature:
        def __init__(self, _fields):
            self.attributes = {}

        def setAttribute(self, name, value):  # noqa: N802
            self.attributes[name] = value

        def setGeometry(self, geometry):  # noqa: N802
            self.geometry = geometry

    monkeypatch.setattr(pending, "QgsGeometry", Geometry)
    monkeypatch.setattr(pending, "QgsFeature", RestoredFeature)
    clean.dataProvider = lambda: SimpleNamespace(reloadData=lambda: None)
    clean.beginEditCommand = lambda _name: None
    clean.endEditCommand = lambda: None
    clean.destroyEditCommand = lambda: None
    clean.triggerRepaint = lambda: None
    clean.addFeature = lambda feature: restored.append(feature) or True
    controller._restore(clean, document)
    assert len(restored) == 1
    assert restored[0].attributes == {"name": "original edit"}
    assert restored[0].geometry.value.hex() == document["operations"][0]["geometry"]
