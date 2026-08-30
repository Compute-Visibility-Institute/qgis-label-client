"""Finding the plugin's own layers again, which is harder than it sounds.

The QA tools do not look up layers by collection name -- a deployment names its
collections whatever it likes -- so they look them up by the fields they expose. That
works only if the field sets actually discriminate, and one pair does not: the audit
collection carries the same ``label_id`` and ``class_id`` as the label collection.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from qgis_label_client import layers as layer_tools
from qgis_label_client.core.errors import BackendError
from qgis_label_client.core.registry import parse_registry

REGISTRY = parse_registry({"classes": [{"class_id": "alpha", "label_en": "Alpha"}]})


class _Field:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _Fields(list):
    """A field list that can also answer indexOf, which QgsFields does."""

    def indexOf(self, name):  # noqa: N802 - Qt naming
        for i, field in enumerate(self):
            if field.name() == name:
                return i
        return -1


class _Feature:
    """One row, addressed by field index -- how verify_recorded_echo reads a value."""

    def __init__(self, values: list) -> None:
        self._values = values

    def attribute(self, index):
        return self._values[index]


class _FakeLayer:
    """Only what the layer helpers touch: a name, a field list, and custom properties."""

    def __init__(self, name: str, field_names: list[str]) -> None:
        self._name = name
        self._fields = _Fields(_Field(n) for n in field_names)
        self.rows: list[_Feature] = []
        self.properties: dict[str, str] = {}
        self.editable = False
        self.modified = False
        self.read_only = False
        self.abstract = ""

    def name(self) -> str:
        return self._name

    def fields(self) -> _Fields:
        return self._fields

    def getFeatures(self):  # noqa: N802 - Qt naming
        return list(self.rows)

    def customProperty(self, key, default=""):  # noqa: N802 - Qt naming
        return self.properties.get(key, default)

    def setCustomProperty(self, key, value):  # noqa: N802
        self.properties[key] = value

    def isEditable(self) -> bool:  # noqa: N802
        return self.editable

    def isModified(self) -> bool:  # noqa: N802
        return self.modified

    def setReadOnly(self, value: bool) -> None:  # noqa: N802
        self.read_only = value

    def setAbstract(self, text: str) -> None:  # noqa: N802
        self.abstract = text


LABEL_FIELDS = ["label_id", "class_id", "names", "attrs", "valid_from", "valid_to"]
AUDIT_FIELDS = ["history_id", "label_id", "operation", "changed", "actor", "class_id"]
EXTENT_FIELDS = ["extent_id", "class_id", "completeness", "caveat"]


def _with_layers(monkeypatch, *layers):
    monkeypatch.setattr(layer_tools, "plugin_layers", lambda project=None: list(layers))


def test_the_audit_layer_is_not_mistaken_for_the_label_layer(monkeypatch):
    # Regression. label_history is keyed on the same label_id and carries the class_id of
    # each superseded state, so identity-and-class does not tell the two apart. Loading
    # both collections and picking the wrong one runs the coverage check over every
    # historical revision -- counting a label once per edit and classifying geometry that
    # is no longer on the map. mapLayers() order is not something the plugin controls, so
    # this has to be decided by the fields, not by luck.
    audit = _FakeLayer("audit", AUDIT_FIELDS)
    label = _FakeLayer("label", LABEL_FIELDS)
    _with_layers(monkeypatch, audit, label)
    assert layer_tools.find_label_layer(REGISTRY) is label

    # ...and in the other order, because that is exactly what varies.
    _with_layers(monkeypatch, label, audit)
    assert layer_tools.find_label_layer(REGISTRY) is label


def test_the_extent_layer_is_found_by_its_completeness_column(monkeypatch):
    extent = _FakeLayer("extent", EXTENT_FIELDS)
    _with_layers(monkeypatch, _FakeLayer("label", LABEL_FIELDS), extent)
    assert layer_tools.find_extent_layer(REGISTRY) is extent


def test_no_label_layer_loaded_is_none_rather_than_a_wrong_guess(monkeypatch):
    _with_layers(monkeypatch, _FakeLayer("audit", AUDIT_FIELDS))
    assert layer_tools.find_label_layer(REGISTRY) is None


# --- history tracks ---------------------------------------------------------
#
# What matters here is not isolation -- that is row-level security in the database, and
# nothing in this module implements it. What matters is that the track reaches the
# database at all, on requests the plugin never sees: QGIS's native provider makes the
# item reads and the Part 4 writes itself, so the track has to be in the layer's own data
# source or it is not sent.

from snapshot_fixtures import OTHER_TRACK, TRACK  # noqa: E402

from qgis_label_client.core.tracks import Track  # noqa: E402
from qgis_label_client.settings import PluginSettings  # noqa: E402

TRACKED_FIELDS = [*LABEL_FIELDS, "track_id"]


def _settings(**values) -> PluginSettings:
    settings = PluginSettings()
    settings.set("api_base_url", "https://api.example.org/oapif")
    for key, value in values.items():
        settings.set(key, value)
    return settings


def test_the_track_rides_on_every_provider_request_as_a_header():
    uri = layer_tools.build_layer_uri(_settings(), "label", REGISTRY, TRACK)
    assert f"http-header:X-Track='{TRACK.name}'" in uri


def test_the_landing_url_names_the_track_too_but_is_not_relied_on():
    # The weakest of the three routes: the provider builds item requests from the links
    # the server returns, so a landing-page query parameter can be dropped entirely.
    assert f"track={TRACK.name}" in layer_tools.landing_url(_settings(), TRACK)
    assert "track=" not in layer_tools.landing_url(_settings(), None)


def test_a_layer_uses_the_credential_stored_for_its_own_track():
    settings = _settings()
    settings.set_authcfg_by_track({"": "default1", TRACK.name: "tracked"})
    assert "authcfg='tracked'" in layer_tools.build_layer_uri(settings, "label", REGISTRY, TRACK)


def test_a_track_with_no_credential_of_its_own_falls_back_to_the_untracked_one():
    """The ordering problem, made harmless.

    Signing in happens BEFORE Connect -- you need a credential to discover what tracks
    exist -- so the common case is one credential stored under "" and used by every track.
    The track itself travels in the header, which does not depend on this.
    """
    settings = _settings()
    settings.set_authcfg_by_track({"": "default1"})
    uri = layer_tools.build_layer_uri(settings, "label", REGISTRY, TRACK)
    assert "authcfg='default1'" in uri
    assert f"http-header:X-Track='{TRACK.name}'" in uri


def test_the_canary_is_only_applied_to_layers_that_carry_a_track_id():
    """Filtering on a column a layer does not have makes the LAYER invalid.

    Several collections are shared between tracks by design -- the class registry
    describes both datasets, one GeoTIFF serves both -- so they have no track_id, and a
    filter on it would leave the annotator with no layer rather than an unfiltered one.
    """
    tracked = _FakeLayer("label", TRACKED_FIELDS)
    shared = _FakeLayer("capture", ["capture_id", "stac_id"])
    assert layer_tools.track_filter_for(tracked, TRACK, REGISTRY) is not None
    assert layer_tools.track_filter_for(shared, TRACK, REGISTRY) is None
    assert layer_tools.track_filter_for(tracked, None, REGISTRY) is None


def test_the_canary_clause_pins_the_layer_to_the_servers_own_value():
    clause = layer_tools.track_filter_for(_FakeLayer("label", TRACKED_FIELDS), TRACK, REGISTRY)
    uri = layer_tools.build_layer_uri(_settings(), "label", REGISTRY, TRACK, clause)
    # Redundant under RLS, and the whole point: if the track ever stops reaching the
    # database, app.track() falls back to the DEFAULT track and answers with somebody
    # else's polygons. With this the layer goes empty instead.
    #
    # The expression's own single quotes are backslash-escaped, because the whole filter
    # is itself a quoted URI value. Asserted literally rather than loosely: a filter that
    # loses its quoting is a layer QGIS refuses to open, and the message it gives names
    # neither the layer nor the cause.
    assert "filter='\"track_id\" = \\'" + TRACK.track_id + "\\''" in uri


def test_a_project_saved_on_another_track_is_noticed_rather_than_redirected(monkeypatch):
    """The shared-.qgz case.

    A layer keeps talking to the track it was loaded from -- correctly, because the track
    is in its own data source. The only thing that could go wrong is nobody being told.
    """
    here = _FakeLayer("label", TRACKED_FIELDS)
    here.properties = {layer_tools.TRACK_PROPERTY: TRACK.name}
    elsewhere = _FakeLayer("label-other", TRACKED_FIELDS)
    elsewhere.properties = {layer_tools.TRACK_PROPERTY: "somewhere_else"}
    _with_layers(monkeypatch, here, elsewhere)

    strays = layer_tools.layers_on_other_tracks(TRACK)
    assert strays == [elsewhere]


def test_a_layer_loaded_before_tracks_existed_is_not_reported_as_a_stray(monkeypatch):
    # It records no track, so there is nothing to disagree with and nothing to say.
    untracked = _FakeLayer("label", LABEL_FIELDS)
    _with_layers(monkeypatch, untracked)
    assert layer_tools.layers_on_other_tracks(TRACK) == []


def test_switching_tracks_is_refused_while_edits_are_unsaved(monkeypatch):
    """setDataSource on a dirty layer discards the buffer with no prompt and no undo.

    Ten minutes of drawing would vanish because somebody changed a combo box, so the
    controller refuses; this is the query it refuses on.
    """
    clean = _FakeLayer("clean", TRACKED_FIELDS)
    dirty = _FakeLayer("dirty", TRACKED_FIELDS)
    dirty.editable = True
    dirty.modified = True
    _with_layers(monkeypatch, clean, dirty)
    assert layer_tools.dirty_layers() == [dirty]


def test_a_track_with_no_uuid_gets_the_header_but_no_canary():
    # The backend did not send an id, so there is nothing to filter on -- and an
    # always-false clause would be a worse answer than no check at all.
    nameless = Track(name="nameonly")
    uri = layer_tools.build_layer_uri(_settings(), "label", REGISTRY, nameless)
    assert "http-header:X-Track='nameonly'" in uri
    assert (
        layer_tools.track_filter_for(_FakeLayer("label", TRACKED_FIELDS), nameless, REGISTRY)
        is None
    )


def test_re_pointing_a_loaded_layer_keeps_the_canary_and_costs_one_round_trip(monkeypatch):
    """The as-of control and the track switch both go through here.

    The clause is worked out from the layer's EXISTING fields, before the source is
    swapped: the field set is the same collection either way, so re-pointing first and
    asking afterwards would be a second provider round trip for nothing. Getting this
    wrong the other way is worse -- ``repoint_layer`` rebuilds the provider, so an as-of
    change that forgot the clause would silently drop the canary.
    """
    calls = []
    monkeypatch.setattr(layer_tools, "repoint_layer", lambda layer, uri: calls.append(uri))
    layer = _FakeLayer("label", TRACKED_FIELDS)
    layer.properties = {layer_tools.COLLECTION_PROPERTY: "label"}
    layer.setCustomProperty = lambda key, value: layer.properties.__setitem__(key, value)

    layer_tools.repoint_for(layer, _settings(), REGISTRY, TRACK)

    assert len(calls) == 1
    assert TRACK.track_id in calls[0]
    assert layer.properties[layer_tools.TRACK_PROPERTY] == TRACK.name


# --- the transaction-time axis -----------------------------------------------
#
# A historical layer answers "what did the team believe at this instant" -- including
# labels deleted since. Three properties matter, and each one fails silently if it is
# wrong: the instant has to reach the database on every request the provider makes; a
# change on the OTHER axis must not quietly turn a historical layer into a live one; and
# the QA tools must never mistake one for the layer the annotator is working on.

from qgis_label_client.core import recorded  # noqa: E402

MOMENT = "2026-01-15T08:00:00Z"
ASOF_FIELDS = [
    *LABEL_FIELDS,
    "track_id",
    "asof_id",
    "belief_from",
    "belief_to",
    "superseded",
    "recorded_at",
]


def _landing_query(uri: str) -> dict[str, str]:
    """The query parameters on a data-source URI's `url=`, decoded."""
    landing = re.search(r"url='([^']*)'", uri).group(1)
    return {k: v[0] for k, v in parse_qs(urlparse(landing).query).items()}


