"""History tracks: parsing them, and refusing to publish into the wrong one.

WHAT IS ACTUALLY AT RISK HERE

Not isolation -- that is row-level security in the database, and nothing in this plugin
implements or can weaken it. What is at risk is the annotator *knowing which dataset they
are in*, and that risk is entirely one-sided: every failure in this area produces data
that looks completely correct. A publish into the test track leaves 1,246 plausible
polygons in a dataset nobody will ever look at; a publish into the analysts' track from a
session somebody thought was a test contaminates the one dataset that matters. Neither can
be undone, because identity is server-assigned and nothing on this side can find those
rows again.

So the tests below are mostly about *saying so*: the track is in the plan, in the preview
banner, in the button, in the confirmation, in the report and on the layer stamp. The one
genuinely mechanical property is the canary -- if the track ever fails to reach the
database, a layer must go EMPTY rather than quietly show the other dataset.

No track name in this file is one any deployment would use. Tracks are data, exactly like
classes: see :data:`snapshot_fixtures.TRACK`.
"""

from __future__ import annotations

import pytest
from snapshot_fixtures import ARCHIVED_TRACK, OTHER_TRACK, REGISTRY, SNAPSHOT_LAYERS, TRACK

from qgis_label_client.core import tracks as track_tools
from qgis_label_client.core.errors import BackendError
from qgis_label_client.core.expressions import all_of, equals, identifier, literal
from qgis_label_client.core.publish import (
    LayerChoice,
    PublishRecord,
    PublishReport,
    SourceLayer,
    build_plan,
    format_record,
    parse_record,
)
from qgis_label_client.core.tracks import STATUS_ARCHIVED, Track, parse_tracks
from qgis_label_client.core.uri import build_oapif_uri, header_params
from qgis_label_client.core.urls import tracks_url

# --- parsing ----------------------------------------------------------------


def test_a_track_list_is_read_from_the_backend_not_from_here():
    parsed = parse_tracks(
        {
            "tracks": [
                {"name": "beta", "track_id": "b-uuid", "sort_order": 20},
                {"name": "alpha", "track_id": "a-uuid", "sort_order": 10, "is_default": True},
            ]
        }
    )
    assert [t.name for t in parsed] == ["alpha", "beta"]
    assert parsed[0].is_default and parsed[0].track_id == "a-uuid"


def test_a_bare_array_is_accepted_too():
    # The plugin's two existing custom endpoints disagree on the wrapper. Hard-failing on
    # the shape turns a cosmetic backend change into "the plugin is broken".
    assert [t.name for t in parse_tracks([{"name": "solo"}])] == ["solo"]


@pytest.mark.parametrize("document", [{}, {"tracks": "alpha"}, "alpha", None, 7])
def test_a_response_that_is_not_a_track_list_is_an_error_not_an_empty_list(document):
    # An empty list reads as "this deployment has no tracks", which is a completely
    # different fact and one that would silently disable every write.
    with pytest.raises(BackendError):
        parse_tracks(document)


def test_entries_with_no_name_are_dropped_rather_than_named_by_position():
    assert parse_tracks({"tracks": [{"track_id": "x"}, {"name": "ok"}]})[0].name == "ok"


def test_an_archived_track_is_readable_and_not_writable():
    assert ARCHIVED_TRACK.archived is True
    assert ARCHIVED_TRACK.writable is False
    # Readable forever, writable never: that one difference is the whole of archiving.
    assert "read-only" in ARCHIVED_TRACK.describe().lower()
    assert "archived" in ARCHIVED_TRACK.warning().lower()


def test_an_active_track_has_nothing_to_warn_about():
    assert TRACK.warning() == ""


def test_the_status_vocabulary_matches_the_schema():
    # There is deliberately no 'deleted': a track is archived, never dropped, because its
    # label_history rows are the proof of what happened.
    assert STATUS_ARCHIVED == "archived"
    assert Track(name="x").status == track_tools.STATUS_ACTIVE


# --- resolving --------------------------------------------------------------


def test_an_empty_setting_resolves_to_the_deployment_default():
    tracks = [Track(name="a"), Track(name="b", is_default=True)]
    assert track_tools.resolve(tracks, "").name == "b"


def test_a_track_the_backend_does_not_offer_resolves_to_nothing_not_to_the_default():
    """The contamination failure in reverse, and the reason this is not a fallback.

    Silently answering a request for one dataset from another would leave the annotator
    concluding their track was empty -- and then drawing into the one they were handed.
    """
    tracks = [Track(name="a", is_default=True)]
    assert track_tools.resolve(tracks, "gone") is None


def test_an_archived_track_is_never_the_implicit_default():
    # The database refuses every write to it, so landing there by default would mean a
    # session that cannot save and does not say why.
    tracks = [Track(name="old", is_default=True, status=STATUS_ARCHIVED)]
    assert track_tools.default_track(tracks) is None


