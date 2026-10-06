"""Recovery guards must preserve local work and never repeat an ambiguous save.

These tests exercise controller decisions using captured task callbacks. They do
not simulate an OAPIF provider or substitute for native QGIS validation.
"""

from contextlib import suppress
from types import SimpleNamespace

import pytest
from qgis_stubs import Signal

from qgis_label_client import pending
from qgis_label_client.core.errors import BackendError
from qgis_label_client.core.pending import JournalError, JournalStore


class Settings:
    def __init__(self):
        self.api_base_url = "https://api.example.org"
        self.oauth_email = "analyst@example.org"
        self.authcfg_by_track = {"default": "auth001"}
        self.track = "default"
        self.values = {"signed_out_connection": ""}

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
        self.added_fields = []
        self.deleted_fields = []

    def addedFeatures(self):  # noqa: N802
        return self.added

    def changedAttributeValues(self):  # noqa: N802
        return {}

    def changedGeometries(self):  # noqa: N802
        return {}

    def deletedFeatureIds(self):  # noqa: N802
        return set()

    def addedAttributes(self):  # noqa: N802
        return self.added_fields

    def deletedAttributeIds(self):  # noqa: N802
        return self.deleted_fields


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
            "attributeAdded",
            "attributeDeleted",
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
        return bool(self.buffer.added or self.buffer.added_fields or self.buffer.deleted_fields)

    def editBuffer(self):  # noqa: N802
        return self.buffer

    def fields(self):
        return [SimpleNamespace(name=lambda: "name"), *self.buffer.added_fields]

    def providerType(self):  # noqa: N802
        return "oapif"

    def dataProvider(self):  # noqa: N802
        return SimpleNamespace(reset_write_session=lambda: None, last_refresh_error="")

    def updateFields(self):  # noqa: N802
        pass

    def crs(self):
        return SimpleNamespace(authid=lambda: "EPSG:4326", toWkt=lambda: "WGS 84")

    def commitChanges(self, stop_editing=True):  # noqa: N802
        self.commits += 1
        self.beforeCommitChanges.emit(stop_editing)
        self.afterCommitChanges.emit()
        self.buffer.added.clear()
        self.buffer.added_fields.clear()
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
    plugin._trace_context = lambda: (("Backend", settings.api_base_url),)
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


def test_save_outcome_is_judged_only_after_the_native_commit_returns(controller, monkeypatch):
    shown = []
    controller._warn = shown.append
    loop = SimpleNamespace(level=1)
    monkeypatch.setattr(
        pending.QThread, "currentThread", lambda: SimpleNamespace(loopLevel=lambda: loop.level)
    )
    monkeypatch.setattr(pending.layers, "refresh_class_layer_after_commit", lambda _layer: None)

    def run_timers():
        due = list(controller.test_timers)
        controller.test_timers.clear()
        for callback in due:
            callback()

    layer = Layer()
    controller.watch_layers([layer])
    layer.beforeCommitChanges.emit(True)
    # The class provider awaits its POST in a nested event loop, which runs timers too.
    loop.level = 2
    run_timers()
    assert shown == []
    loop.level = 1
    layer.afterCommitChanges.emit()
    run_timers()
    assert shown == []
    assert controller.store.list() == []

    layer.buffer.added[-2] = Feature("refused by the server")
    controller.changed(layer, -2)
    run_timers()
    layer.beforeCommitChanges.emit(True)
    # The commit returned without afterCommitChanges: QGIS could not save.
    run_timers()
    assert len(shown) == 1
    assert shown[0].startswith(
        "Labels: the save did not complete, and it is not known which edits reached the server."
    )


SELF_INTERSECTION = {
    "code": "GeometryInvalid",
    "description": "the feature is not a valid geometry: Self-intersection at "
    "POINT(12.34567891234567 45.67890123456). This is the same check app.label_check() makes.",
}