def _historical(name: str = "believed", echo: object = "@" + MOMENT) -> _FakeLayer:
    """A loaded historical layer, serving `echo` in its recorded_at column.

    The default is what a correctly pinned layer answers with -- the instant it asked for,
    carrying the server's `@` marker. Pass something else to simulate the pin not arriving.
    """
    layer = _FakeLayer(name, ASOF_FIELDS)
    layer.properties = {
        layer_tools.COLLECTION_PROPERTY: "label_asof",
        layer_tools.RECORDED_AT_PROPERTY: MOMENT,
        layer_tools.TRACK_PROPERTY: TRACK.name,
    }
    values = [None] * len(ASOF_FIELDS)
    values[ASOF_FIELDS.index("recorded_at")] = echo
    layer.rows = [_Feature(values)]
    return layer


def test_the_instant_rides_on_the_landing_url_because_headers_do_not_arrive():
    """THE regression that made this feature not work at all, guarded.

    QGIS 3.44's OAPIF provider drops the URI's `http-header:` parameters -- captured against
    a bare listener, none of three headers reached the wire -- so a header-only pin never
    arrived and every historical layer quietly answered as of now(). A landing-URL query
    parameter does survive, onto every request the provider builds bar the OPTIONS probe.
    """
    uri = layer_tools.build_layer_uri(
        _settings(), "label_asof", REGISTRY, TRACK, recorded_at=MOMENT
    )
    # Percent-encoded, because it is a query parameter and `:` is reserved. Asserted
    # through a parse rather than as a literal, so the test is about the value the server
    # receives rather than about how urlencode happens to spell it.
    assert _landing_query(uri)["recorded_at"] == MOMENT
    # The header is still sent: it costs one parameter, it is what curl and the viewer use,
    # and a build that starts honouring it would just be saying the same thing twice.
    assert f"http-header:X-Recorded-At='{MOMENT}'" in uri
    assert f"http-header:X-Track='{TRACK.name}'" in uri


