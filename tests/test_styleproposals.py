"""Style handoff decisions, independently of any server write or QGIS renderer."""

import json
from dataclasses import replace

from snapshot_fixtures import REGISTRY, TRACK

from qgis_label_client.core.publish import LayerChoice, PublishReport, SourceLayer, build_plan
from qgis_label_client.core.stylecapture import NO_SYMBOL, CaptureResult, Note, NoteKind
from qgis_label_client.core.styleproposals import proposals_json, propose_style, resolve_styles


def proposal(color="#123456", *, current=None, include=True, layer="one"):
    return propose_style(
        layer,
        layer,
        "compound",
        CaptureResult(style={"stroke": color}, kind="fill"),
        current or {"stroke": "#654321"},
        include,
    )


def test_proposal_preserves_web_keys_and_replaces_a_custom_dash_with_solid():
    p = proposal(current={"stroke": "#654321", "min_zoom": 6, "future": {"a": 2}, "dash": [4, 2]})
    assert p.proposed == {"stroke": "#123456", "min_zoom": 6, "future": {"a": 2}}


def test_equivalent_css_colors_are_not_changes():
    p = proposal("#aabbcc", current={"stroke": "#ABC"})
    assert p.status == "unchanged"
    assert json.loads(proposals_json([p]))["proposals"] == []


def test_different_styles_conflict_even_when_one_matches_current():
    ps = resolve_styles([proposal(), proposal("#654321", layer="two")])
    assert [p.status for p in ps] == ["conflict", "conflict"]
    assert json.loads(proposals_json(ps))["proposals"] == []
    assert all("Conflicting" in p.detail_lines()[0] for p in ps)


def test_identical_class_proposals_merge_with_all_source_layers():
    doc = json.loads(proposals_json([proposal(), proposal(layer="two")]))
    assert len(doc["proposals"]) == 1
    assert doc["proposals"][0]["layers"] == ["one", "two"]


def test_opt_out_resolves_conflict_without_dropping_labels():
    ps = resolve_styles([proposal(), proposal("#abcdef", layer="two", include=False)])
    assert [p.status for p in ps] == ["proposed", "excluded"]
    assert len(json.loads(proposals_json(ps))["proposals"]) == 1


def test_capture_refusal_is_independent_of_label_publishing():
    source = SourceLayer(
        "one",
        "Compounds",
        geometry_type="MultiPolygon",
        crs_authid="EPSG:4326",
        feature_count=1,
        style_capture=CaptureResult(refusal=NO_SYMBOL),
    )
    plan = build_plan([source], REGISTRY, {"one": LayerChoice("one", True, "compound")}, TRACK)
    assert len(plan.selected()) == 1
    assert not plan.problems()
    assert plan.style_proposals()[0].status == "refused"


def test_cancelled_report_keeps_capture_losses_and_transferable_proposals():
    p = proposal()
    p = replace(p, capture=replace(p.capture, notes=(Note(NoteKind.DROPPED, "Hatch dropped."),)))
    report = PublishReport(cancelled=True, style_proposals=(p,))
    assert "Hatch dropped." in "\n".join(report.detail_lines())
    assert json.loads(report.styles_json())["proposals"][0]["notes"] == ["Hatch dropped."]


def test_unselected_layers_do_not_propose_styles():
    source = SourceLayer(
        "one", "Compounds", style_capture=CaptureResult(style={"stroke": "#123456"}, kind="fill")
    )
    plan = build_plan([source], REGISTRY, {"one": LayerChoice("one", False, "compound")}, TRACK)
    assert plan.style_proposals() == ()