# --- the canary -------------------------------------------------------------


def test_the_canary_filters_on_the_track_the_server_itself_supplies():
    clause = track_tools.canary_filter(TRACK)
    assert clause == f"\"track_id\" = '{TRACK.track_id}'"


def test_a_track_with_no_uuid_cannot_be_verified_and_gets_no_filter():
    # Filtering on a value the backend did not send would produce an always-false clause
    # and an empty layer, which is a worse answer than no check at all.
    assert track_tools.canary_filter(Track(name="nameonly")) is None


def test_the_canary_and_the_as_of_filter_compose_without_rebinding_the_or():
    """The bracket bug this would otherwise have.

    cql2_filter is ``a <= t AND (b IS NULL OR b > t)``. Concatenating a second clause with
    a bare AND rebinds that OR and changes which features come back -- a filter that is
    wrong rather than absent, which is the harder of the two to notice.
    """
    asof = '"valid_from" <= \'t\' AND ("valid_to" IS NULL OR "valid_to" > \'t\')'
    combined = all_of(asof, equals("track_id", "u"))
    assert combined == f"({asof}) (\"track_id\" = 'u')".replace(") (", ") AND (")
    assert combined.startswith("(") and " AND (" in combined


def test_one_clause_is_left_unbracketed_so_the_common_uri_stays_readable():
    assert all_of("a = 1") == "a = 1"
    assert all_of(None, "", None) == ""


def test_identifiers_and_literals_are_quoted_for_a_qgis_expression():
    # Double quotes are a QGIS expression's column reference; single quotes are a string.
    # Handing the provider literal CQL2 makes the LAYER invalid, not the filter ignored.
    assert identifier('od"d') == '"od""d"'
    assert literal("it's") == "'it''s'"


def test_a_mismatch_is_a_sentence_that_says_do_not_edit():
    message = track_tools.mismatch(TRACK, OTHER_TRACK.track_id)
    assert OTHER_TRACK.track_id in message and TRACK.name in message
    assert "not in force" in message


@pytest.mark.parametrize("value", [None, "", "  ", "null"])
def test_an_absent_track_id_is_not_a_mismatch(value):
    # Several collections are shared between tracks on purpose and carry no track_id.
    assert track_tools.mismatch(TRACK, value) == ""


def test_a_matching_track_is_silent():
    assert track_tools.mismatch(TRACK, TRACK.track_id) == ""


# --- the URI ----------------------------------------------------------------


def test_the_track_rides_on_the_layer_uri_as_a_request_header():
    """The route that always survives.

    QGIS's native provider makes the item requests -- including the Part 4 writes -- from
    links the server returns, so a landing-page query parameter can be dropped. A header
    in the URI is attached per request and cannot be.
    """
    uri = build_oapif_uri(
        landing_url="https://host/oapif",
        collection_id="label",
        headers={"X-Track": TRACK.name},
    )
    assert f"http-header:X-Track='{TRACK.name}'" in uri


def test_a_blank_header_value_is_dropped_rather_than_sent_empty():
    # A blank X-Track is not "no track": it is a header the edge has to decide about.
    assert header_params({"X-Track": ""}) == {}
    assert header_params(None) == {}


def test_the_tracks_endpoint_lives_under_the_backend_namespace():
    assert tracks_url("https://host/oapif", "v1/tracks") == "https://host/oapif/v1/tracks"


# --- the publish plan -------------------------------------------------------


def _source(name: str, **kwargs) -> SourceLayer:
    return SourceLayer(
        layer_id=kwargs.pop("layer_id", name.lower()),
        name=name,
        field_names=tuple(SNAPSHOT_LAYERS.get(name, ())),
        feature_count=kwargs.pop("feature_count", 10),
        **kwargs,
    )


def _plan(track, sources=None, **choice_kwargs):
    sources = sources or [_source("Compounds")]
    choices = {
        s.layer_id: LayerChoice(s.layer_id, publish=True, class_id="compound", **choice_kwargs)
        for s in sources
    }
    return build_plan(sources, REGISTRY, choices, track)


def test_a_plan_with_no_track_cannot_be_published():
    """Blocking, unlike every other claim on that screen.

    A missing survey extent is unrecoverable and still only a warning, because it
    describes something the publish fails to say. This describes the publish saying it in
    the wrong place, and there is no defensible version of "into a dataset nobody named".
    """
    problems = _plan(None).problems()
    assert problems and "No history track is selected" in problems[0]


def test_an_archived_track_cannot_be_published_into():
    # The database refuses every write to it, so this would fail feature by feature after
    # the first request -- 1,246 round trips to discover something knowable now.
    problems = _plan(ARCHIVED_TRACK).problems()
    assert problems and "archived" in problems[0]


