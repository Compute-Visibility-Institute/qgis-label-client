"""Parsing the OGC API - Features collection list.

The plugin does not know what the backend serves. It asks. Collection ids, titles and
extents all come from ``/collections``, so adding ``labeled_extent`` or a new snapshot
collection to the deployment needs no plugin change -- the panel simply lists it.

Only the fields the panel actually shows are extracted, and every one of them is
optional in the specification, so every one of them is optional here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import routing
from .errors import BackendError


def _is_list(value: Any) -> bool:
    """True for a JSON array.

    ``isinstance(value, Sequence)`` is not enough: a string is a Sequence, so a malformed
    ``"collections": "label"`` would iterate character by character and produce a list of
    empty collections instead of an error.
    """
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


@dataclass(frozen=True)
class Collection:
    """One entry from ``/collections``."""

    collection_id: str
    title: str
    description: str | None = None
    item_type: str = "feature"
    bbox: tuple[float, float, float, float] | None = None
    temporal_interval: tuple[str | None, str | None] | None = None
    #: True when the server advertises a create/update/delete capability for this
    #: collection. Part 4 has no required flag for this, so absence means "unknown",
    #: never "read-only" -- the panel says so rather than disabling editing.
    transactional: bool | None = None

    @property
    def display_name(self) -> str:
        return self.title or self.collection_id


def _first_bbox(extent: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(extent, Mapping):
        return None
    spatial = extent.get("spatial")
    if not isinstance(spatial, Mapping):
        return None
    boxes = spatial.get("bbox")
    if not _is_list(boxes) or not boxes:
        return None
    box = boxes[0]
    if not _is_list(box) or len(box) < 4:
        return None
    try:
        # A 6-element bbox carries min/max elevation in the middle; take the horizontal
        # corners by position from each end rather than assuming 2D.
        values = [float(v) for v in box]
    except (TypeError, ValueError):
        return None
    if len(values) >= 6:
        return (values[0], values[1], values[3], values[4])
    return (values[0], values[1], values[2], values[3])


def _first_interval(extent: Any) -> tuple[str | None, str | None] | None:
    if not isinstance(extent, Mapping):
        return None
    temporal = extent.get("temporal")
    if not isinstance(temporal, Mapping):
        return None
    intervals = temporal.get("interval")
    if not _is_list(intervals) or not intervals:
        return None
    interval = intervals[0]
    if not _is_list(interval) or len(interval) < 2:
        return None
    start, end = interval[0], interval[1]
    return (start if isinstance(start, str) else None, end if isinstance(end, str) else None)


def _transactional(raw: Mapping[str, Any]) -> bool | None:
    """Best-effort read of whether the collection accepts writes.

    pygeoapi does not currently advertise Part 4 support per collection, so this stays
    tri-state. Guessing ``False`` would be worse than admitting ignorance: it would hide
    the editing capability that is the whole reason QGIS is the editing surface.
    """
    for key in ("transactional", "editable"):
        value = raw.get(key)
        if isinstance(value, bool):
            return value
    links = raw.get("links")
    if _is_list(links):
        for link in links:
            if not isinstance(link, Mapping):
                continue
            rel = link.get("rel")
            if isinstance(rel, str) and rel.endswith("/create-replace-delete"):
                return True
    return None


def parse_collections(document: Any) -> list[Collection]:
    """Parse a ``/collections`` response, sorted by display name."""
    if not isinstance(document, Mapping) or not _is_list(document.get("collections")):
        raise BackendError(
            "Response is not an OGC API - Features collections document "
            "(no 'collections' array). Check that the backend URL points at the API "
            "landing page."
        )
    parsed: list[Collection] = []
    for raw in document["collections"]:
        if not isinstance(raw, Mapping):
            continue
        collection_id = raw.get("id")
        if not isinstance(collection_id, str) or not collection_id:
            continue
        extent = raw.get("extent")
        parsed.append(
            Collection(
                collection_id=collection_id,
                title=str(raw.get("title") or collection_id),
                description=raw.get("description")
                if isinstance(raw.get("description"), str)
                else None,
                item_type=str(raw.get("itemType") or "feature"),
                bbox=_first_bbox(extent),
                temporal_interval=_first_interval(extent),
                transactional=_transactional(raw),
            )
        )
    parsed.sort(key=lambda c: (c.display_name.lower(), c.collection_id))
    return parsed


@dataclass(frozen=True)
class CollectionGroup:
    """One row in the "load label collections" panel.

    A stem with two or more geometry-typed siblings (``label_current_point`` and its
    typed neighbours) collapses to a single row here, because the panel is asking "is
    this MODE loaded", not "is this geometry family loaded" -- see :func:`group_by_mode`.
    Every other stem still gets one row per collection, with ``members`` of length one, so
    the panel has exactly one shape to render and never branches on whether a row is a
    collapsed group or a plain collection.
    """

    stem: str
    display_name: str
    members: tuple[Collection, ...]

    @property
    def collection_ids(self) -> tuple[str, ...]:
        return tuple(member.collection_id for member in self.members)


# Display vocabulary only. Routing still derives the geometry from the collection id.
# Requiring the title's geometry word to match that family prevents removing arbitrary
# differences between sibling titles (e.g. "draft" versus "approved").
_TITLE_GEOMETRIES = {
    "areas": routing.POLYGON,
    "polygons": routing.POLYGON,
    "points": routing.POINT,
    "lines": routing.LINE,
}
_TYPED_TITLE = re.compile(r"(.+?)\s+—\s+(areas|polygons|points|lines)(\s+\([^()]+\))?", re.I)


def _shared_title(members: Sequence[Collection]) -> str | None:
    """Preserve an advertised title, removing only its geometry-specific segment.

    This is a display operation, never a capability inference: pygeoapi's titles may
    say "(editable)" even when it supplies no machine-readable transactional flag.
    Collection.transactional and the native provider/account permission checks remain
    untouched. Unfamiliar title shapes and contradictory capability metadata fall back
    to the id-derived name instead of inventing a common meaning.
    """
    names: set[str] = set()
    for member in members:
        match = _TYPED_TITLE.fullmatch(member.display_name.strip())
        geometry = routing.typed(member.collection_id)
        if match is None or geometry is None:
            return None
        if _TITLE_GEOMETRIES[match[2].lower()] != geometry[0]:
            return None
        names.add(match[1].strip() + (match[3] or ""))
    if len(names) != 1:
        return None
    name = names.pop()
    flags = {member.transactional for member in members}
    if "(editable)" in name.lower() and False in flags:
        return None
    if "read-only" in name.lower() and True in flags:
        return None
    return name


def group_by_mode(collections: Sequence[Collection]) -> list[CollectionGroup]:
    """Collapse geometry-typed siblings into one row per mode, for the panel.

    "Which collection is this checkbox" and "which collection does a layer publish into"
    are different questions. :func:`.routing.build_routes` answers the second by picking
    ONE stem-group when several are offered, refusing outright on ambiguity -- the right
    move for a write that cannot be undone. This answers the first by listing EVERY
    stem-group unconditionally: the panel is never choosing between modes, only showing
    all of them, so there is no ambiguity here to refuse.

    A stem's untyped member (``label_current`` alongside ``label_current_point`` /
    ``_line`` / ``_polygon``) is dropped once two or more typed siblings share its stem.
    Not merely redundant: this deployment builds the mixed collections to carry a
    ``geom_family`` column precisely BECAUSE they mix geometries, so
    :func:`~.layers.mixes_geometry` refuses one unconditionally, every time, by schema --
    a row for it would be a checkbox that can warn and never load anything. A stem with
    fewer than two typed members has no duplicate to collapse, so each of its collections
    keeps the row it has always had; collapsing or hiding it would be solving a different,
    unasked-for problem (a checkbox that fails) instead of the one complained about
    (duplicate checkboxes for one mode).
    """
    by_stem: dict[str, list[Collection]] = {}
    for collection in collections:
        by_stem.setdefault(routing.stem_of(collection.collection_id), []).append(collection)

    groups: list[CollectionGroup] = []
    for stem, members in by_stem.items():
        typed_members = [c for c in members if routing.typed(c.collection_id) is not None]
        untyped_members = [c for c in members if routing.typed(c.collection_id) is None]

        if len(typed_members) < 2:
            # Nothing to collapse: a lone typed collection, a lone mixed one, or (in
            # principle) several untyped collections sharing a stem nobody has split. Each
            # keeps its own row, byte-for-byte the behaviour from before this function.
            groups.extend(
                CollectionGroup(stem=stem, display_name=c.display_name, members=(c,))
                for c in members
            )
            continue

        if untyped_members:
            # Empirically already the generic form this deployment wants ("CVI Labels
            # (current, read-only)"), with no geometry qualifier -- no synthesis needed.
            display_name = untyped_members[0].display_name
        else:
            # Keep the server's vocabulary where siblings differ only by geometry.
            # Otherwise use the id-derived stem, qualified only by unanimous explicit
            # capabilities. A familiar collection id never implies edit permission.
            shared = _shared_title(typed_members)
            base = shared or stem.replace("_", " ").strip().title() or stem
            flags = {member.transactional for member in typed_members}
            if flags == {True} and "(editable)" not in base.lower():
                display_name = f"{base} (editable)"
            elif flags == {False} and "read-only" not in base.lower():
                display_name = f"{base} (read-only)"
            else:
                display_name = base

        groups.append(
            CollectionGroup(stem=stem, display_name=display_name, members=tuple(typed_members))
        )

    # Mirrors parse_collections' own sort key, so collapsing rows never scrambles the
    # panel's alphabetical order into something that reads as random.
    groups.sort(key=lambda group: (group.display_name.lower(), group.stem))
    return groups
