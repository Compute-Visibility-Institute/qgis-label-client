"""Startup preferences and reconnect refreshes must preserve native edit buffers."""

from qgis_label_client import startup
from qgis_label_client.plugin import LabelClientPlugin


class Layer:
    def __init__(self, name, *, editing=False, modified=False, pending="", backend="current"):
        self.title = name
        self.editing = editing
        self.modified = modified
        self.pending = pending
        self.backend = backend
        self.reloads = 0
        self.paints = 0

    def name(self):
        return self.title

    def isEditable(self):  # noqa: N802
        return self.editing

    def isModified(self):  # noqa: N802
        return self.modified

    def customProperty(self, key, default):  # noqa: N802
        return self.pending if key == "cvi/pending_state" else default

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
    plugin.startup.set_enabled(False)
    plugin.startup.show_if_enabled()
    assert calls == ["show"]
    plugin.unload()
    reopened = LabelClientPlugin(fake_iface)
    assert reopened.settings.get("show_startup_connection") is False


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