def test_a_live_layer_sends_no_instant_at_all():
    uri = layer_tools.build_layer_uri(_settings(), "label", REGISTRY, TRACK)
    assert "X-Recorded-At" not in uri
    assert "recorded_at=" not in uri


def test_a_correctly_pinned_layer_passes_the_echo_check():
    layer_tools.verify_recorded_echo(_historical(), MOMENT, REGISTRY)


def test_the_echo_check_catches_a_pin_that_did_not_arrive():
    """The failure this closes is the POPULATED one, and it was observed live.

    v_label_asof falls back to now() when no instant reaches it, so a pin that goes missing
    answers a January request with today's data under a layer named for January. The view
    echoes the instant it actually resolved at; comparing that to what was asked for is the
    detector.
    """
    layer = _historical(echo="@2026-08-25T14:39:11Z")
    with pytest.raises(BackendError) as err:
        layer_tools.verify_recorded_echo(layer, MOMENT, REGISTRY)
    # It names BOTH instants. A message saying only "mismatch" leaves the reader unable to
    # tell a lost pin from a clock skew from a stale layer.
    assert MOMENT in str(err.value)
    assert "2026-08-25T14:39:11Z" in str(err.value)


def test_the_echo_check_accepts_the_server_marker_being_absent_or_a_date():
    """Tolerant about the SHAPE, strict about the INSTANT.

    The echo is a value the server chose, read back through whatever type QGIS decided the
    column had. An older backend sends no `@`; a QGIS that types the column as a date hands
    back a datetime. Both still answer the only question being asked.
    """
    layer_tools.verify_recorded_echo(_historical(echo=MOMENT), MOMENT, REGISTRY)
    layer_tools.verify_recorded_echo(
        _historical(echo=datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)), MOMENT, REGISTRY
    )


