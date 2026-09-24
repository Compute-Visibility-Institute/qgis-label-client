"""Startup preferences and reconnect refreshes must preserve native edit buffers."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qgis_label_client import startup
from qgis_label_client.core.collections import Collection
from qgis_label_client.core.tracks import Track
from qgis_label_client.plugin import LabelClientPlugin


class Layer:
    def __init__(
        self,
        name,
        *,
        editing=False,
        modified=False,
        pending="",
        backend="current",
        track="default",
        collection="label_current_polygon",
        recorded_at="",
    ):
        self.title = name
        self.editing = editing
        self.modified = modified
        self.pending = pending
        self.backend = backend
        self.track = track
        self.collection = collection
        self.recorded_at = recorded_at
        self.reloads = 0
        self.paints = 0

    def name(self):
        return self.title

    def isEditable(self):  # noqa: N802
        return self.editing

    def isModified(self):  # noqa: N802
        return self.modified

    def customProperty(self, key, default):  # noqa: N802
        return {
            "cvi/pending_state": self.pending,
            startup.layers.TRACK_PROPERTY: self.track,
            startup.layers.COLLECTION_PROPERTY: self.collection,
            startup.layers.RECORDED_AT_PROPERTY: self.recorded_at,
        }.get(key, default)

    def reload(self):
        self.reloads += 1

    def dataProvider(self):  # noqa: N802
        return self

    def providerType(self):  # noqa: N802
        return "OAPIF"

    def reloadData(self):  # noqa: N802
        self.reloads += 1

    def updateExtents(self):  # noqa: N802
        pass

    def triggerRepaint(self):  # noqa: N802
        self.paints += 1


def test_reconnect_refreshes_only_clean_layers_on_the_connected_backend(monkeypatch):
    clean = Layer("clean")
    editing = Layer("editing", editing=True)
    modified = Layer("modified", modified=True)
    pending = Layer("pending", pending="uncertain")
    other = Layer("other backend", backend="other")
    targets = [clean, editing, modified, pending, other]
    monkeypatch.setattr(startup.layers, "live_layers", lambda: targets)
    monkeypatch.setattr(
        startup.layers, "belongs_to_backend", lambda layer, url: layer.backend == url
    )
    result = startup.refresh_connected_layers("current")
    assert result.refreshed == ["clean", "editing"]
    assert result.editing == ["modified", "pending"]
    assert result.failed == []
    assert clean.reloads == clean.paints == 1
    assert editing.reloads == editing.paints == 1 and editing.editing
    assert all(layer.reloads == 0 for layer in targets[2:])


def test_a_failed_provider_does_not_prevent_other_clean_layers_refreshing(monkeypatch):
    failed, clean = Layer("broken"), Layer("clean")

    def cannot_reload():
        raise RuntimeError("provider unavailable")

    failed.reload = cannot_reload
    monkeypatch.setattr(startup.layers, "live_layers", lambda: [failed, clean])
    monkeypatch.setattr(startup.layers, "belongs_to_backend", lambda *_: True)
    result = startup.refresh_connected_layers("current")
    assert result.refreshed == ["clean"]
    assert result.failed == ["broken: provider unavailable"]


def test_startup_prompt_setting_persists_and_manual_prompt_remains_available(
    fake_iface, monkeypatch
):
    plugin = LabelClientPlugin(fake_iface)
    plugin.initGui()
    calls = []
    monkeypatch.setattr(plugin.startup, "show", lambda: calls.append("show"))
    plugin.startup.show_if_enabled()
    assert calls == ["show"]
    plugin.dock.startupPromptChanged.emit(False)
    plugin.dock.unpushedWarningsChanged.emit(False)
    plugin.startup.show_if_enabled()
    assert calls == ["show"]
    plugin.unload()
    reopened = LabelClientPlugin(fake_iface)
    assert reopened.settings.get("show_startup_connection") is False
    assert reopened.settings.get("show_unpushed_warnings") is False


def test_connection_buttons_delegate_to_existing_plugin_actions(fake_iface, monkeypatch):
    plugin = LabelClientPlugin(fake_iface)
    plugin.initGui()
    calls = []
    monkeypatch.setattr(plugin, "sign_in", lambda: calls.append("login"))
    monkeypatch.setattr(plugin, "sign_out", lambda: calls.append("logout"))
    monkeypatch.setattr(plugin, "connect_backend", lambda: calls.append("connect"))
    plugin.startup.sign_in()
    plugin.startup.sign_out()
    plugin.startup.connect()
    assert calls == ["login", "logout", "connect"]
    plugin.unload()


def test_manual_pull_limits_refresh_to_selected_environment_current_labels(monkeypatch):
    clean = Layer("clean", track="dev")
    dirty = Layer("local edits", track="dev", modified=True)
    pending = Layer("recovery copy", track="dev", pending="uncertain")
    excluded = [
        Layer("production", track="default"),
        Layer("reference", track="dev", collection="label_class"),
        Layer("another backend", track="dev", backend="other"),
        Layer("past view", track="dev", recorded_at="2026-09-01T00:00:00Z"),
    ]
    monkeypatch.setattr(
        startup.layers, "plugin_layers", lambda project=None: [clean, dirty, pending, *excluded]
    )
    monkeypatch.setattr(
        startup.layers, "belongs_to_backend", lambda layer, url: layer.backend == url
    )
    result = startup.refresh_connected_layers(
        "current", track="dev", collection_ids={"label_current_polygon"}
    )
    assert result.refreshed == ["clean"]
    assert result.editing == ["local edits", "recovery copy"]
    assert result.failed == []
    assert clean.reloads == 1
    assert all(layer.reloads == 0 for layer in [dirty, pending, *excluded])


@pytest.mark.parametrize(
    "result,closed",
    [
        (startup.RefreshResult(refreshed=["labels"]), True),
        (startup.RefreshResult(editing=["local edits"]), True),
        (startup.RefreshResult(refreshed=["labels"], failed=["another: offline"]), False),
    ],
)
def test_connection_prompt_closes_after_success_but_stays_open_on_refresh_failure(
    monkeypatch, result, closed
):
    plugin = SimpleNamespace(settings=SimpleNamespace(api_base_url="current"), _message=Mock())
    connection = startup.StartupConnection(plugin)
    connection.dialog = Mock()
    connection.update_status = Mock()
    monkeypatch.setattr(startup, "refresh_connected_layers", lambda backend: result)
    assert connection.refresh_layers() is result
    assert connection.connected
    connection.update_status.assert_called_once_with()
    assert connection.dialog.accept.call_count == int(closed)
    assert plugin._message.call_count == int(bool(result.failed))
    if result.editing:
        assert "Kept local edits unchanged" in connection.connection_note


@pytest.mark.parametrize(
    "activity_count,resuming,allowed",
    [(0, False, True), (1, True, True), (1, False, False), (2, True, False)],
)
def test_pull_is_available_to_readers_and_never_invokes_connect_upload_path(
    fake_iface, monkeypatch, activity_count, resuming, allowed
):
    plugin = LabelClientPlugin(fake_iface)
    plugin.dock = Mock()
    plugin.registry = [object()]
    plugin.settings.set("track", "dev")
    plugin.settings.set_authcfg_by_track({"dev": "auth001"})
    plugin.tracks = [Track("dev")]
    plugin.collections = [
        Collection("label_polygon", "Editable"),
        Collection("label_current_polygon", "Current"),
        Collection("cl_alpha__point", "Class", class_layer={"class_id": "alpha"}),
        Collection("label_history", "History"),
        Collection("labeled_extent", "Coverage"),
        Collection("label_class", "Registry"),
    ]
    closed = Mock()
    plugin.activities = SimpleNamespace(
        state=SimpleNamespace(busy=activity_count > 0, count=activity_count),
        begin=lambda: SimpleNamespace(close=closed),
    )
    plugin._session = SimpleNamespace(resuming=resuming)
    plugin.connect_backend = Mock(side_effect=AssertionError("Pull must not connect/upload"))
    plugin.push_local = SimpleNamespace(push_all=Mock(), on_connected=Mock())
    plugin.pending = SimpleNamespace(on_connected=Mock())
    monkeypatch.setattr(plugin, "_current_write_access", lambda: False)
    monkeypatch.setattr(plugin, "_defer_until_fresh", lambda callback: False)
    refreshed_registry = [object(), object()]
    refreshed_collections = [
        Collection("cl_new__line", "New line", class_layer={"class_id": "new"})
    ]
    fetch_collections = Mock(return_value=[])
    fetch_class_layers = Mock(return_value=refreshed_collections)
    fetch_registry = Mock(return_value=refreshed_registry)
    monkeypatch.setattr("qgis_label_client.plugin.client.fetch_collections", fetch_collections)
    monkeypatch.setattr("qgis_label_client.plugin.client.fetch_class_layers", fetch_class_layers)
    monkeypatch.setattr("qgis_label_client.plugin.client.fetch_registry", fetch_registry)
    apply_catalog = Mock()
    monkeypatch.setattr(plugin, "_apply_current_catalog", apply_catalog)
    monkeypatch.setattr(
        plugin, "_run_read_task", lambda description, work, ready, failed: ready(work(None))
    )
    refresh = Mock(return_value=startup.RefreshResult(refreshed=["labels"]))
    monkeypatch.setattr("qgis_label_client.plugin.refresh_connected_layers", refresh)
    plugin.pull_all_remote()
    if allowed:
        fetch_collections.assert_called_once()
        fetch_class_layers.assert_called_once()
        fetch_registry.assert_called_once()
        assert plugin.collections == refreshed_collections
        assert plugin.registry is refreshed_registry
        apply_catalog.assert_called_once_with(plugin.settings.authcfg_by_track, "dev")
        refresh.assert_called_once_with(
            plugin.settings.api_base_url,
            track="dev",
            collection_ids={"cl_new__line"},
        )
        closed.assert_called_once_with()
    else:
        fetch_collections.assert_not_called()
        apply_catalog.assert_not_called()
        refresh.assert_not_called()
        closed.assert_not_called()
    plugin.connect_backend.assert_not_called()
    plugin.push_local.push_all.assert_not_called()
    plugin.push_local.on_connected.assert_not_called()
    plugin.pending.on_connected.assert_not_called()


def test_panel_push_and_pull_signals_delegate_to_their_distinct_actions(fake_iface, monkeypatch):
    plugin = LabelClientPlugin(fake_iface)
    push, pull = Mock(), Mock()
    monkeypatch.setattr(plugin, "push_all_local", push)
    monkeypatch.setattr(plugin, "pull_all_remote", pull)
    plugin.initGui()
    try:
        plugin.dock.pushAllLocalRequested.emit()
        push.assert_called_once_with()
        pull.assert_not_called()
        plugin.dock.pullAllRemoteRequested.emit()
        pull.assert_called_once_with()
        push.assert_called_once_with()
    finally:
        plugin.unload()
