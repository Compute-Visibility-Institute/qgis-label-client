"""The five-reload test, made mechanical.

QGIS cleans up nothing. The canonical way to find a broken ``unload()`` is to reload five
times with Plugin Reloader and count toolbar buttons; five buttons means it is wrong. That
loop is reproduced here against a recording ``iface``, so the regression is caught in CI
rather than by someone noticing a duplicated panel.
"""

from __future__ import annotations

import pytest

from qgis_label_client.plugin import MENU_NAME, LabelClientPlugin


def _cycle(plugin: LabelClientPlugin) -> None:
    plugin.initGui()
    plugin.unload()


def test_one_cycle_attaches_and_detaches_everything(fake_iface):
    plugin = LabelClientPlugin(fake_iface)

    plugin.initGui()
    assert len(fake_iface.toolbar_icons) == 1
    assert len(fake_iface.docks) == 1
    # Four menu entries: the panel toggle, the imagery refresh, the transaction-time
    # historical view and the bootstrap publish.
    assert [menu for menu, _ in fake_iface.plugin_menu] == [MENU_NAME] * 4

    plugin.unload()
    assert fake_iface.toolbar_icons == []
    assert fake_iface.docks == []
    assert fake_iface.plugin_menu == []


def test_five_reloads_leave_nothing_behind(fake_iface):
    for _ in range(5):
        _cycle(LabelClientPlugin(fake_iface))

    assert fake_iface.toolbar_icons == []
    assert fake_iface.docks == []
    assert fake_iface.plugin_menu == []


def test_unload_is_idempotent(fake_iface):
    plugin = LabelClientPlugin(fake_iface)
    plugin.initGui()
    plugin.unload()
    plugin.unload()
    assert fake_iface.toolbar_icons == []


def test_every_attachment_registers_a_teardown(fake_iface):
    plugin = LabelClientPlugin(fake_iface)
    plugin.initGui()
    # expression function, dock, toolbar icon, four menu entries.
    assert plugin.teardown.labels == [
        "valid-time expression",
        "dock widget",
        "toolbar icon",
        "menu: panel",
        "menu: refresh imagery",
        "menu: historical view",
        "menu: publish local layers",
    ]
    plugin.unload()
    assert len(plugin.teardown) == 0


def test_in_flight_tasks_are_cancelled_on_unload(fake_iface):
    plugin = LabelClientPlugin(fake_iface)
    plugin.initGui()

    task = plugin.tasks.run("pretend work", lambda feedback: None)
    assert len(plugin.tasks) == 1

    plugin.unload()
    assert task.isCanceled()
    assert len(plugin.tasks) == 0


# --- history tracks ---------------------------------------------------------


def test_a_backend_without_the_tracks_endpoint_still_connects(monkeypatch):
    """404 means "this deployment has no tracks", not "the plugin is broken".

    Refusing to connect would make this plugin version unusable against a backend that
    simply has not had the migration applied yet -- over a feature it does not have. The
    honest state is an empty track list: reads work, and every write is refused.
    """
    from qgis_label_client import client
    from qgis_label_client import plugin as plugin_module
    from qgis_label_client.core.errors import BackendError

    def not_found(*_args, **_kwargs):
        raise BackendError("HTTP 404 from /v1/tracks", status=404)

    monkeypatch.setattr(client, "fetch_tracks", not_found)
    assert plugin_module._fetch_tracks_or_none("https://host", "v1/tracks", "", None) == []


def test_any_other_failure_of_the_track_list_is_not_swallowed(monkeypatch):
    # A track list the plugin could not read must never look like a deployment with no
    # tracks: the first silently disables writes, the second is a real outage.
    from qgis_label_client import client
    from qgis_label_client import plugin as plugin_module
    from qgis_label_client.core.errors import BackendError

    def broken(*_args, **_kwargs):
        raise BackendError("HTTP 503", status=503)

    monkeypatch.setattr(client, "fetch_tracks", broken)
    with pytest.raises(BackendError):
        plugin_module._fetch_tracks_or_none("https://host", "v1/tracks", "", None)