def test_the_plan_names_the_dataset_even_when_nothing_is_wrong():
    """The sentence that is always rendered.

    Every other warning on the preview appears only when there is something to warn
    about, so a clean preview says nothing at all about where 1,246 features are going --
    and "where" is the one decision on that screen that was made in another panel.
    """
    claim = _plan(TRACK).track_claim()
    assert TRACK.name in claim
    assert "cannot be undone" in claim


def test_the_summary_and_the_button_both_carry_the_track():
    plan = _plan(TRACK)
    assert f"on track {TRACK.name}" in plan.summary()
    assert plan.track_name == TRACK.name


def test_publishing_the_same_layer_to_a_second_track_is_not_a_duplicate():
    """And must not be reported as one.

    Sending a shapefile into a test track and then into the analysts' track is how a test
    dataset gets populated. Calling it a duplicate would train people to click through the
    warning that catches the real one.
    """
    previous = PublishRecord(collection_id="label", feature_count=190, track=OTHER_TRACK.name)
    source = _source("Compounds", previous=previous)
    plan = build_plan([source], REGISTRY, None, TRACK)

    # Pre-selected, unlike a layer already published to THIS track.
    assert plan.layers[0].choice.publish is True
    assert plan.republished_elsewhere()
    assert plan.republished()  # still announced, with a different sentence


def test_publishing_the_same_layer_to_the_same_track_stays_off_by_default():
    previous = PublishRecord(collection_id="label", feature_count=190, track=TRACK.name)
    plan = build_plan([_source("Compounds", previous=previous)], REGISTRY, None, TRACK)
    assert plan.layers[0].choice.publish is False
    assert not plan.republished_elsewhere()


def test_a_record_written_before_tracks_existed_claims_no_track():
    # Nobody recorded where those features went; inventing an answer would be a claim the
    # data cannot support.
    record = parse_record('{"collection_id": "label", "feature_count": 5}')
    assert record.track == ""
    assert record.on_another_track(TRACK.name) is False


def test_the_previous_publish_line_names_the_track_it_went_to():
    record = PublishRecord(collection_id="label", feature_count=190, track=OTHER_TRACK.name)
    assert f"track {OTHER_TRACK.name!r}" in record.describe()


def test_a_record_round_trips_with_its_track():
    record = PublishRecord(
        published_at="2026-08-25T00:00:00+00:00",
        collection_id="label",
        class_id="compound",
        feature_count=190,
        track=TRACK.name,
    )
    assert parse_record(format_record(record)) == record


# --- the report -------------------------------------------------------------


def test_every_count_the_report_states_names_the_track():
    """Because a partial run leaves somebody deciding whether to re-run.

    "Cancelled after publishing 400 feature(s)" is a different decision depending on which
    dataset the 400 are in, so the answer travels with the number.
    """
    report = PublishReport(track=TRACK.name)
    report.outcome_for("Compounds", "compound", expected=10).published = 4
    assert f"on track {TRACK.name}" in report.summary()

    report.cancelled = True
    assert f"on track {TRACK.name}" in report.summary()

    report.cancelled = False
    report.error = "boom."
    assert f"on track {TRACK.name}" in report.summary()


def test_the_coverage_warning_names_the_track_it_is_about():
    report = PublishReport(track=TRACK.name, classes_without_extent=("compound",))
    assert f"on track {TRACK.name}" in report.coverage_warning()


def test_a_report_with_no_track_still_reads_as_a_sentence():
    # Reachable from a caller that predates tracks; it must degrade, not break.
    report = PublishReport()
    report.outcome_for("Compounds", "compound").published = 1
    assert report.summary() == "1 feature(s) published."


# --- the transaction-time floor ----------------------------------------------


def test_a_track_can_say_how_far_back_its_record_goes():
    """The floor for the historical-view picker, and the fact its empty message needs.

    "Nothing was believed to exist at that instant" is a sentence; "and the record on this
    track only starts here" is an explanation. Without the second, an empty layer is
    indistinguishable from a broken one.
    """
    parsed = parse_tracks(
        {"tracks": [{"name": "alpha", "earliest_recorded": "2026-08-25T12:57:00Z"}]}
    )
    assert parsed[0].earliest_recorded == "2026-08-25T12:57:00Z"


def test_a_backend_that_has_not_shipped_the_floor_is_not_broken_by_it():
    # Every field of the response is optional as far as this plugin is concerned: a backend
    # mid-rollout must not make the panel unusable. No floor simply means the picker has
    # none, and an instant before the data existed is a valid question.
    parsed = parse_tracks({"tracks": [{"name": "alpha"}]})
    assert parsed[0].earliest_recorded == ""
