"""Track changes must carry matching registry styles without discarding edits."""

from types import SimpleNamespace

import pytest

from qgis_label_client import client, layers
from qgis_label_client.core.errors import BackendError
from qgis_label_client.core.registry import parse_registry
from qgis_label_client.core.tracks import Track
from qgis_label_client.plugin import LabelClientPlugin


def registry(fill):
    return parse_registry({"classes": [{"class_id": "building", "style": {"fill": fill}}]})


@pytest.fixture
def plugin(fake_iface, monkeypatch):
    plugin = LabelClientPlugin(fake_iface)
    plugin.initGui()
    plugin.settings.set("track", "old")
    plugin.tracks = [Track("old"), Track("new"), Track("third")]
    plugin.registry = registry("#ff0000")
    monkeypatch.setattr(layers, "plugin_layers", lambda *_: [])
    monkeypatch.setattr(layers, "dirty_layers", lambda: [])
    monkeypatch.setattr(plugin.dock, "set_tracks", lambda *_: None)
    monkeypatch.setattr(plugin.dock, "set_registry", lambda *_: None)
    monkeypatch.setattr(plugin, "_refresh_access", lambda: None)
    yield plugin
    plugin.unload()


def test_switch_fetches_new_registry_before_committing_track(plugin, monkeypatch):
    observed = []
    fresh = registry("#00ff00")

    def fetch(*_args, **kwargs):
        observed.append(kwargs["track"])
        return fresh

    monkeypatch.setattr(client, "fetch_registry", fetch)
    plugin.set_track("new")
    assert plugin.settings.track == "old"
    assert plugin.registry.classes[0].style == {"fill": "#ff0000"}
    assert plugin._registry_pending
    assert plugin._require_track("publish") is None
    task = plugin.tasks._tasks[-1]
    task.finished(task.run())
    assert observed == ["new"]
    assert plugin.settings.track == "new"
    assert plugin.registry is fresh
    assert not plugin._registry_pending


def test_edit_started_during_style_fetch_keeps_original_view(plugin, monkeypatch):
    original = plugin.registry
    plugin.set_track("new")
    task = plugin.tasks._tasks[-1]
    monkeypatch.setattr(layers, "dirty_layers", lambda: [SimpleNamespace(name=lambda: "Edited")])
    task._on_success(registry("#00ff00"))
    assert plugin.settings.track == "old"
    assert plugin.registry is original
    assert not plugin._registry_pending


def test_late_style_reply_cannot_override_the_latest_selection(plugin):
    plugin.set_track("new")
    first = plugin.tasks._tasks[-1]
    plugin.set_track("third")
    latest = plugin.tasks._tasks[-1]
    first._on_success(registry("#00ff00"))
    assert plugin.settings.track == "old"
    assert plugin._registry_pending
    fresh = registry("#0000ff")
    latest._on_success(fresh)
    assert plugin.settings.track == "third"
    assert plugin.registry is fresh


def test_selecting_current_track_cancels_pending_style_switch(plugin):
    original = plugin.registry
    plugin.set_track("new")
    task = plugin.tasks._tasks[-1]
    plugin.set_track("old")
    task._on_success(registry("#00ff00"))
    assert plugin.settings.track == "old"
    assert plugin.registry is original
    assert not plugin._registry_pending


def test_failed_style_fetch_keeps_current_registry_and_track(plugin):
    original = plugin.registry
    plugin.set_track("new")
    task = plugin.tasks._tasks[-1]
    task._on_error("Could not load styles")
    assert plugin.settings.track == "old"
    assert plugin.registry is original
    assert not plugin._registry_pending


def test_session_change_discards_pending_track_registry(plugin):
    plugin.set_track("new")
    task = plugin.tasks._tasks[-1]
    plugin._advance_session()
    task._on_success(registry("#00ff00"))
    assert plugin.settings.track == "old"
    assert plugin.registry is None


