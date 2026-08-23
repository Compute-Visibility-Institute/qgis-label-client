"""Parsing the OGC API - Features collection list.

The plugin does not know what the backend serves. It asks. Collection ids, titles and
extents all come from ``/collections``, so adding ``labeled_extent`` or a new snapshot
collection to the deployment needs no plugin change -- the panel simply lists it.

Only the fields the panel actually shows are extracted, and every one of them is
optional in the specification, so every one of them is optional here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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
