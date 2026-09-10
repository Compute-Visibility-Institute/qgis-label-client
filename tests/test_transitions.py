"""Failure injection at provider boundaries, including partially mutated layers."""

from datetime import date

import pytest

from qgis_label_client import layers
from qgis_label_client.core.errors import BackendError
from qgis_label_client.core.tracks import Track
from qgis_label_client.plugin import LabelClientPlugin
from qgis_label_client.settings import PluginSettings
from qgis_label_client.transitions import PendingSettings, TransitionError, transition


class Layer:
    def __init__(self, name):
        self.title = name
        self.uri = f"old-{name}"
        self.properties = {layers.TRACK_PROPERTY: "old"}
        self.read_only = False
        self.description = "Original description"
        self.style = "Original style"
        self.invalid_sources = set()
        self.dirty = False
        self.restores = []

    def name(self):
        return self.title

    def source(self):
        return self.uri

    def providerType(self):  # noqa: N802
        return "OAPIF"

    def isModified(self):  # noqa: N802
        return self.dirty

    def isValid(self):  # noqa: N802
        return bool(self.uri) and self.uri not in self.invalid_sources

    def readOnly(self):  # noqa: N802
        return self.read_only

    def setReadOnly(self, value):  # noqa: N802
        self.read_only = value

    def abstract(self):
        return self.description

    def setAbstract(self, value):  # noqa: N802
        self.description = value

    def customProperty(self, key, default=None):  # noqa: N802
        return self.properties.get(key, default)

    def setCustomProperty(self, key, value):  # noqa: N802
        self.properties[key] = value

    def removeCustomProperty(self, key):  # noqa: N802
        self.properties.pop(key, None)

    def exportNamedStyle(self, document):  # noqa: N802
        document.saved_style = self.style

    def importNamedStyle(self, document):  # noqa: N802
        self.style = document.saved_style
        return True, ""

    def setDataSource(self, uri, *_args):  # noqa: N802
        self.uri = uri
        self.style = "provider default"
        self.read_only = False
        self.restores.append(uri)

    def triggerRepaint(self):  # noqa: N802
        pass


@pytest.fixture
def setup(monkeypatch):
    settings = PluginSettings()
    settings.set("track", "old")
    targets = [Layer(name) for name in ("one", "two", "three")]
    monkeypatch.setattr(layers, "validate_repoint", lambda *_args: None)
    return settings, targets


def test_middle_failure_rolls_back_including_the_failing_layer_and_allows_retry(setup, monkeypatch):
    settings, targets = setup
    fail = True
    observed_settings = []

    def repoint(layer, pending, *_args):
        observed_settings.append(settings.track)
        assert pending.track == "new"
        layer.uri = "new-" + layer.name()
        layer.properties[layers.TRACK_PROPERTY] = "new"
        layer.style = "changed"
        if fail and layer is targets[1]:
            raise BackendError("failed after replacing the provider")

    monkeypatch.setattr(layers, "repoint_for", repoint)
    with pytest.raises(TransitionError, match="previous view was retained"):
        transition(targets, settings, {"track": "new"}, None, Track("new"))
    assert settings.track == "old"
    assert [layer.uri for layer in targets] == ["old-one", "old-two", "old-three"]
    assert all(layer.style == "Original style" for layer in targets)
    assert all(layer.properties[layers.TRACK_PROPERTY] == "old" for layer in targets)
    assert targets[2].restores == []
    fail = False
    assert transition(targets, settings, {"track": "new"}, None, Track("new")) == 3
    assert settings.track == "new"
    assert set(observed_settings) == {"old"}


def test_candidate_failure_changes_no_layer_or_setting(setup, monkeypatch):
    settings, targets = setup

    def validate(layer, *_args):
        if layer is targets[1]:
            raise BackendError("invalid provider")

    monkeypatch.setattr(layers, "validate_repoint", validate)
    monkeypatch.setattr(layers, "repoint_for", lambda *_args: pytest.fail("must not mutate"))
    with pytest.raises(TransitionError):
        transition(targets, settings, {"track": "new"}, None, Track("new"))
    assert settings.track == "old"
    assert all(not layer.restores for layer in targets)


def test_restore_failure_disables_uncertain_layer_and_restores_others(setup, monkeypatch):
    settings, targets = setup
    targets[1].invalid_sources.add("old-two")

    def repoint(layer, *_args):
        layer.uri = "new-" + layer.name()
        if layer is targets[1]:
            raise BackendError("broken provider")

    monkeypatch.setattr(layers, "repoint_for", repoint)
    with pytest.raises(TransitionError, match=r"disabled.*two"):
        transition(targets, settings, {"track": "new"}, None, Track("new"))
    assert targets[0].uri == "old-one"
    assert targets[1].uri == ""
    assert targets[1].read_only
    assert "Remove and reload" in targets[1].description
    assert targets[2].uri == "old-three"
    assert settings.track == "old"


