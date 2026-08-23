"""Reading a label's edit history out of the audit collection.

The history is the append-only ``label_history`` table, written by a database trigger
rather than by the API -- a client can be uninstalled and an API can be bypassed with a
psql connection string, so the trigger is the only layer nothing routes around.

The plugin's job is small: ask for one label's rows and render them. What makes it worth
having at all is the thing the source shapefiles could not do, because ``id`` was 0%
populated across all 1,246 features: identify *the same feature* across edits. Every
query here is keyed on ``label_id``, the server-assigned immutable UUID, never on the
OAPIF feature id, which is a surrogate that a genuine change on the ground deliberately
replaces.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import BackendError
from .fields import DEFAULT_FIELDS, CoreFields


@dataclass(frozen=True)
class HistoryEntry:
    """One superseded belief about one label."""

    history_id: object
    label_id: str | None
    operation: str
    changed: tuple[str, ...]
    actor: str | None
    reason: str | None
    recorded_from: str | None
    recorded_to: str | None
    class_id: str | None
    names: Mapping[str, Any] | None
    attrs: Mapping[str, Any] | None

    @property
    def is_current_belief(self) -> bool:
        """True when this belief has not been superseded.

        ``recorded_to`` is NULL for an open transaction-time range. That is the row you
        are looking at on the map right now.
        """
        return not self.recorded_to

    def changed_summary(self) -> str:
        return ", ".join(self.changed) if self.changed else ""

    def name_summary(self) -> str:
        """Best available name at this point in history.

        Chinese first: 82.6% of compounds have a Chinese name and only 8.9% an English
        one, so an English-first display would be blank most of the time. The names are
        unbounded UTF-8 JSONB here, which is the direct fix for the UTF-7 truncation
        eating the final character of about half the Chinese names in the shapefiles.
        """
        if not self.names:
            return ""
        for key in ("zh", "en"):
            value = self.names.get(key)
            if isinstance(value, str) and value:
                return value
        for value in self.names.values():
            if isinstance(value, str) and value:
                return value
        return ""


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(str(item) for item in value)
    if isinstance(value, str) and value:
        # PostgreSQL text[] can arrive as its literal form '{geom,attrs}' depending on
        # how the provider serialises it.
        stripped = value.strip("{}")
        return tuple(part for part in (p.strip().strip('"') for p in stripped.split(",")) if part)
    return ()


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def parse_history(
    document: Any,
    fields: CoreFields = DEFAULT_FIELDS,
) -> list[HistoryEntry]:
    """Parse an OAPIF FeatureCollection from the audit collection.

    Sorted newest belief first. The sort is done here rather than trusted from the server
    because ``sortby`` is an OAPIF extension and not every deployment implements it, and
    a history list in arbitrary order is worse than useless -- it reads as if edits
    happened in that order.
    """
    if not isinstance(document, Mapping):
        raise BackendError("History response is not a JSON object.")
    features = document.get("features")
    if not isinstance(features, Sequence) or isinstance(features, (str, bytes)):
        raise BackendError("History response has no 'features' array.")

    entries: list[HistoryEntry] = []
    for feature in features:
        if not isinstance(feature, Mapping):
            continue
        properties = feature.get("properties")
        if not isinstance(properties, Mapping):
            continue
        entries.append(
            HistoryEntry(
                history_id=properties.get(fields.history_id, feature.get("id")),
                label_id=str(properties[fields.label_id])
                if properties.get(fields.label_id)
                else None,
                operation=str(properties.get(fields.operation) or ""),
                changed=_as_tuple(properties.get(fields.changed)),
                actor=properties.get(fields.actor) or None,
                reason=properties.get(fields.reason) or None,
                recorded_from=properties.get(fields.recorded_from) or None,
                recorded_to=properties.get(fields.recorded_to) or None,
                class_id=properties.get(fields.class_id) or None,
                names=_as_mapping(properties.get(fields.names)),
                attrs=_as_mapping(properties.get(fields.attrs)),
            )
        )

    # Descending by recorded_from. ISO 8601 UTC strings sort lexicographically, and the
    # empty string sorts first, so reversing puts unknown timestamps last where they
    # belong rather than at the top pretending to be the most recent edit.
    entries.sort(key=lambda e: e.recorded_from or "", reverse=True)
    return entries
