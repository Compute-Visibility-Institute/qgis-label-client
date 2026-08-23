"""Coverage classification.

The rule being protected: unlabeled ground inside an exhaustive extent is negative;
everything else is UNKNOWN. Getting that backwards silently poisons every model trained
on the export, so the distinction is asserted from several directions.
"""

from __future__ import annotations

from qgis_label_client.core.coverage import (
    Coverage,
    ExtentRef,
    LabelRef,
    applicable_extents,
    build_report,
    classify,
    extent_from_properties,
    label_from_properties,
)
from qgis_label_client.core.fields import CoreFields

ALPHA = LabelRef(feature_id=1, label_id="uuid-1", class_id="alpha")
BETA = LabelRef(feature_id=2, label_id="uuid-2", class_id="beta")

EXHAUSTIVE_ALPHA = ExtentRef(extent_id="e1", class_id="alpha", completeness="exhaustive")
PARTIAL_ALPHA = ExtentRef(
    extent_id="e2", class_id="alpha", completeness="partial", caveat="north strip clouded"
)
UNKNOWN_ALPHA = ExtentRef(extent_id="e3", class_id="alpha", completeness="unknown")
EXHAUSTIVE_BETA = ExtentRef(extent_id="e4", class_id="beta", completeness="exhaustive")


def _hits(*extents):
    """An intersection predicate that reports exactly the given extents as hits."""
    chosen = set(map(id, extents))
    return lambda _label, extent: id(extent) in chosen


def test_extents_are_filtered_by_class_before_any_geometry_test():
    # An exhaustive sweep for one class says nothing about another on the same ground.
    # Conflating them is the original defect restated.
    assert applicable_extents("alpha", [EXHAUSTIVE_ALPHA, EXHAUSTIVE_BETA]) == [EXHAUSTIVE_ALPHA]


def test_a_label_in_a_same_class_exhaustive_extent_is_covered():
    finding = classify(ALPHA, [EXHAUSTIVE_ALPHA], _hits(EXHAUSTIVE_ALPHA))
    assert finding.coverage is Coverage.EXHAUSTIVE
    assert finding.extent_ids == ("e1",)


def test_a_label_inside_another_class_extent_is_still_unsurveyed():
    finding = classify(ALPHA, [EXHAUSTIVE_BETA], _hits(EXHAUSTIVE_BETA))
    assert finding.coverage is Coverage.UNSURVEYED


def test_partial_only_is_not_exhaustive():
    # A qualified sweep does not license treating the surroundings as negative.
    finding = classify(ALPHA, [PARTIAL_ALPHA], _hits(PARTIAL_ALPHA))
    assert finding.coverage is Coverage.PARTIAL


def test_exhaustive_wins_when_extents_overlap():
    finding = classify(
        ALPHA, [PARTIAL_ALPHA, EXHAUSTIVE_ALPHA], _hits(PARTIAL_ALPHA, EXHAUSTIVE_ALPHA)
    )
    assert finding.coverage is Coverage.EXHAUSTIVE


def test_unknown_completeness_is_treated_as_unsurveyed():
    # 007_labeled_extent.sql defines 'unknown' as a backfilled guess.
    finding = classify(ALPHA, [UNKNOWN_ALPHA], _hits(UNKNOWN_ALPHA))
    assert finding.coverage is Coverage.UNSURVEYED


def test_a_label_outside_everything_is_unsurveyed():
    finding = classify(ALPHA, [EXHAUSTIVE_ALPHA], _hits())
    assert finding.coverage is Coverage.UNSURVEYED
    assert finding.extent_ids == ()


def test_report_flags_classes_with_no_exhaustive_extent_anywhere():
    report = build_report([ALPHA, BETA], [EXHAUSTIVE_ALPHA], _hits(EXHAUSTIVE_ALPHA))
    assert report.classes_without_extents == ("beta",)
    assert len(report.unsurveyed) == 1
    assert not report.clean


def test_a_partial_extent_does_not_count_as_declaring_the_class():
    report = build_report([ALPHA], [PARTIAL_ALPHA], _hits(PARTIAL_ALPHA))
    assert report.classes_without_extents == ("alpha",)


def test_clean_report_when_everything_is_inside_an_exhaustive_extent():
    report = build_report([ALPHA], [EXHAUSTIVE_ALPHA], _hits(EXHAUSTIVE_ALPHA))
    assert report.clean
    assert "exhaustive survey extent" in report.summary()


def test_summary_states_the_consequence_not_just_a_count():
    report = build_report([ALPHA], [EXHAUSTIVE_ALPHA], _hits())
    summary = report.summary()
    assert "UNKNOWN" in summary
    assert "not negative" in summary


def test_extent_properties_default_to_the_safest_reading():
    # No completeness recorded must not be read as 'exhaustive'.
    ref = extent_from_properties({"class_id": "alpha"}, CoreFields(), fallback_id=7)
    assert ref is not None
    assert ref.completeness == "unknown"
    assert ref.extent_id == "7"


def test_extent_without_a_class_is_rejected():
    assert extent_from_properties({"completeness": "exhaustive"}, CoreFields()) is None


def test_property_readers_honour_server_supplied_field_names():
    fields = CoreFields().merged({"class_id": "kind", "completeness": "how_complete"})
    ref = extent_from_properties({"kind": "alpha", "how_complete": "exhaustive"}, fields)
    assert ref is not None and ref.is_exhaustive

    label = label_from_properties({"kind": "alpha", "label_id": "u"}, fields, feature_id=3)
    assert label is not None and label.class_id == "alpha" and label.feature_id == 3