def test_the_echo_check_is_silent_where_it_has_nothing_to_check():
    # No column, no pin, and no rows. The last one matters: a historical view legitimately
    # has no rows before the first label was drawn, and an empty layer cannot mislead
    # anybody about what the team believed.
    layer_tools.verify_recorded_echo(_FakeLayer("label", LABEL_FIELDS), MOMENT, REGISTRY)
    layer_tools.verify_recorded_echo(_historical(), "", REGISTRY)
    empty = _historical()
    empty.rows = []
    layer_tools.verify_recorded_echo(empty, MOMENT, REGISTRY)


def test_the_echo_is_never_a_layer_filter():
    """It was one, and QGIS compiled it onto the WRONG AXIS.

    QGIS types an OAPIF property by sniffing its value, so the unprefixed instant arrived as
    a DateTime field and `"recorded_at" = '...'` compiled to `?datetime=` -- valid time. The
    canary verified nothing, a deliberately wrong one still returned features, and the
    layer's Temporal Controller was pinned to one valid instant as a side effect.
    """
    uri = layer_tools.build_layer_uri(
        _settings(), "label_asof", REGISTRY, TRACK, recorded_at=MOMENT
    )
    assert 'recorded_at" =' not in uri
    assert "filter=" not in uri


def test_the_track_canary_still_survives_on_a_historical_layer(monkeypatch):
    # track_id is a UUID, which QGIS types as a string, so a filter on it stays a filter.
    calls = []
    monkeypatch.setattr(layer_tools, "repoint_layer", lambda layer, uri: calls.append(uri))
    layer = _historical()

    layer_tools.repoint_for(layer, _settings(), REGISTRY, TRACK)

    assert len(calls) == 1
    assert TRACK.track_id in calls[0]
    assert MOMENT in calls[0]