def test_reconnect_resolves_saved_track_after_discovery(plugin, monkeypatch):
    # A fresh plugin has a saved nondefault track but no discovery result yet.
    plugin.tracks = []
    plugin.settings.set("track", "new")
    observed = []
    monkeypatch.setattr(
        client, "fetch_tracks", lambda *_: [Track("old", is_default=True), Track("new")]
    )
    monkeypatch.setattr(client, "fetch_capabilities", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(client, "fetch_collections", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(client, "fetch_class_layers", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        client,
        "fetch_registry",
        lambda *_args, **kwargs: observed.append(kwargs["track"]) or registry("#00ff00"),
    )
    monkeypatch.setattr(plugin, "_fetch_access", lambda *_args: True)
    plugin.connect_backend()
    task = plugin.tasks._tasks[-1]
    assert task.run()
    assert observed == ["new"]


def test_dev_connection_uses_neutral_auth_and_advertised_track_for_metadata(plugin, monkeypatch):
    plugin.settings.set("track", "default")
    plugin.settings.set_authcfg_by_track({"": "neutral", "default": "production-auth"})
    observed = []

    def tracks(_url, _path, authcfg, _feedback):
        observed.append(("tracks", authcfg, ""))
        return [Track("dev", is_default=True)]

    def capabilities(_url, _path, authcfg, _feedback, *, track):
        observed.append(("capabilities", authcfg, track))
        return {}

    def collections(_url, authcfg, _feedback, *, track):
        observed.append(("collections", authcfg, track))
        return []

    def class_layers(_url, authcfg, _feedback, *, track):
        observed.append(("class_layers", authcfg, track))
        return []

    def classes(_url, _path, authcfg, _feedback, *, track):
        observed.append(("registry", authcfg, track))
        return registry("#00ff00")

    monkeypatch.setattr(client, "fetch_tracks", tracks)
    monkeypatch.setattr(client, "fetch_capabilities", capabilities)
    monkeypatch.setattr(client, "fetch_collections", collections)
    monkeypatch.setattr(client, "fetch_class_layers", class_layers)
    monkeypatch.setattr(client, "fetch_registry", classes)
    monkeypatch.setattr(plugin, "_fetch_access", lambda *_args: True)

    plugin.connect_backend()
    task = plugin.tasks._tasks[-1]
    assert task.run()
    assert observed == [
        ("tracks", "neutral", ""),
        ("capabilities", "neutral", "dev"),
        ("collections", "neutral", "dev"),
        ("class_layers", "neutral", "dev"),
        ("registry", "neutral", "dev"),
    ]
    assert task._result["registry_track"] == "dev"
    assert plugin.settings.track == "default"


def test_failed_metadata_keeps_discovered_environments_for_recovery(plugin, monkeypatch):
    plugin.settings.set("track", "default")
    offered = [Track("dev", is_default=True)]
    monkeypatch.setattr(client, "fetch_tracks", lambda *_: offered)
    monkeypatch.setattr(client, "fetch_capabilities", lambda *_args, **_kwargs: {})

    def unavailable(*_args, **_kwargs):
        raise BackendError("HTTP 503 from /collections", status=503)

    monkeypatch.setattr(client, "fetch_collections", unavailable)
    plugin.connect_backend()
    task = plugin.tasks._tasks[-1]
    assert not task.run()
    task.finished(False)

    assert plugin.tracks == offered
    assert plugin.registry is None
    assert not plugin._registry_pending
    assert not plugin.dock._connected
    assert plugin.settings.track == "default"


def test_selecting_environment_after_failed_connection_reconnects_without_repointing(
    plugin, monkeypatch
):
    plugin.registry = None
    plugin.settings.set("track", "default")
    plugin.tracks = [Track("dev", is_default=True)]
    connected = []
    monkeypatch.setattr(plugin, "connect_backend", lambda: connected.append(plugin.settings.track))
    monkeypatch.setattr(
        layers, "repoint_for", lambda *_args, **_kwargs: pytest.fail("must not repoint layers")
    )

    plugin.set_track("dev")

    assert plugin.settings.track == "dev"
    assert connected == ["dev"]
    assert plugin.registry is None


def test_failed_connection_recovery_preserves_unsaved_edits(plugin, monkeypatch):
    plugin.registry = None
    plugin.settings.set("track", "default")
    plugin.tracks = [Track("dev", is_default=True)]
    monkeypatch.setattr(layers, "dirty_layers", lambda: [SimpleNamespace(name=lambda: "Edited")])
    monkeypatch.setattr(plugin, "connect_backend", lambda: pytest.fail("must preserve edit context"))

    plugin.set_track("dev")

    assert plugin.settings.track == "default"


@pytest.mark.parametrize(
    "current,expected", [("server-fingerprint", True), ("custom-symbols", False)]
)
def test_only_unchanged_generated_renderers_receive_track_styles(monkeypatch, current, expected):
    class Layer:
        def customProperty(self, key, default=""):  # noqa: N802
            return "server-fingerprint" if key == layers.GENERATED_RENDERER_PROPERTY else default

        def fields(self):
            return [SimpleNamespace(name=lambda: "class_id")]

    layer = Layer()
    calls = []
    fresh = registry("#00ff00")
    monkeypatch.setattr(layers, "renderer_fingerprint", lambda _: current)
    monkeypatch.setattr(layers, "_apply_class_renderer", lambda *args: calls.append(args))
    assert layers.refresh_generated_style(layer, fresh) is expected
    assert calls == ([(layer, fresh, False)] if expected else [])


def test_legacy_unmarked_renderer_is_preserved(monkeypatch):
    layer = SimpleNamespace(customProperty=lambda *_: "")
    monkeypatch.setattr(
        layers, "_apply_class_renderer", lambda *_: pytest.fail("local styling must remain")
    )
    assert not layers.refresh_generated_style(layer, registry("#00ff00"))


@pytest.mark.parametrize("track,backend", [("new", ""), ("old", "https://other.example.org")])
def test_late_publish_style_does_not_change_another_track_or_backend(plugin, track, backend):
    from qgis_label_client.core.bootstrapstyles import StyleResult
    from qgis_label_client.core.publish import PublishReport

    old = plugin.registry
    plugin.dock = None
    report = PublishReport(
        style_results=[StyleResult("building", "initialized", {"fill": "#00ff00"})]
    )
    plugin._on_published([], "label_polygon", report, track, backend)
    assert plugin.registry is old