class ClassLayer(Layer):
    """A class layer whose provider reports what the server said to this Save."""

    def __init__(self, refusal, *, wrote=False, failure=None):
        super().__init__()
        self.provider = SimpleNamespace(
            reset_write_session=lambda: None,
            last_refresh_error="",
            begin_save=lambda: None,
            save_refusal=lambda: None if wrote else refusal,
            last_refusal=lambda: refusal,
            last_failure=lambda: failure or refusal,
        )

    def providerType(self):  # noqa: N802
        return pending.layers.CLASS_PROVIDER

    def dataProvider(self):  # noqa: N802
        return self.provider


def _refused_save(controller, monkeypatch, layer):
    shown = []
    controller._warn = shown.append
    monkeypatch.setattr(
        pending.QThread, "currentThread", lambda: SimpleNamespace(loopLevel=lambda: 1)
    )
    controller.watch_layers([layer])
    layer.beforeCommitChanges.emit(True)
    # The commit returned without afterCommitChanges: the server refused it.
    for callback in list(controller.test_timers):
        callback()
    return shown


def test_a_refused_save_says_why_and_keeps_the_edits_ready_to_save(controller, monkeypatch):
    refusal = BackendError("HTTP 422", status=422, payload=SELF_INTERSECTION)
    layer = ClassLayer(refusal)
    shown = _refused_save(controller, monkeypatch, layer)
    # Nothing was written, so nothing is in doubt: no duplicate-risk dialog.
    assert shown == []
    assert controller.plugin.iface.messages[-1] == (
        "Labels was not saved. The server refused a shape because its outline crosses "
        "itself near 45.678901° N, 12.345679° E. Find it with Vector ▸ Geometry Tools ▸ "
        "Check Validity, fix it (or run the Fix geometries tool), then Save again. Your edits are "
        "still in the layer."
    )
    [copy] = controller.store.list()
    assert copy["state"] == "pending"
    assert copy["note"].startswith("Not saved: The server refused a shape")
    assert layer.name() == "Labels [Unpushed: 1]"


def test_a_refusal_after_part_of_the_save_was_written_stays_held(controller, monkeypatch):
    refusal = BackendError("HTTP 422", status=422, payload=SELF_INTERSECTION)
    layer = ClassLayer(refusal, wrote=True)
    shown = _refused_save(controller, monkeypatch, layer)
    assert len(shown) == 1
    assert shown[0].startswith(
        "Labels: part of this save reached the server before it refused the rest. "
        "The server refused a shape because its outline crosses itself"
    )
    assert controller.store.list()[0]["state"] == "uncertain"


def test_an_unclassified_failure_is_quoted_not_explained(controller, monkeypatch):
    failure = ValueError("This class layer is not editable")
    shown = _refused_save(controller, monkeypatch, ClassLayer(None, failure=failure))
    assert len(shown) == 1
    assert "It stopped with: This class layer is not editable." in shown[0]
    assert "connection" not in shown[0] and "timed out" not in shown[0]


def test_a_refusal_whose_copy_cannot_be_released_says_so(controller, monkeypatch):
    shown = []
    controller._warn = shown.append
    monkeypatch.setattr(
        pending.QThread, "currentThread", lambda: SimpleNamespace(loopLevel=lambda: 1)
    )
    layer = ClassLayer(BackendError("HTTP 422", status=422, payload=SELF_INTERSECTION))
    controller.watch_layers([layer])
    layer.beforeCommitChanges.emit(True)
    # Another session changed the copy on disk before this one judged the Save.
    load = controller.store.load
    monkeypatch.setattr(controller.store, "load", lambda key: dict(load(key), state="conflict"))
    for callback in list(controller.test_timers):
        callback()
    assert len(shown) == 1
    assert shown[0].startswith("Labels was not saved. The server refused a shape")
    assert "could not be updated" in shown[0]
    assert "part of this save" not in shown[0]


def test_a_not_saved_note_goes_once_the_edits_change(controller, monkeypatch):
    refusal = BackendError("HTTP 422", status=422, payload=SELF_INTERSECTION)
    layer = ClassLayer(refusal)
    _refused_save(controller, monkeypatch, layer)
    assert controller.store.list()[0]["note"].startswith("Not saved:")
    layer.buffer.added[-2] = Feature("the fixed shape")
    controller.snapshot(layer)
    assert "note" not in controller.store.list()[0]