# --- transaction time --------------------------------------------------------
#
# The controller half of the historical view. What is worth testing here is not that the
# layer appears -- that needs a real provider -- but the two refusals that keep a wrong
# layer from appearing, both of which fail silently if they are missing.

from snapshot_fixtures import REGISTRY  # noqa: E402

from qgis_label_client import layers as layer_tools  # noqa: E402


class _Field:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _FakeLayer:
    def __init__(self, field_names, properties=None):
        self._fields = [_Field(n) for n in field_names]
        self.properties = dict(properties or {})

    def fields(self):
        return self._fields

    def name(self) -> str:
        return "fake"

    def customProperty(self, key, default=""):  # noqa: N802 - Qt naming
        return self.properties.get(key, default)


def _plugin(fake_iface) -> LabelClientPlugin:
    plugin = LabelClientPlugin(fake_iface)
    plugin.initGui()
    plugin.registry = REGISTRY
    return plugin


def test_a_transaction_time_collection_checked_in_the_list_is_refused(fake_iface):
    """The populated-and-wrong failure, closed at the point somebody would cause it.

    That collection's view resolves at now() when no instant reaches it. Checking it in the
    collection list would produce a layer that loads, is full of features, is titled after a
    past-belief view, and shows the present. Recognised by the echo column rather than by
    id, because collection ids are a deployment's choice.
    """
    plugin = _plugin(fake_iface)
    believed = _FakeLayer(["label_id", "class_id", "recorded_at", "superseded"])

    assert plugin._refuse_unpinned_historical(believed, "some_collection") is True
    # Remembered on the way past, so the panel's own control does not have to ask later.
    assert plugin.settings.get("recorded_collection") == "some_collection"
    assert any("some_collection" in text for _, text, _ in fake_iface.messages)
    plugin.unload()


def test_an_ordinary_collection_is_loaded_normally(fake_iface):
    plugin = _plugin(fake_iface)
    live = _FakeLayer(["label_id", "class_id", "valid_from", "valid_to"])
    assert plugin._refuse_unpinned_historical(live, "some_collection") is False
    assert plugin.settings.get("recorded_collection") == ""
    plugin.unload()


def test_a_remembered_mixed_collection_is_forgotten_rather_than_refused_for_ever(
    fake_iface, monkeypatch
):
    """The historical view asks which collection serves it ONCE, and remembers the answer.

    So a remembered collection that mixes geometry types -- which every deployment has,
    because the mixed views stay for the web viewer -- would refuse every historical view
    from then on, with no control anywhere to change the answer. Clearing it on that one
    failure makes the next attempt ask again, with the geometry-typed collections in the
    list it asks with. Any other failure leaves the setting alone: a timeout is not a
    reason to make somebody choose a collection again.
    """
    from qgis_label_client.core.errors import BackendError, MixedGeometryError

    plugin = _plugin(fake_iface)
    plugin.settings.set("recorded_collection", "mixes_everything")

    def refuse(*_args, **_kwargs):
        raise MixedGeometryError("mixes_everything serves points, lines and polygons")

    monkeypatch.setattr(layer_tools, "create_layer", refuse)
    plugin.open_recorded_view("2026-01-15T08:00:00Z")
    assert plugin.settings.get("recorded_collection") == ""
    assert any("mixes_everything" in text for _, text, _ in fake_iface.messages)

    # A different failure must not cost the analyst their answer.
    plugin.settings.set("recorded_collection", "believed")

    def outage(*_args, **_kwargs):
        raise BackendError("HTTP 503", status=503)

    monkeypatch.setattr(layer_tools, "create_layer", outage)
    plugin.open_recorded_view("2026-01-15T08:00:00Z")
    assert plugin.settings.get("recorded_collection") == "believed"
    plugin.unload()


def test_a_malformed_instant_never_reaches_the_wire(fake_iface):
    """Refused before a request is made, and before a collection is even chosen.

    An instant the database's echo cannot match produces an empty layer, which reads as a
    backend outage rather than as a typo.
    """
    plugin = _plugin(fake_iface)
    plugin.open_recorded_view("last tuesday")
    assert any("last tuesday" in text for _, text, _ in fake_iface.messages)
    plugin.unload()


