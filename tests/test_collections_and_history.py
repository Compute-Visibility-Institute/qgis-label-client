"""Parsing the two backend documents the plugin reads directly."""

from __future__ import annotations

import pytest

from qgis_label_client.core.collections import Collection, group_by_mode, parse_collections
from qgis_label_client.core.errors import BackendError
from qgis_label_client.core.fields import CoreFields
from qgis_label_client.core.history import parse_history

COLLECTIONS = {
    "collections": [
        {
            "id": "zeta",
            "title": "Zeta collection",
            "description": "Something",
            "extent": {
                "spatial": {"bbox": [[84.8, 23.0, 125.1, 46.9]]},
                "temporal": {"interval": [["2026-04-21T00:00:00Z", None]]},
            },
            "links": [{"rel": "http://www.opengis.net/def/rel/ogc/1.0/create-replace-delete"}],
        },
        {"id": "alpha"},
    ]
}


def test_collections_sort_by_display_name():
    parsed = parse_collections(COLLECTIONS)
    assert [c.collection_id for c in parsed] == ["alpha", "zeta"]


def test_a_collection_without_a_title_falls_back_to_its_id():
    alpha = parse_collections(COLLECTIONS)[0]
    assert alpha.display_name == "alpha"


def test_bbox_and_interval_are_extracted():
    zeta = parse_collections(COLLECTIONS)[1]
    assert zeta.bbox == (84.8, 23.0, 125.1, 46.9)
    assert zeta.temporal_interval == ("2026-04-21T00:00:00Z", None)


def test_a_six_element_bbox_keeps_the_horizontal_corners():
    document = {
        "collections": [{"id": "a", "extent": {"spatial": {"bbox": [[1, 2, 0, 3, 4, 100]]}}}]
    }
    assert parse_collections(document)[0].bbox == (1.0, 2.0, 3.0, 4.0)


def test_transactional_is_tri_state():
    parsed = {c.collection_id: c for c in parse_collections(COLLECTIONS)}
    assert parsed["zeta"].transactional is True
    # Unknown, not False: guessing read-only would hide the editing capability that is
    # the whole reason QGIS is the editing surface.
    assert parsed["alpha"].transactional is None


@pytest.mark.parametrize("document", [{}, {"collections": "x"}, "nope"])
def test_non_collections_documents_raise(document):
    with pytest.raises(BackendError):
        parse_collections(document)


# --- grouping typed siblings for the panel -----------------------------------
#
# The complaint this section protects against: a deployment serving label_current,
# label_current_polygon/point/line (and the same for _asof and _history) put up to 12
# checkboxes in the "load label collections" panel for what the analyst experiences as
# three modes -- current, as-of, history. group_by_mode collapses each stem's typed
# siblings into one row, using ids and vocabulary this deployment ACTUALLY listed, never
# a word compiled into the plugin -- see core/routing.py's own docstring for why that
# rule exists at all.


def _collection(
    collection_id: str,
    title: str = "",
    transactional: bool | None = None,
    description: str | None = None,
) -> Collection:
    return Collection(
        collection_id=collection_id,
        title=title or collection_id,
        transactional=transactional,
        description=description,
    )


def test_three_typed_siblings_collapse_to_one_group_and_check_all_three_ids():
    # "label_polygon"/"label_point"/"label_line": this deployment's editable stem, with no
    # bare "label" collection alongside it at all. Collapsing must not depend on a mixed
    # sibling existing to trigger on.
    members = [
        _collection("label_polygon", "CVI Labels — areas (editable)"),
        _collection("label_point", "CVI Labels — points (editable)"),
        _collection("label_line", "CVI Labels — lines (editable)"),
    ]
    groups = group_by_mode(members)
    assert len(groups) == 1
    group = groups[0]
    assert set(group.collection_ids) == {"label_polygon", "label_point", "label_line"}


def test_a_lone_mixed_collection_with_no_typed_siblings_stays_its_own_row():
    # labeled_extent has no geometry word at all -- nothing to collapse it with, and
    # nothing here may guess that it should disappear just because it stands alone.
    members = [_collection("labeled_extent", "CVI Surveyed extents")]
    groups = group_by_mode(members)
    assert len(groups) == 1
    assert groups[0].collection_ids == ("labeled_extent",)
    assert groups[0].display_name == "CVI Surveyed extents"


def test_an_untyped_sibling_alongside_a_typed_trio_is_dropped():
    # label_current is REFUSED unconditionally by layers.mixes_geometry -- it carries a
    # geom_family column by schema, on every deployment, not by sampling luck -- so it
    # contributes its title to the collapsed row and is never itself a member.
    members = [
        _collection("label_current", "CVI Labels (current, read-only)"),
        _collection("label_current_polygon", "CVI Labels — areas (current, read-only)"),
        _collection("label_current_point", "CVI Labels — points (current, read-only)"),
        _collection("label_current_line", "CVI Labels — lines (current, read-only)"),
    ]
    groups = group_by_mode(members)
    assert len(groups) == 1
    group = groups[0]
    assert group.display_name == "CVI Labels (current, read-only)"
    assert set(group.collection_ids) == {
        "label_current_polygon",
        "label_current_point",
        "label_current_line",
    }
    assert "label_current" not in group.collection_ids


