"""The bootstrap publish plan, the feature it drafts, and the report it produces.

Three properties are worth more than the rest, and each has a failure mode that is silent:

* **no client-side identity.** ``id`` is 0% populated across all 1,246 source features and
  the server assigns ``label_id``. A draft that carried an id would look correct and would
  reintroduce the single hardest defect in the source data.
* **geometry types must match the class exactly.** The server compares ``ST_GeometryType``
  against ``label_class.geom_type`` with ``<>``, so a Polygon offered to a MultiPolygon
  class is rejected -- and shapefiles hand back both spellings for the same drawing.
* **the second publish must not be silent.** Identity is the server's, so nothing here can
  deduplicate; the only defence is a warning that a layer has been sent before.
"""

from __future__ import annotations

import pytest
from snapshot_fixtures import REGISTRY, SEED_CLASSES, SNAPSHOT_LAYERS

from qgis_label_client.core.fields import CoreFields
from qgis_label_client.core.legacy import map_fields
from qgis_label_client.core.publish import (
    LayerChoice,
    LayerOutcome,
    PublishRecord,
    PublishReport,
    SourceLayer,
    build_draft,
    build_plan,
    conform_geometry,
    format_record,
    parse_record,
    was_promoted,
)
from qgis_label_client.core.registry import parse_registry

COMPOUND = REGISTRY.get("compound")
COOLING_UNIT = REGISTRY.get("cooling_unit")

SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
POINT = {"type": "Point", "coordinates": [113.3, 41.0]}


def _source(name: str, **kwargs) -> SourceLayer:
    return SourceLayer(
        layer_id=kwargs.pop("layer_id", name.lower()),
        name=name,
        field_names=tuple(SNAPSHOT_LAYERS.get(name, ())),
        feature_count=kwargs.pop("feature_count", 10),
        **kwargs,
    )


# --- geometry ---------------------------------------------------------------


def test_a_polygon_is_promoted_to_the_multipolygon_the_class_declares():
    # The single most common reshape in this dataset: six of the seven layers are
    # polygons and every polygon class declares MultiPolygon.
    conformed, problem = conform_geometry(SQUARE, "MultiPolygon")
    assert problem is None
    assert conformed["type"] == "MultiPolygon"
    assert conformed["coordinates"] == [SQUARE["coordinates"]]
    assert was_promoted(SQUARE, conformed)


def test_a_point_is_promoted_to_multipoint():
    conformed, problem = conform_geometry(POINT, "MultiPoint")
    assert problem is None and conformed["coordinates"] == [POINT["coordinates"]]


def test_a_single_part_multi_geometry_can_be_demoted_losslessly():
    # cooling_unit declares Point, and OGR reports shapefile points as MultiPoint in
    # some drivers. One part in, one part out.
    multi = {"type": "MultiPoint", "coordinates": [POINT["coordinates"]]}
    conformed, problem = conform_geometry(multi, "Point")
    assert problem is None and conformed == POINT


def test_a_genuine_multi_part_geometry_is_never_silently_flattened():
    multi = {"type": "MultiPoint", "coordinates": [[0, 0], [1, 1]]}
    conformed, problem = conform_geometry(multi, "Point")
    assert conformed is None
    assert "discarding geometry" in problem


def test_a_class_accepting_any_geometry_takes_what_it_is_given():
    conformed, problem = conform_geometry(SQUARE, "Any")
    assert problem is None and conformed is SQUARE
    assert not was_promoted(SQUARE, conformed)


def test_an_unrelated_type_is_refused_rather_than_mangled():
    conformed, problem = conform_geometry(SQUARE, "MultiLineString")
    assert conformed is None and "cannot reshape" in problem


@pytest.mark.parametrize("geometry", [None, {}, {"coordinates": []}])
def test_a_feature_with_nothing_on_the_ground_is_refused(geometry):
    # A shapefile "Null shape" is an attribute row with no geometry. label.geom is NOT
    # NULL and there is nothing to invent.
    conformed, problem = conform_geometry(geometry, "MultiPolygon")
    assert conformed is None and problem


# --- one feature ------------------------------------------------------------


