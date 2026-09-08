"""Permission hints must prevent refused edits without locking out unknown writers."""

import time

import pytest

from qgis_label_client import client, layers
from qgis_label_client.access import ACCESS_PROPERTY, READ_ONLY_REASON, LayerAccess
from qgis_label_client.core.errors import BackendError
from qgis_label_client.plugin import LabelClientPlugin


class Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def disconnect(self, callback):
        self.callbacks.remove(callback)

    def emit(self):
        for callback in list(self.callbacks):
            callback()


class Layer:
    def __init__(self, name="labels", readonly=False, historical=False, editing=False):
        self.title = name
        self.readonly = readonly
        self.description = "Original metadata"
        self.historical = historical
        self.editing = editing
        self.edits = ["unsaved geometry"] if editing else []
        self.editingStopped = Signal()
        self.properties = {}

    def id(self):
        return self.title

    def name(self):
        return self.title

    def customProperty(self, key, default=""):  # noqa: N802
        if key == layers.RECORDED_AT_PROPERTY and self.historical:
            return "2026-01-01T00:00:00Z"
        return self.properties.get(key, default)

    def setCustomProperty(self, key, value):  # noqa: N802
        self.properties[key] = value

    def removeCustomProperty(self, key):  # noqa: N802
        self.properties.pop(key, None)

    def isEditable(self):  # noqa: N802
        return self.editing

    def readOnly(self):  # noqa: N802
        return self.readonly

    def setReadOnly(self, value):  # noqa: N802
        assert not self.editing, "must never touch a native edit buffer"
        self.readonly = value

    def abstract(self):
        return self.description

    def setAbstract(self, value):  # noqa: N802
        self.description = value


@pytest.mark.parametrize("next_access", [True, None])
def test_only_plugin_restrictions_are_removed_when_access_changes(next_access):
    reader = Layer()
    user_readonly = Layer("user", readonly=True)
    historical = Layer("past", readonly=True, historical=True)
    targets = [reader, user_readonly, historical]
    access = LayerAccess()
    access.apply(targets, False)
    assert reader.readonly and reader.description == READ_ONLY_REASON
    access.apply(targets, next_access)
    assert not reader.readonly and reader.description == "Original metadata"
    assert user_readonly.readonly and historical.readonly


def test_native_edit_buffers_are_preserved_for_newly_detected_reader():
    layer = Layer(editing=True)
    assert LayerAccess().apply([layer], False) == ["labels"]
    assert layer.edits == ["unsaved geometry"] and layer.editing


def test_repointed_readonly_layer_is_restricted_again():
    layer = Layer()
    access = LayerAccess()
    access.apply([layer], False)
    layer.readonly = False  # QGIS setDataSource resets the layer flag.
    access.apply([layer], False)
    assert layer.readonly
    access.apply([layer], None)
    assert layer.description == "Original metadata"


def test_saved_reader_layer_can_be_reopened_by_a_writer_or_unknown_session():
    for writable in (True, None):
        layer = Layer()
        LayerAccess().apply([layer], False)
        assert ACCESS_PROPERTY in layer.properties
        LayerAccess().apply([layer], writable)
        assert not layer.readonly
        assert layer.description == "Original metadata"
        assert ACCESS_PROPERTY not in layer.properties


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"scopes": ["labels:read"]}, False),
        ({"scopes": ["labels:read", "labels:write"]}, True),
        ({"scopes": []}, False),
        ({}, None),
        ({"scopes": "labels:read"}, None),
        ({"scopes": [None]}, None),
        ([], None),
    ],
)
def test_whoami_requires_a_valid_scope_list(monkeypatch, payload, expected):
    requests = []

    def fetch(url, **kwargs):
        requests.append((url, kwargs))
        return payload

    monkeypatch.setattr(client, "request_json", fetch)
    assert client.fetch_write_access("https://api.example.org", "config") is expected
    assert requests[0][0] == "https://api.example.org/v1/whoami"
    assert requests[0][1]["authcfg"] == "config"


def test_failed_whoami_is_unknown(monkeypatch):
    def fail(*args):
        raise BackendError("unavailable", status=503)

    monkeypatch.setattr(client, "fetch_write_access", fail)
    assert LabelClientPlugin._fetch_access("https://api.example.org", "", None) is None


def test_stale_identity_or_backend_cannot_keep_a_writer_readonly(fake_iface, monkeypatch):
    plugin = LabelClientPlugin(fake_iface)
    plugin.initGui()
    layer = Layer()
    monkeypatch.setattr(layers, "plugin_layers", lambda: [layer])
    plugin._write_access = False
    plugin._access_context = plugin._permission_context()
    assert plugin._apply_access() is False
    assert layer.readonly
    plugin.settings.set("api_base_url", "https://other.example.org")
    assert plugin._apply_access() is None
    assert not layer.readonly
    plugin.unload()


def test_expired_scope_snapshot_is_unknown(fake_iface):
    plugin = LabelClientPlugin(fake_iface)
    plugin.settings.set_oauth_session("reader@example.org", int(time.time()) - 1)
    plugin._write_access = False
    plugin._access_context = plugin._permission_context()
    assert plugin._current_write_access() is None


def test_confirmed_reader_cannot_open_publish_preview(fake_iface):
    plugin = LabelClientPlugin(fake_iface)
    plugin.initGui()
    plugin.dock.api_url = lambda: plugin.settings.api_base_url
    plugin._write_access = False
    plugin._access_context = plugin._permission_context()
    plugin.publish_local_layers()
    assert any("read-only access" in text for _, text, _ in fake_iface.messages)
    assert not plugin.publishing
    plugin.unload()


def test_reader_becomes_readonly_when_native_editing_stops(fake_iface, monkeypatch):
    plugin = LabelClientPlugin(fake_iface)
    plugin.initGui()
    layer = Layer(editing=True)
    monkeypatch.setattr(layers, "plugin_layers", lambda: [layer])
    plugin._write_access = False
    plugin._access_context = plugin._permission_context()
    plugin._apply_access()
    assert not layer.readonly and layer.edits
    layer.editing = False
    layer.editingStopped.emit()
    assert layer.readonly
    plugin.unload()
    assert layer.editingStopped.callbacks == []


def test_startup_and_project_read_check_a_valid_stored_credential(fake_iface):
    plugin = LabelClientPlugin(fake_iface)
    plugin.settings.set_authcfg_by_track({"": "stored"})
    plugin.settings.set_oauth_session("reader@example.org", int(time.time()) + 3600)
    plugin.initGui()

    def checks():
        return [t for t in plugin.tasks._tasks if t.description() == "Check account access"]

    assert len(checks()) == 1
    plugin._on_project_read()
    assert len(checks()) == 2
    plugin.unload()