def test_the_editable_stems_display_name_falls_back_to_the_stem_qualified_by_transactional():
    # This deployment's typed titles say "areas"/"points"/"lines", vocabulary
    # routing._FAMILY_TOKENS deliberately does not recognise (it knows "polygon"/"point"/
    # "line", the id vocabulary). Deriving a group name by editing a typed title would
    # need an English-synonym table this plugin refuses to hardcode -- the stem is the
    # fallback instead, because it is always id-derived. A bare "Label" is silent on the
    # one thing this exact row exists to answer, so every member agreeing `transactional`
    # qualifies it -- this is the real deployment's own shape: label_polygon/point/line
    # advertise `editable: true` and have no untyped sibling to borrow a title from.
    members = [
        _collection("label_polygon", "CVI Labels — areas (editable)", transactional=True),
        _collection("label_point", "CVI Labels — points (editable)", transactional=True),
        _collection("label_line", "CVI Labels — lines (editable)", transactional=True),
    ]
    assert group_by_mode(members)[0].display_name == "Label (editable)"


def test_the_stem_fallback_stays_bare_when_members_disagree_or_are_all_unknown():
    # Naming a state the row is not actually in is worse than naming none. A split
    # verdict must not resolve to either qualifier by chance of iteration order, and
    # pygeoapi not advertising the field at all (every member None) is not a report
    # of "read-only" -- it is a report of "unstated".
    disagreeing = [
        _collection("odd_polygon", "Odd areas", transactional=True),
        _collection("odd_point", "Odd points", transactional=False),
    ]
    assert group_by_mode(disagreeing)[0].display_name == "Odd"

    unstated = [
        _collection("odd_polygon", "Odd areas"),
        _collection("odd_point", "Odd points"),
    ]
    assert group_by_mode(unstated)[0].display_name == "Odd"


def test_a_collection_this_grouping_has_never_seen_is_neither_grouped_nor_dropped():
    members = [
        _collection("label_current", "CVI Labels (current, read-only)"),
        _collection("label_current_polygon", "CVI Labels — areas (current, read-only)"),
        _collection("label_current_point", "CVI Labels — points (current, read-only)"),
        _collection("label_current_line", "CVI Labels — lines (current, read-only)"),
        _collection("labeled_extent", "CVI Surveyed extents"),
    ]
    groups = group_by_mode(members)
    by_stem = {group.stem: group for group in groups}
    assert set(by_stem) == {"label_current", "labeled_extent"}
    # Neither swept into the collapsed row nor removed for standing alone.
    assert by_stem["labeled_extent"].collection_ids == ("labeled_extent",)


def test_group_rows_are_sorted_like_parse_collections_sorts_plain_ones():
    members = [
        _collection("labeled_extent", "CVI Surveyed extents"),
        _collection("label_polygon", "CVI Labels — areas (editable)"),
        _collection("label_point", "CVI Labels — points (editable)"),
        _collection("label_line", "CVI Labels — lines (editable)"),
    ]
    groups = group_by_mode(members)
    names = [group.display_name.lower() for group in groups]
    assert names == sorted(names)


# --- history ----------------------------------------------------------------

HISTORY = {
    "type": "FeatureCollection",
    "features": [
        {
            "id": 1,
            "properties": {
                "history_id": 1,
                "label_id": "uuid-1",
                "operation": "INSERT",
                "changed": ["created"],
                "actor": "someone@example.org",
                "recorded_from": "2026-01-02T00:00:00Z",
                "recorded_to": "2026-03-01T00:00:00Z",
                "names": {"zh": "示例", "en": "Example"},
            },
        },
        {
            "id": 2,
            "properties": {
                "history_id": 2,
                "label_id": "uuid-1",
                "operation": "UPDATE",
                "changed": "{geom,attrs}",
                "actor": "someone@example.org",
                "reason": "re-digitised from newer imagery",
                "recorded_from": "2026-03-01T00:00:00Z",
                "recorded_to": None,
                "names": {"en": "Example"},
            },
        },
    ],
}


def test_history_is_newest_belief_first():
    entries = parse_history(HISTORY)
    assert [e.history_id for e in entries] == [2, 1]


def test_the_open_transaction_range_is_the_current_belief():
    newest = parse_history(HISTORY)[0]
    assert newest.is_current_belief is True
    assert parse_history(HISTORY)[1].is_current_belief is False


def test_postgres_array_literals_are_understood():
    newest = parse_history(HISTORY)[0]
    assert newest.changed == ("geom", "attrs")
    assert newest.changed_summary() == "geom, attrs"


def test_name_summary_prefers_chinese():
    # 82.6% of compounds have a Chinese name and 8.9% an English one.
    oldest = parse_history(HISTORY)[1]
    assert oldest.name_summary() == "示例"


def test_name_summary_falls_back_when_there_is_no_chinese_name():
    newest = parse_history(HISTORY)[0]
    assert newest.name_summary() == "Example"


def test_history_honours_server_supplied_field_names():
    fields = CoreFields().merged({"label_id": "lid"})
    document = {"features": [{"properties": {"lid": "uuid-9", "operation": "INSERT"}}]}
    assert parse_history(document, fields)[0].label_id == "uuid-9"


@pytest.mark.parametrize("document", ["nope", {"nothing": 1}])
def test_unusable_history_documents_raise(document):
    with pytest.raises(BackendError):
        parse_history(document)