def _compound_draft(values, geometry=SQUARE, **kwargs):
    mappings = map_fields(SNAPSHOT_LAYERS["Compounds"], COMPOUND)
    return build_draft(values, geometry, COMPOUND, mappings, **kwargs)


def test_a_drafted_feature_carries_no_identity_at_all():
    # Even when the source column is populated -- it is not, in any of the 1,246 features,
    # which is the defect being fixed. Identity is issued by the server on insert; a
    # client-side id would look correct and would reintroduce exactly that defect.
    result = _compound_draft({"id": 42, "Name_en": "Yunhui Ulanqab"})
    feature = result.draft.to_geojson()
    assert "id" not in feature
    assert set(feature["properties"]) == {"class_id", "names"}
    assert 42 not in feature["properties"].values()


def test_names_and_attributes_land_in_their_json_containers():
    result = _compound_draft(
        {"Name:ch": "云汇数据中心", "Name_en": "Yunhui", "No. transf": "6"}
    )
    feature = result.draft.to_geojson()
    assert feature["properties"]["names"] == {"zh": "云汇数据中心", "en": "Yunhui"}
    assert feature["properties"]["attrs"] == {"transformer_count": 6}
    assert feature["properties"]["class_id"] == "compound"
    assert feature["geometry"]["type"] == "MultiPolygon"


def test_empty_containers_are_omitted_rather_than_sent_as_empty_objects():
    # Both columns default to '{}' server-side; an omitted key cannot be mistaken for a
    # client asserting emptiness.
    feature = _compound_draft({"id": None}).draft.to_geojson()
    assert feature["properties"] == {"class_id": "compound"}


def test_the_field_names_come_from_the_registry_not_from_this_plugin():
    fields = CoreFields().merged({"class_id": "kind", "names": "labels"})
    feature = _compound_draft({"Name_en": "Yunhui"}).draft.to_geojson(fields)
    assert feature["properties"]["kind"] == "compound"
    assert feature["properties"]["labels"] == {"en": "Yunhui"}


def test_a_promoted_geometry_is_reported_so_the_summary_can_say_so():
    assert _compound_draft({}).promoted is True


def test_a_damaged_name_is_reported_even_when_it_is_published():
    result = _compound_draft({"Name:ch": "云枢智能云乌兰察布数据中X8"})
    assert result.damaged_names == ("zh",)
    assert result.omitted_names == ()
    assert result.draft.names["zh"].endswith("X8")


def test_skipping_damaged_names_removes_them_from_the_draft():
    result = _compound_draft(
        {"Name:ch": "云枢智能云乌兰察布数据中X8", "Name_en": "Yunshu"},
        skip_damaged_names=True,
    )
    assert result.draft.names == {"en": "Yunshu"}
    assert result.omitted_names == ("zh",)


def test_a_feature_whose_geometry_cannot_be_reshaped_is_not_drafted():
    result = build_draft({}, POINT, COMPOUND, ())
    assert result.draft is None and not result.published
    assert "geometry" in result.issues[0]


def test_attribute_problems_travel_with_the_draft_rather_than_stopping_it():
    result = _compound_draft({"Year": 1200, "Name_en": "Yunhui"})
    assert result.draft is not None
    assert result.draft.attrs == {}
    assert result.issues and "minimum" in result.issues[0]


def test_a_point_layer_drafts_against_its_point_class():
    mappings = map_fields(SNAPSHOT_LAYERS["CoolingUnits"], COOLING_UNIT)
    result = build_draft({"Model": "Dry cooler A"}, POINT, COOLING_UNIT, mappings)
    assert result.draft.to_geojson()["geometry"] == POINT
    assert result.draft.attrs == {"model": "Dry cooler A"}
    assert not result.promoted


# --- the idempotency record -------------------------------------------------


def test_a_publish_record_round_trips_through_a_layer_property():
    record = PublishRecord(
        published_at="2026-08-23T10:00:00+00:00",
        collection_id="label",
        class_id="compound",
        feature_count=190,
    )
    assert parse_record(format_record(record)) == record


@pytest.mark.parametrize("raw", ["", None, "not json", "[]", '"text"', 42])
def test_an_unreadable_record_is_treated_as_no_record(raw):
    # A corrupt custom property must not block a publish; the only thing lost is the
    # warning it would have produced.
    assert parse_record(raw) is None


