"""The "load label collections" panel: one checkbox per mode, not per geometry.

core/collections.py's group_by_mode decides WHICH collections collapse into one row --
tested without QGIS in test_collections_and_history.py. What is tested here is the half
that function cannot reach on its own: that LabelClientDock actually renders one
QListWidgetItem per CollectionGroup, pre-checks it only when EVERY member is already on
the map (never when merely one of several is -- a checkbox that overstates what is loaded
is worse than one that understates it, see set_collections' own comment), and that
checked_collections() hands the controller a flat list of ids regardless of how many rows
were collapsed to produce it.
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt

from qgis_label_client.core.collections import Collection, CollectionGroup, group_by_mode
from qgis_label_client.dockwidget import (
    COLLECTION_ROLE,
    LabelClientDock,
    _collection_group_tooltip,
)


def _collection(collection_id: str, title: str = "", transactional=None, description=None):
    return Collection(
        collection_id=collection_id,
        title=title or collection_id,
        transactional=transactional,
        description=description,
    )


def _dock() -> LabelClientDock:
    return LabelClientDock(None)


# --- rendering: one row per group, tuple ids regardless of group size -------


def test_a_single_member_group_renders_and_round_trips_like_a_plain_collection_did():
    dock = _dock()
    group = CollectionGroup(
        stem="labeled_extent",
        display_name="CVI Surveyed extents",
        members=(_collection("labeled_extent", "CVI Surveyed extents"),),
    )
    dock.set_collections([group])
    item = dock.collection_list.item(0)
    # A comma-joined string, not the tuple itself -- see set_collections' own comment on
    # why this matches every other item-data role in this codebase.
    assert item.data(COLLECTION_ROLE) == "labeled_extent"

    item.setCheckState(Qt.CheckState.Checked)
    assert dock.checked_collections() == ["labeled_extent"]


def test_checking_a_collapsed_row_yields_every_members_id():
    dock = _dock()
    group = CollectionGroup(
        stem="label_current",
        display_name="CVI Labels (current, read-only)",
        members=(
            _collection("label_current_polygon"),
            _collection("label_current_point"),
            _collection("label_current_line"),
        ),
    )
    dock.set_collections([group])
    assert dock.collection_list.count() == 1
    dock.collection_list.item(0).setCheckState(Qt.CheckState.Checked)
    assert sorted(dock.checked_collections()) == [
        "label_current_line",
        "label_current_point",
        "label_current_polygon",
    ]


def test_an_unchecked_collapsed_row_contributes_nothing():
    dock = _dock()
    group = CollectionGroup(
        stem="label_current",
        display_name="mode",
        members=(_collection("label_current_polygon"), _collection("label_current_point")),
    )
    dock.set_collections([group])
    assert dock.checked_collections() == []


def test_group_by_mode_feeds_set_collections_with_no_shape_mismatch():
    # The integration this whole change is for: what plugin.py actually hands the panel.
    dock = _dock()
    collections = [
        _collection("label_polygon", "CVI Labels — areas (editable)"),
        _collection("label_point", "CVI Labels — points (editable)"),
        _collection("label_line", "CVI Labels — lines (editable)"),
        _collection("labeled_extent", "CVI Surveyed extents"),
    ]
    dock.set_collections(group_by_mode(collections))
    assert dock.collection_list.count() == 2  # one collapsed row, one lone row


# --- the pre-check rule: ALL members, never merely one -----------------------


def _trio() -> CollectionGroup:
    return CollectionGroup(
        stem="label_current",
        display_name="mode",
        members=(
            _collection("label_current_polygon"),
            _collection("label_current_point"),
            _collection("label_current_line"),
        ),
    )


def test_the_row_is_checked_when_every_sibling_is_already_loaded():
    dock = _dock()
    dock.set_collections(
        [_trio()],
        checked={
            "label_current_polygon",
            "label_current_point",
            "label_current_line",
        },
    )
    assert dock.collection_list.item(0).checkState() == Qt.CheckState.Checked


def test_the_row_stays_unchecked_when_only_one_of_three_siblings_is_loaded():
    # The failure this guards: if only a fraction of a mode were counted as "loaded", the
    # checkbox would assert the whole mode is on the map while most of its geometry is not
    # drawn anywhere -- populated and wrong, not merely incomplete.
    dock = _dock()
    dock.set_collections([_trio()], checked={"label_current_polygon"})
    assert dock.collection_list.item(0).checkState() == Qt.CheckState.Unchecked


def test_the_row_is_unchecked_when_none_of_the_siblings_are_loaded():
    dock = _dock()
    dock.set_collections([_trio()], checked=set())
    assert dock.collection_list.item(0).checkState() == Qt.CheckState.Unchecked


# --- tooltip content ----------------------------------------------------------


def test_a_single_member_tooltip_is_unchanged_from_a_plain_collection():
    group = CollectionGroup(
        stem="labeled_extent",
        display_name="CVI Surveyed extents",
        members=(
            _collection(
                "labeled_extent",
                "CVI Surveyed extents",
                transactional=None,
                description="Surveyed ground.",
            ),
        ),
    )
    tooltip = _collection_group_tooltip(group)
    assert tooltip == (
        "id: labeled_extent\nSurveyed ground.\nEditability not advertised by the server."
    )


def test_a_single_member_tooltip_says_nothing_extra_when_read_only():
    group = CollectionGroup(
        stem="s",
        display_name="s",
        members=(_collection("s", transactional=False),),
    )
    assert _collection_group_tooltip(group) == "id: s"


def test_a_collapsed_tooltip_lists_ids_and_keeps_per_member_descriptions_separate():
    group = CollectionGroup(
        stem="label_current",
        display_name="mode",
        members=(
            _collection(
                "label_current_polygon",
                transactional=True,
                description="Area-shaped classes.",
            ),
            _collection(
                "label_current_point",
                transactional=True,
                description="Point-shaped classes.",
            ),
            _collection("label_current_line", transactional=True),
        ),
    )
    tooltip = _collection_group_tooltip(group)
    lines = tooltip.splitlines()
    assert lines[0] == ("ids: label_current_line, label_current_point, label_current_polygon")
    # Merged prose would lose which class list belongs to which geometry family; each
    # description stays pinned to the id that actually has it.
    assert "label_current_polygon: Area-shaped classes." in lines
    assert "label_current_point: Point-shaped classes." in lines
    assert lines[-1] == "Editable (OGC API - Features Part 4)."


def test_a_collapsed_tooltip_states_editability_once_when_every_member_agrees():
    group = CollectionGroup(
        stem="s",
        display_name="s",
        members=(_collection("a", transactional=False), _collection("b", transactional=False)),
    )
    tooltip = _collection_group_tooltip(group)
    assert "read-only" not in tooltip  # False adds no line, exactly like a single row
    assert "Editable" not in tooltip


def test_a_collapsed_tooltip_names_the_split_when_members_disagree():
    # Not expected on this deployment -- provider-identical siblings agree by
    # construction -- but the spec does not guarantee it, so a message that would end an
    # investigation is worth having ready rather than presenting a box that is just wrong.
    group = CollectionGroup(
        stem="s",
        display_name="s",
        members=(
            _collection("label_current_point", transactional=True),
            _collection("label_current_line", transactional=False),
            _collection("label_current_polygon", transactional=None),
        ),
    )
    tooltip = _collection_group_tooltip(group)
    assert "Editability disagrees between parts of this collection:" in tooltip
    assert "label_current_line=read-only" in tooltip
    assert "label_current_point=editable" in tooltip
    assert "label_current_polygon=not advertised" in tooltip