def test_a_copy_already_held_is_not_released_by_a_later_clean_refusal(controller, monkeypatch):
    """A partly written Save holds the copy. The next Save is refused before sending
    anything -- but the earlier attempt may already have created or updated rows, so
    releasing the copy now would let a later upload create them a second time."""
    refusal = BackendError("HTTP 422", status=422, payload=SELF_INTERSECTION)
    layer = ClassLayer(refusal, wrote=True)
    _refused_save(controller, monkeypatch, layer)
    assert controller.store.list()[0]["state"] == "uncertain"

    layer.provider.save_refusal = lambda: refusal  # this attempt wrote nothing
    controller.test_timers.clear()
    shown = _refused_save(controller, monkeypatch, layer)
    assert controller.store.list()[0]["state"] == "uncertain"
    assert len(shown) == 1
    assert shown[0].startswith("Labels was not saved. The server refused a shape")
    assert "so the copy stays held" in shown[0]
    assert controller.plugin.iface.messages == []


class RefusingClassLayer(ClassLayer):
    """QGIS's commit as the class provider makes it when the server refuses: the
    native signal fires, nothing is acknowledged, and the commit returns False."""

    def commitChanges(self, stop_editing=True):  # noqa: N802
        self.commits += 1
        self.beforeCommitChanges.emit(stop_editing)
        return False


def test_a_first_upload_on_connect_that_is_refused_goes_back_to_unsaved(controller, monkeypatch):
    """The upload calls before_commit itself and again through commitChanges. The
    second call sees the copy already marked uncertain; it must not decide where the
    copy started, or a first refusal reads as a held earlier attempt."""
    monkeypatch.setattr(
        pending.QThread, "currentThread", lambda: SimpleNamespace(loopLevel=lambda: 1)
    )
    shown = []
    controller._warn = shown.append
    layer = RefusingClassLayer(BackendError("HTTP 422", status=422, payload=SELF_INTERSECTION))
    controller.watch_layers([layer])
    document = controller.snapshot(layer)
    controller._check_and_push(layer, document, lambda: None)
    controller.plugin.tasks.calls[-1].done("")
    for callback in list(controller.test_timers):
        callback()
    assert layer.commits == 1
    assert shown == []
    assert controller.store.list()[0]["state"] == "pending"
    assert controller.plugin.iface.messages[-1].startswith(
        "Labels was not saved. The server refused a shape"
    )
    assert layer.name() == "Labels [Unpushed: 1]"


def test_a_refused_sign_in_says_to_sign_in_again(controller, monkeypatch):
    refusal = BackendError(
        "HTTP 401 from https://api.example.org The API rejected the credential.",
        status=401,
        payload={"detail": "ID token could not be verified"},
    )
    _refused_save(controller, monkeypatch, ClassLayer(refusal))
    assert controller.plugin.iface.messages[-1] == (
        "Labels was not saved. The server did not accept your sign-in. Sign in again from "
        "the CVI panel, then Save again. Your edits are still in the layer."
    )


def test_one_layers_note_is_not_given_as_the_reason_for_several(controller):
    first, second = Layer(), Layer("layer-second")
    second.title = "Second"
    controller.watch_layers([first, second])
    noted = controller.snapshot(first)
    noted["note"] = "Not saved: something about the first layer only."
    controller._persist(first, noted)
    controller.snapshot(second)
    controller.report_attention()
    [(_title, text, _level)] = controller.plugin.iface.messages
    assert text == "Labels, Second: edits kept on this computer are not saved on the server yet."


def test_editing_is_journaled_silently(controller):
    shown = []
    controller._warn = shown.append
    layer = Layer()
    controller.watch_layers([layer])
    for fid in (-2, -3):
        layer.buffer.added[fid] = Feature("another edit")
        controller.changed(layer, fid)
    for callback in controller.test_timers:
        callback()
    # QGIS already shows unsaved edits; the journal is a safety net, not news.
    assert shown == []
    assert controller.plugin.iface.messages == []
    assert len(controller.store.list()[0]["operations"]) == 3