def test_the_record_says_what_a_second_publish_would_do():
    text = PublishRecord(collection_id="label", feature_count=190).describe()
    assert "SECOND copy" in text
    assert "190" in text


# --- the plan ---------------------------------------------------------------


def test_a_fresh_layer_is_preselected_with_its_guessed_class():
    plan = build_plan([_source("Compounds", feature_count=190)], REGISTRY)
    only = plan.layers[0]
    assert only.choice.publish is True
    assert only.class_id == "compound"
    assert only.publish
    assert plan.total_features() == 190


def test_a_layer_with_no_confident_guess_is_not_preselected():
    plan = build_plan([_source("Roads")], REGISTRY)
    assert plan.layers[0].choice.publish is False
    assert plan.selected() == ()
    assert "Nothing selected" in plan.summary()


def test_a_previously_published_layer_defaults_to_off():
    # The idempotency guard. Re-running after a partial failure is the obvious thing to
    # do, and the server cannot recognise a repeat, so the second send must be reached
    # for deliberately.
    previous = PublishRecord(published_at="2026-08-23T10:00:00+00:00", feature_count=190)
    plan = build_plan([_source("Compounds", previous=previous)], REGISTRY)
    assert plan.layers[0].choice.publish is False
    assert any("SECOND copy" in note for note in plan.layers[0].notes())


def test_republishing_is_reported_once_the_user_ticks_the_box():
    previous = PublishRecord(feature_count=190)
    source = _source("Compounds", previous=previous)
    plan = build_plan(
        [source],
        REGISTRY,
        {source.layer_id: LayerChoice(source.layer_id, publish=True, class_id="compound")},
    )
    assert [p.source.name for p in plan.republished()] == ["Compounds"]


def test_an_explicit_class_choice_overrides_the_guess():
    source = _source("Compounds")
    plan = build_plan(
        [source],
        REGISTRY,
        {source.layer_id: LayerChoice(source.layer_id, publish=True, class_id="administrative")},
    )
    assert plan.layers[0].class_id == "administrative"
    # The columns are remapped against the class actually chosen.
    assert all(m.target is None for m in plan.layers[0].mappings if m.source.startswith("No."))


def test_selecting_a_layer_with_no_class_is_a_blocking_problem():
    source = _source("Roads")
    plan = build_plan(
        [source], REGISTRY, {source.layer_id: LayerChoice(source.layer_id, publish=True)}
    )
    assert plan.problems() and "no class chosen" in plan.problems()[0]


def test_a_retired_class_cannot_be_published_into():
    registry = parse_registry(
        {"classes": [dict(e, active=e["class_id"] != "compound") for e in SEED_CLASSES]}
    )
    source = _source("Compounds")
    plan = build_plan(
        [source],
        registry,
        {source.layer_id: LayerChoice(source.layer_id, publish=True, class_id="compound")},
    )
    assert "retired" in plan.problems()[0]


def test_an_empty_layer_is_a_blocking_problem():
    source = _source("Compounds", feature_count=0)
    plan = build_plan(
        [source],
        REGISTRY,
        {source.layer_id: LayerChoice(source.layer_id, publish=True, class_id="compound")},
    )
    assert "no features" in plan.problems()[0]


def test_reprojection_is_announced_before_it_happens():
    plan = build_plan([_source("Compounds", crs_authid="EPSG:32649")], REGISTRY)
    assert any("EPSG:4326" in note for note in plan.layers[0].notes())
    assert (
        not build_plan([_source("Compounds", crs_authid="EPSG:4326")], REGISTRY)
        .layers[0]
        .source.needs_reprojection
    )


def test_a_crs_with_no_authority_code_still_needs_reprojecting():
    # QGIS reports an empty authid() for a CRS defined only by WKT. Reading that as
    # "already EPSG:4326" would publish its coordinates verbatim.
    assert _source("Compounds", crs_authid="").needs_reprojection is True