def test_a_track_switch_does_not_quietly_turn_a_historical_layer_into_a_live_one(monkeypatch):
    """THE regression this axis is most exposed to.

    Every caller of repoint_for is changing some other axis -- the track, or the valid-time
    as-of -- and would have to remember to carry this one through. Forgetting produces a
    layer with an unchanged name, unchanged styling and present-day data. So the instant is
    read back off the layer rather than passed in, and it cannot be forgotten.
    """
    calls = []
    monkeypatch.setattr(layer_tools, "repoint_layer", lambda layer, uri: calls.append(uri))
    layer = _historical()

    layer_tools.repoint_for(layer, _settings(), REGISTRY, OTHER_TRACK)

    assert _landing_query(calls[0])["recorded_at"] == MOMENT
    assert OTHER_TRACK.track_id in calls[0]
    assert layer.properties[layer_tools.RECORDED_AT_PROPERTY] == MOMENT


def test_re_pointing_re_applies_the_read_only_hold(monkeypatch):
    # setDataSource rebuilds the provider, and QGIS recomputes a layer's read-only state
    # from the new provider's capabilities when it does. Without re-applying, a track switch
    # would hand back an editable view of a past belief.
    monkeypatch.setattr(layer_tools, "repoint_layer", lambda layer, uri: None)
    layer = _historical()
    layer.read_only = False

    layer_tools.repoint_for(layer, _settings(), REGISTRY, TRACK)

    assert layer.read_only is True
    assert "believed" in layer.abstract.lower()


def test_a_live_layer_is_not_marked_read_only_by_a_re_point(monkeypatch):
    monkeypatch.setattr(layer_tools, "repoint_layer", lambda layer, uri: None)
    layer = _FakeLayer("label", TRACKED_FIELDS)
    layer.properties = {layer_tools.COLLECTION_PROPERTY: "label"}

    layer_tools.repoint_for(layer, _settings(), REGISTRY, TRACK)

    assert layer.read_only is False