def test_dirty_layer_refused_before_provider_validation(setup, monkeypatch):
    settings, targets = setup
    targets[1].dirty = True
    monkeypatch.setattr(layers, "validate_repoint", lambda *_args: pytest.fail("must not connect"))
    with pytest.raises(TransitionError, match="unsaved edits"):
        transition(targets, settings, {"track": "new"}, None, Track("new"))
    assert settings.track == "old"


def test_pending_valid_time_uses_proposed_values_without_persisting():
    settings = PluginSettings()
    settings.set_as_of(date(2026, 1, 1))
    proposed = PendingSettings(settings, {"as_of_date": "2026-02-01"})
    assert proposed.as_of == date(2026, 2, 1)
    assert settings.as_of == date(2026, 1, 1)


def test_provider_invalid_after_swap_raises(monkeypatch):
    layer = Layer("one")
    layer.invalid_sources.add("new")
    with pytest.raises(BackendError, match="new provider"):
        layers.repoint_layer(layer, "new")


def test_candidate_validation_checks_historical_echo(monkeypatch):
    layer = Layer("historical")
    layer.properties[layers.RECORDED_AT_PROPERTY] = "2026-01-01T00:00:00Z"
    candidate = Layer("candidate")
    monkeypatch.setattr(layers, "build_layer_uri", lambda *_args, **_kw: "proposed")
    monkeypatch.setattr(layers, "track_filter_for", lambda *_args: None)
    monkeypatch.setattr(layers, "QgsVectorLayer", lambda *_args: candidate)
    observed = []
    monkeypatch.setattr(layers, "verify_recorded_echo", lambda *args: observed.append(args))
    layers.validate_repoint(layer, PluginSettings(), None, Track("new"))
    assert observed == [(candidate, "2026-01-01T00:00:00Z", None)]
    candidate.uri = ""
    with pytest.raises(BackendError, match="proposed view"):
        layers.validate_repoint(layer, PluginSettings(), None, Track("new"))


@pytest.mark.parametrize("axis", ["track", "as_of"])
def test_controller_failure_keeps_settings_and_resets_controls(fake_iface, monkeypatch, axis):
    plugin = LabelClientPlugin(fake_iface)
    plugin.initGui()
    plugin.settings.set("track", "old")
    plugin.settings.set_as_of(date(2026, 1, 1))
    plugin.tracks = [Track("old"), Track("new")]
    monkeypatch.setattr(layers, "dirty_layers", lambda: [])
    monkeypatch.setattr(layers, "plugin_layers", lambda: [])

    def failed(*_args):
        raise TransitionError("provider failed")

    monkeypatch.setattr("qgis_label_client.plugin.transition", failed)
    restored = []
    plugin.dock.set_tracks = lambda _tracks, name: restored.append(name)
    plugin.dock.set_as_of = restored.append
    plugin.dock.as_of = lambda: date(2026, 2, 1)
    if axis == "track":
        plugin.set_track("new")
        assert restored == ["old"]
        plugin.tasks._tasks[-1]._on_success(None)
        assert restored == ["old", "old"]
    else:
        plugin.apply_as_of()
        assert restored == [date(2026, 1, 1)]
    assert plugin.settings.track == "old"
    assert plugin.settings.as_of == date(2026, 1, 1)
    assert any("provider failed" in text for _, text, _ in fake_iface.messages)
    plugin.unload()


def test_failed_disable_still_restores_all_other_layers_and_settings(setup, monkeypatch):
    settings, targets = setup
    original_set = settings.set
    fail_save = True

    def save(key, value):
        original_set(key, value)
        if fail_save and key == "track" and value == "new":
            raise RuntimeError("failure after persisting setting")

    monkeypatch.setattr(settings, "set", save)
    monkeypatch.setattr(layers, "repoint_for", lambda layer, *_args: setattr(layer, "uri", "new"))

    def refuse(*_args):
        raise RuntimeError("restore and disable both fail")

    monkeypatch.setattr(targets[1], "setDataSource", refuse)
    with pytest.raises(TransitionError, match="could not be safely disabled: two"):
        transition(targets, settings, {"track": "new"}, None, Track("new"))
    assert settings.track == "old"
    assert targets[0].uri == "old-one"
    assert targets[2].uri == "old-three"


def test_style_export_failure_refuses_before_any_provider_change(setup, monkeypatch):
    settings, targets = setup
    monkeypatch.setattr(targets[1], "exportNamedStyle", lambda _doc: "style could not be exported")
    monkeypatch.setattr(layers, "validate_repoint", lambda *_args: pytest.fail("must not connect"))
    with pytest.raises(TransitionError, match="previous view was retained"):
        transition(targets, settings, {"track": "new"}, None, Track("new"))
    assert all(not layer.restores for layer in targets)
    assert settings.track == "old"
    with pytest.raises(BackendError, match="save the style"):
        layers.repoint_layer(targets[1], "new")
    assert targets[1].uri == "old-two"