def test_a_layer_with_no_valid_crs_cannot_be_published_at_all():
    # The failure this blocks is silent in every layer of the stack: QGIS builds a
    # transform that does nothing, PostGIS has no range check on a 4326 column, and
    # ST_GeometryType still matches the class. Projected metres become degrees and the
    # features look exactly like valid data.
    source = _source("Compounds", crs_valid=False)
    plan = build_plan(
        [source],
        REGISTRY,
        {source.layer_id: LayerChoice(source.layer_id, publish=True, class_id="compound")},
    )
    assert plan.problems() and ".prj" in plan.problems()[0]
    assert source.needs_reprojection is False


def test_an_unusable_crs_is_not_quietly_treated_as_the_storage_crs():
    assert _source("Compounds", crs_valid=False).needs_reprojection is False


def test_a_partial_damage_scan_is_reported_as_a_floor_not_a_total():
    plan = build_plan(
        [_source("Compounds", feature_count=190, damaged_names=81, scanned=100)], REGISTRY
    )
    note = next(n for n in plan.layers[0].notes() if "final character" in n)
    assert note.startswith("at least 81")


def test_a_complete_damage_scan_states_the_number_plainly():
    plan = build_plan(
        [_source("Compounds", feature_count=190, damaged_names=81, scanned=190)], REGISTRY
    )
    note = next(n for n in plan.layers[0].notes() if "final character" in n)
    assert note.startswith("81 name(s)")
    assert "PUBLISHED AS THEY ARE" in note


def test_the_plan_says_which_classes_are_being_published_without_a_survey_extent():
    # The claim nobody remembers to refuse deliberately. 872 cooling units on one campus
    # and 187 compounds with none in the data are indistinguishable without this.
    plan = build_plan([_source("Compounds"), _source("CoolingUnits")], REGISTRY)
    assert plan.classes_without_extent() == ("compound", "cooling_unit")


def test_declaring_an_extent_removes_that_class_from_the_warning():
    compounds = _source("Compounds")
    cooling = _source("CoolingUnits")
    plan = build_plan(
        [compounds, cooling],
        REGISTRY,
        {
            compounds.layer_id: LayerChoice(
                compounds.layer_id,
                publish=True,
                class_id="compound",
                extent_completeness="partial",
            ),
            cooling.layer_id: LayerChoice(cooling.layer_id, publish=True, class_id="cooling_unit"),
        },
    )
    assert plan.classes_without_extent() == ("cooling_unit",)


def test_the_preview_can_show_every_column_and_where_it_goes():
    # The matcher is structural, so it maps a column onto whichever declared attribute
    # its concept is a subset of and cannot know that a particular column is wrong for
    # reasons outside the schema. Making the whole mapping visible is the only defence
    # that does not require the plugin to carry a second copy of the vocabulary.
    plan = build_plan([_source("Compounds")], REGISTRY)
    lines = plan.layers[0].mapping_lines()

    assert len(lines) == len(SNAPSHOT_LAYERS["Compounds"])
    assert any(line.startswith("Name:ch -> name") for line in lines)
    assert any("cooling_unit_count" in line for line in lines)
    # The standing example: empty in all 1,246 features today, and a square-degree value
    # in a square-metre attribute the moment somebody fills it in.
    assert any(line.startswith("Area -> attribute") for line in lines)


def test_a_mapping_line_carries_what_the_registry_says_about_the_target():
    # "Area -> attribute area_m2" looks correct and is exactly the mapping that is wrong.
    # The sentence that gives it away is in the class's own schema and arrives at runtime
    # with the class, so showing it is not a second copy of the vocabulary -- and dropping
    # it leaves the human with nothing to catch the mistake with.
    plan = build_plan([_source("Compounds")], REGISTRY)
    line = next(li for li in plan.layers[0].mapping_lines() if li.startswith("Area ->"))

    assert "NEVER in EPSG:4326" in line


def test_a_mapping_line_carries_the_declared_range_and_length():
    plan = build_plan([_source("Compounds")], REGISTRY)
    lines = plan.layers[0].mapping_lines()

    assert any("range 1990..2100" in line for line in lines)


def test_the_field_summary_counts_what_lands_where():
    summary = build_plan([_source("Compounds")], REGISTRY).layers[0].mapping_summary()
    assert "1 unmapped" in summary
    assert "2 -> name" in summary