def test_the_menu_entry_sends_people_to_the_panel_rather_than_a_second_picker(fake_iface):
    # Two pickers for one axis is two places for the remembered default to live and two
    # chances for them to disagree about what UTC means.
    plugin = _plugin(fake_iface)
    plugin.request_recorded_view()
    assert any("Historical view" in text for _, text, _ in fake_iface.messages)
    plugin.unload()


def test_the_historical_view_is_wired_to_the_panel(fake_iface):
    plugin = _plugin(fake_iface)
    assert plugin.dock is not None
    assert plugin.open_recorded_view in plugin.dock.recordedViewRequested.slots
    plugin.unload()


def test_a_project_full_of_historical_layers_still_finds_no_label_layer(monkeypatch, fake_iface):
    # The QA tools must never run over a belief the team has since revised.
    plugin = _plugin(fake_iface)
    believed = _FakeLayer(["label_id", "class_id", "recorded_at", "superseded"])
    monkeypatch.setattr(layer_tools, "plugin_layers", lambda project=None: [believed])
    assert layer_tools.find_label_layer(REGISTRY) is None
    plugin.unload()


def test_a_future_instant_is_refused_by_the_controller_not_only_by_the_picker(fake_iface):
    """The panel is a view; the controller must not depend on a widget constraint.

    The picker's ceiling is set at Connect, so a Connect that failed leaves it unset -- and
    a future instant resolves to the CURRENT belief set, which is a full layer under a
    caption asserting something nobody has ever believed.
    """
    from datetime import datetime, timedelta, timezone

    plugin = _plugin(fake_iface)
    ahead = datetime.now(timezone.utc) + timedelta(days=30)
    plugin.open_recorded_view(ahead.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert any("in the future" in text for _, text, _ in fake_iface.messages)
    plugin.unload()


# --- which collections hold the labels --------------------------------------
#
# Labels are stored one collection per geometry type. Which ones those are is read from
# /collections at runtime, exactly as the class vocabulary is, and the plugin's own
# remembered setting is a hint about WHICH group -- never a compiled-in id.


def _connected(fake_iface, *collection_ids):
    from qgis_label_client.core.collections import Collection

    plugin = _plugin(fake_iface)
    plugin.collections = [Collection(collection_id=c, title=c) for c in collection_ids]
    return plugin


def test_the_routes_come_from_the_collections_the_backend_listed(fake_iface):
    plugin = _connected(fake_iface, "label_polygon", "label_point", "labeled_extent")
    routes = plugin._label_routes()
    assert routes.collection_for("MultiPolygon") == "label_polygon"
    assert routes.collection_for("Point") == "label_point"
    plugin.unload()


def test_a_setting_stored_before_the_split_needs_no_migration(fake_iface):
    """The remembered collection is ``label``; the backend now serves ``label_*``.

    Matching by stem is what keeps the backend change from re-prompting every existing
    user at the exact moment they are about to publish irreversibly. If this breaks, the
    symptom is a dialog asking a question that was already answered.
    """
    plugin = _connected(fake_iface, "label_polygon", "label_point", "label_line")
    plugin.settings.set("label_collection", "label")
    routes = plugin._label_routes()
    assert routes.stem == "label"
    assert routes.collection_for("MultiLineString") == "label_line"
    plugin.unload()


def test_a_backend_this_cannot_read_falls_back_to_asking(monkeypatch, fake_iface):
    # Degrading honestly: the plugin asks which collection holds the labels and sends
    # everything there, which is exactly what it did before the split. Guessing instead
    # would put the founding dataset in another dataset's collections, permanently.
    plugin = _connected(fake_iface, "capture_point", "capture_polygon", "annotation_point")
    monkeypatch.setattr(plugin, "_ask_collection", lambda *args: "annotation_point")
    routes = plugin._label_routes()
    assert routes.collection_for("MultiPolygon") == "annotation_point"
    plugin.unload()


def test_a_backend_this_cannot_read_and_nobody_answers_for_routes_nothing(monkeypatch, fake_iface):
    plugin = _connected(fake_iface, "capture_point", "capture_polygon", "annotation_point")
    monkeypatch.setattr(plugin, "_ask_collection", lambda *args: "")
    assert not plugin._label_routes()
    plugin.unload()