def test_discarding_edits_deletes_their_never_submitted_copy_silently(controller):
    shown = []
    controller._warn = shown.append
    layer = Layer()
    controller.watch_layers([layer])
    controller.snapshot(layer)
    layer.buffer.added.clear()
    layer.afterRollBack.emit()
    assert controller.store.list() == []
    assert layer.name() == "Labels"
    assert shown == []


def test_discarding_new_edits_keeps_a_copy_recovered_from_an_earlier_session(controller):
    layer = Layer()
    controller.watch_layers([layer])
    document = controller.snapshot(layer)
    # Reopened project: the copy is attached to the layer, but not to its new buffer.
    controller.bound_buffers.discard(layer.id())
    layer.afterRollBack.emit()
    assert [row["id"] for row in controller.store.list()] == [document["id"]]


def test_discarding_after_an_attempted_save_keeps_the_uncertain_copy(controller):
    layer = Layer()
    controller.watch_layers([layer])
    document = controller.snapshot(layer)
    document["state"] = "uncertain"
    controller._persist(layer, document)
    layer.afterRollBack.emit()
    assert controller.store.load(document["id"])["state"] == "uncertain"


def test_connect_uploads_only_pending_edits_for_this_account(controller, monkeypatch):
    layer, other = Layer(), Layer("layer-other")
    monkeypatch.setattr(pending.layers, "plugin_layers", lambda: [layer, other])
    monkeypatch.setattr(pending.layers, "live_layers", lambda: [layer, other])
    controller.watch_layers([layer, other])
    controller.snapshot(layer)
    held = controller.snapshot(other)
    held["state"] = "conflict"
    controller._persist(other, held)
    assert controller.uploadable() == [layer]
    controller.plugin._current_write_access = lambda: False
    assert controller.uploadable() == []


def test_copies_left_after_connect_are_reported_once_in_the_message_bar(controller):
    shown = []
    controller._warn = shown.append
    layer = Layer()
    controller.watch_layers([layer])
    controller.snapshot(layer)
    controller.report_attention(skip={layer.id()})
    assert controller.plugin.iface.messages == []
    controller.report_attention()
    assert [text for _title, text, _level in controller.plugin.iface.messages] == [
        "Labels: edits kept on this computer are not saved on the server yet."
    ]
    # The line carries a Review… button that opens the cleanup dialog, and the trace copy.
    assert len(controller.plugin.iface.message_items[0].widgets) == 2
    assert shown == []


def test_cleanup_deletes_selected_copies_and_clears_layer_markers(controller, monkeypatch):
    layer, gone = Layer(), Layer("layer-removed")
    controller.watch_layers([layer, gone])
    attached = controller.snapshot(layer)
    orphan = controller.snapshot(gone)
    layer.buffer.added.clear()
    monkeypatch.setattr(pending.layers, "live_layers", lambda: [layer])
    assert "[Unpushed:" in layer.name()
    assert controller.delete_copies([attached["id"], orphan["id"]]) == []
    assert controller.store.list() == []
    assert layer.name() == "Labels"


def test_cleanup_keeps_a_copy_of_edits_still_open_in_qgis(controller, monkeypatch):
    layer = Layer()
    controller.watch_layers([layer])
    document = controller.snapshot(layer)
    monkeypatch.setattr(pending.layers, "live_layers", lambda: [layer])
    assert controller.delete_copies([document["id"]]) == [document["id"]]
    assert controller.store.load(document["id"])["operations"] == document["operations"]
    assert "save or discard the open edits" in controller.plugin.iface.messages[-1]


def test_cleanup_dialog_confirms_once_then_deletes_and_refreshes(controller, monkeypatch):
    layer = Layer()
    controller.watch_layers([layer])
    document = controller.snapshot(layer)
    layer.buffer.added.clear()
    monkeypatch.setattr(pending.layers, "live_layers", lambda: [layer])
    controller.review()
    first = controller.review_dialog
    questions = []
    monkeypatch.setattr(
        pending.QMessageBox,
        "question",
        lambda _parent, _title, text, *_args: (
            questions.append(text) or pending.QMessageBox.StandardButton.Yes
        ),
    )
    controller._delete_selected([document["id"]])
    assert questions == [
        "Delete 1 recovery copy? Their edits will not be uploaded. This cannot be undone."
    ]
    assert controller.store.list() == []
    assert controller.review_dialog is not first