def test_a_historical_layer_is_never_mistaken_for_the_layer_being_worked_on(monkeypatch):
    """It carries label_id and class_id, exactly as the live collection does.

    Picking it would run the coverage check, the history dialog and the selection they
    drive over a belief the team has since revised -- including labels deleted long ago --
    with nothing on screen to say so. Worse than picking the audit layer, and just as
    silent, because mapLayers() order is not something the plugin controls.
    """
    believed = _historical()
    live = _FakeLayer("label", LABEL_FIELDS)
    _with_layers(monkeypatch, believed, live)
    assert layer_tools.find_label_layer(REGISTRY) is live

    # ...and in the other order, because that is exactly what varies.
    _with_layers(monkeypatch, live, believed)
    assert layer_tools.find_label_layer(REGISTRY) is live


def test_a_historical_layer_alone_yields_no_label_layer_rather_than_itself(monkeypatch):
    _with_layers(monkeypatch, _historical())
    assert layer_tools.find_label_layer(REGISTRY) is None


def test_a_deployment_that_renames_the_echo_column_is_still_covered(monkeypatch):
    """Two independent exclusions, because they fail in different circumstances.

    The field-name test comes from the registry, so it follows a renamed column. The
    property test holds even for a deployment whose historical view exposes entirely
    different columns, because the plugin stamped that property itself.
    """
    exotic = _FakeLayer("believed", [*LABEL_FIELDS, "resolved_at"])
    exotic.properties = {
        layer_tools.COLLECTION_PROPERTY: "elsewhere",
        layer_tools.RECORDED_AT_PROPERTY: MOMENT,
    }
    _with_layers(monkeypatch, exotic)
    assert layer_tools.find_label_layer(REGISTRY) is None


def test_the_collection_list_does_not_treat_a_historical_layer_as_already_loaded(monkeypatch):
    # Otherwise a historical layer occupies its collection's slot and the live one becomes
    # unloadable -- silently, by a `continue`.
    believed = _historical()
    live = _FakeLayer("label", TRACKED_FIELDS)
    live.properties = {layer_tools.COLLECTION_PROPERTY: "label"}
    _with_layers(monkeypatch, believed, live)

    assert layer_tools.live_layers() == [live]
    assert layer_tools.historical_layers() == [believed]


def test_two_historical_layers_at_different_instants_coexist(monkeypatch):
    """The case the header-versus-query decision turns on.

    An http-header: parameter lives in each layer's own data source, so two views of two
    different beliefs are two ordinary layers rather than a mode somebody has to toggle.
    """
    january = _historical("january")
    june = _historical("june")
    june.properties = dict(june.properties)
    june.properties[layer_tools.RECORDED_AT_PROPERTY] = "2026-06-01T00:00:00Z"
    _with_layers(monkeypatch, january, june)

    instants = {layer_tools.recorded_at_of(layer) for layer in layer_tools.historical_layers()}
    assert instants == {MOMENT, "2026-06-01T00:00:00Z"}


def test_a_layer_knows_which_belief_it_shows_after_a_project_reload():
    # The custom property is what makes a .qgz reopen on the instant it was saved with,
    # rather than on whatever a setting happens to say.
    assert layer_tools.recorded_at_of(_historical()) == MOMENT
    assert layer_tools.is_historical(_historical())
    assert layer_tools.recorded_at_of(_FakeLayer("label", LABEL_FIELDS)) == ""
    assert not layer_tools.is_historical(_FakeLayer("label", LABEL_FIELDS))


def test_the_echo_column_names_a_historical_collection_without_knowing_its_id():
    # Collection ids are a deployment's choice, exactly as class names are.
    assert recorded.exposes_recorded_axis(ASOF_FIELDS, REGISTRY.fields)
    assert not recorded.exposes_recorded_axis(LABEL_FIELDS, REGISTRY.fields)