def test_there_is_no_field_summary_until_a_class_is_chosen():
    source = _source("Roads")
    plan = build_plan(
        [source], REGISTRY, {source.layer_id: LayerChoice(source.layer_id, publish=True)}
    )
    assert plan.layers[0].mapping_summary() == ""
    assert plan.layers[0].mapping_lines() == ()


def test_the_summary_counts_features_layers_and_classes():
    plan = build_plan(
        [_source("Compounds", feature_count=190), _source("CoolingUnits", feature_count=872)],
        REGISTRY,
    )
    summary = plan.summary()
    assert "1062 feature(s)" in summary and "2 layer(s)" in summary


def test_damaged_names_are_counted_across_the_whole_plan():
    plan = build_plan(
        [
            _source("Compounds", damaged_names=81, scanned=190),
            _source("Substation", damaged_names=2, scanned=22),
        ],
        REGISTRY,
    )
    assert plan.damaged_name_count() == 83


# --- the report -------------------------------------------------------------


def test_the_whole_snapshot_plans_and_drafts_end_to_end():
    """Every source layer, guessed, mapped and drafted into the geometry its class wants.

    The seven layers are two polygon spellings, a point layer and a line layer against
    four different declared geometry types. This is the shape of the actual bootstrap.
    """
    geometry_for = {
        "Point": POINT,
        "LineString": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
    }
    plan = build_plan([_source(name) for name in SNAPSHOT_LAYERS], REGISTRY)

    assert len(plan.selected()) == len(SNAPSHOT_LAYERS)
    for layer_plan in plan:
        label_class = layer_plan.label_class
        assert label_class is not None, layer_plan.guess.describe()
        source_geometry = geometry_for.get(label_class.geom_type.removeprefix("Multi"), SQUARE)
        result = build_draft({}, source_geometry, label_class, layer_plan.mappings)
        assert result.draft is not None, result.issues
        assert result.draft.geometry["type"] == label_class.geom_type


def test_the_report_totals_across_layers():
    report = PublishReport()
    first = report.outcome_for("Compounds", "compound")
    first.published = 188
    first.failed = 1
    first.skipped_invalid_geometry = 1
    second = report.outcome_for("CoolingUnits", "cooling_unit")
    second.published = 872

    assert report.published == 1060
    assert report.failed == 1
    assert report.skipped == 1
    assert not report.clean
    assert "1060 feature(s) published" in report.summary()
    assert "1 rejected by the server" in report.summary()


def test_a_cancelled_run_says_what_it_left_behind():
    report = PublishReport(cancelled=True)
    report.outcome_for("Compounds", "compound").published = 40
    assert "Cancelled after publishing 40" in report.summary()
    assert "second copy" in report.summary()


def test_repeated_issues_are_deduplicated_with_a_count():
    # A 1,246-feature run produces the same complaint hundreds of times. An
    # undeduplicated list is a wall, and a wall is not read.
    outcome = LayerOutcome(layer_name="Compounds", class_id="compound")
    for _ in range(3):
        outcome.note("invalid geometry, rejected before sending")
    outcome.note("something else")
    report = PublishReport(outcomes=[outcome])
    lines = report.detail_lines()
    assert any("[x3]" in line for line in lines)
    assert any(line.endswith("something else") for line in lines)


def test_the_layer_line_names_every_departure_from_a_plain_run():
    outcome = LayerOutcome(
        layer_name="Compounds",
        class_id="compound",
        published=188,
        skipped_invalid_geometry=2,
        promoted=188,
        reprojected=True,
        damaged_names=81,
        extent_declared=True,
    )
    line = outcome.line()
    for expected in ("188 published", "2 skipped", "promoted", "EPSG:4326", "81 damaged"):
        assert expected in line


def test_the_coverage_warning_states_the_consequence_not_a_count():
    report = PublishReport(classes_without_extent=("compound", "cooling_unit"))
    warning = report.coverage_warning()
    assert "UNKNOWN" in warning and "never negative" in warning
    assert "cannot be reconstructed later" in warning
    assert "compound" in warning and "cooling_unit" in warning


def test_there_is_no_coverage_warning_when_every_class_declared_one():
    assert PublishReport().coverage_warning() == ""