def test_cleanup_explains_copies_kept_by_earlier_discards(controller):
    layer = Layer()
    controller.watch_layers([layer])
    document = controller.snapshot(layer)
    document.update(
        state="conflict",
        note="Editing was cancelled in QGIS. The recovery copy is held for review, "
        "not automatic upload.",
    )
    title, details = controller._describe(document)
    assert title == "Labels: 1 edit(s)"
    assert details.startswith("You discarded these edits in QGIS")
    document["state"] = "uncertain"
    assert "save was attempted" in controller._describe(document)[1]


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
    monkeypatch.setattr(pending.layers, "live_layers", lambda: [layer])
    assert controller.delete_copies([document["id"]]) == [document["id"]]
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


def _new_field(name="Inspection note"):
    return SimpleNamespace(
        name=lambda: name,
        type=lambda: 10,
        typeName=lambda: "Text",
        subType=lambda: 0,
        length=lambda: 80,
        precision=lambda: 0,
        comment=lambda: "Original local definition",
    )


def test_saved_field_definition_restores_type_length_precision_and_comment(monkeypatch):
    captured = []
    monkeypatch.setattr(pending, "QVariant", SimpleNamespace(Type=int))
    monkeypatch.setattr(pending, "QgsField", lambda *args: captured.append(args) or args)
    definition = pending._encode_field(_new_field())
    pending._decode_field(definition)
    assert captured == [("Inspection note", 10, "Text", 80, 0, "Original local definition", 0)]


def test_native_added_field_is_journaled_without_feature_edits(controller):
    layer = Layer(name=None)
    layer.providerType = lambda: pending.layers.CLASS_PROVIDER
    controller.watch_layers([layer])
    layer.buffer.added_fields.append(_new_field())
    revision = controller.revisions.get(layer.id(), 0)
    layer.attributeAdded.emit(1)
    assert controller.revisions[layer.id()] == revision + 1
    document = controller.snapshot(layer)
    assert document["operations"] == []
    assert document["added_fields"] == [
        {
            "name": "Inspection note",
            "type": 10,
            "type_name": "Text",
            "sub_type": 0,
            "length": 80,
            "precision": 0,
            "comment": "Original local definition",
        }
    ]
    assert controller.store.load(document["id"])["added_fields"] == document["added_fields"]
    assert "Unpushed: 1" in layer.name()
    # Adding a column itself sends no feature request, so a local save cannot
    # have produced an ambiguous server mutation.
    assert controller.before_commit(layer)
    assert controller.documents[layer.id()]["state"] == "pending"


def test_partial_native_save_keeps_added_field_definition_until_feature_save_acknowledged(
    controller,
):
    layer = Layer()
    layer.providerType = lambda: pending.layers.CLASS_PROVIDER
    controller.watch_layers([layer])
    field = _new_field()
    layer.buffer.added_fields.append(field)
    layer.buffer.added[-1].attributes["Inspection note"] = "Not sent yet"
    original = controller.snapshot(layer)
    assert controller.before_commit(layer)
    # Native commit succeeds at the schema stage but fails while writing the
    # feature. The field now belongs to the provider, not the edit buffer.
    layer.buffer.added_fields.clear()
    layer.fields = lambda: [SimpleNamespace(name=lambda: "name"), field]
    recovered = controller.snapshot(layer)
    assert recovered["state"] == "uncertain"
    assert recovered["added_fields"] == original["added_fields"]
    assert recovered["operations"][0]["attributes"]["Inspection note"] == "Not sent yet"
    persisted = controller.store.load(recovered["id"])
    assert persisted["added_fields"] == original["added_fields"]
    # Repeated snapshots must not accumulate duplicate field definitions.
    assert controller.snapshot(layer)["added_fields"] == original["added_fields"]


@pytest.mark.parametrize("cancel", [False, True])
def test_undo_or_cancel_discards_only_unsubmitted_field_only_recovery(controller, cancel):
    layer = Layer(name=None)
    layer.providerType = lambda: pending.layers.CLASS_PROVIDER
    controller.watch_layers([layer])
    layer.buffer.added_fields.append(_new_field())
    controller.snapshot(layer)
    layer.buffer.added_fields.clear()
    if cancel:
        controller.rolled_back(layer)
    else:
        layer.attributeDeleted.emit(1)
        assert controller.snapshot(layer) is None
    assert controller.store.list() == []


def test_deleted_schema_and_legacy_added_schema_are_still_refused(controller):
    layer = Layer(name=None)
    layer.buffer.added_fields.append(_new_field())
    with pytest.raises(ValueError, match="Field/schema"):
        controller.snapshot(layer)
    layer.providerType = lambda: pending.layers.CLASS_PROVIDER
    layer.buffer.deleted_fields.append(0)
    with pytest.raises(ValueError, match="Field/schema"):
        controller.snapshot(layer)


def test_restore_adds_native_field_within_edit_command_before_feature_values(
    controller, monkeypatch
):
    layer = Layer(name=None)
    layer.providerType = lambda: pending.layers.CLASS_PROVIDER
    controller.watch_layers([layer])
    layer.buffer.added_fields.append(_new_field())
    document = controller.snapshot(layer)
    document["operations"] = [
        {"kind": "create", "attributes": {"Inspection note": "Needs review"}, "geometry": None}
    ]
    layer.buffer.added_fields.clear()
    events = []
    layer.dataProvider = lambda: SimpleNamespace(
        reloadData=lambda: None, field_definitions=lambda: [], last_refresh_error=""
    )
    layer.beginEditCommand = lambda _name: events.append("begin")
    layer.endEditCommand = lambda: events.append("end")
    layer.destroyEditCommand = lambda: events.append("destroy")
    layer.triggerRepaint = lambda: None

    def add_field(field):
        events.append("field")
        layer.buffer.added_fields.append(field)
        return True

    class RestoredFeature:
        def __init__(self, fields):
            assert "Inspection note" in [field.name() for field in fields]
            self.attributes = {}

        def setAttribute(self, name, value):  # noqa: N802
            self.attributes[name] = value

    restored = []
    layer.addAttribute = add_field
    layer.addFeature = lambda feature: restored.append(feature) or events.append("feature") or True
    monkeypatch.setattr(pending, "_decode_field", lambda _definition: _new_field())
    monkeypatch.setattr(pending, "QgsFeature", RestoredFeature)
    controller._restore(layer, document)
    assert events == ["begin", "field", "feature", "end"]
    assert restored[0].attributes == {"Inspection note": "Needs review"}


def test_restore_reuses_inferred_source_column_without_duplicate_field(controller, monkeypatch):
    layer = Layer(name=None)
    layer.providerType = lambda: pending.layers.CLASS_PROVIDER
    controller.watch_layers([layer])
    layer.buffer.added_fields.append(_new_field())
    document = controller.snapshot(layer)
    document["operations"] = [
        {"kind": "create", "attributes": {"Inspection note": "Kept"}, "geometry": None}
    ]
    layer.buffer.added_fields.clear()
    wire_name = "src_" + b"Inspection note".hex()
    layer.fields = lambda: [_new_field(wire_name)]
    layer.dataProvider = lambda: SimpleNamespace(
        reloadData=lambda: None,
        last_refresh_error="",
        field_definitions=lambda: [
            {"origin": "src", "source_name": "Inspection note", "name": wire_name}
        ],
    )
    layer.beginEditCommand = lambda _name: None
    layer.endEditCommand = lambda: None
    layer.destroyEditCommand = lambda: None
    layer.triggerRepaint = lambda: None
    layer.addAttribute = lambda _field: pytest.fail("Should reuse the existing source column")
    restored = []

    class RestoredFeature:
        def __init__(self, _fields):
            self.attributes = {}

        def setAttribute(self, name, value):  # noqa: N802
            self.attributes[name] = value

    layer.addFeature = lambda feature: restored.append(feature) or True
    monkeypatch.setattr(pending, "_decode_field", lambda _definition: _new_field())
    monkeypatch.setattr(pending, "QgsFeature", RestoredFeature)
    controller._restore(layer, document)
    assert restored[0].attributes == {wire_name: "Kept"}
