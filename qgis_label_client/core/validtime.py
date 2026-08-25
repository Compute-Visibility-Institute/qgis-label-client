"""Choosing a label's ``valid_from``: when the thing was true on the ground.

READ :mod:`.asof` AND :mod:`.recorded` FIRST. All three touch time and they mean
different things by it.

    valid time        when a label was true ON THE GROUND       -> here, and .asof
    transaction time  when WE BELIEVED it                       -> .recorded

:mod:`.asof` READS valid time -- "show me the world as it stood in June". This module
WRITES it -- "the polygon I am drawing now was true in April". Same axis, opposite
direction, and the failure modes are unrelated, which is why they are separate files.

THE DEFECT THIS EXISTS TO PREVENT

Before the server grew ``valid_from`` on the write path (``013_valid_time.sql``) there was
no way for any client to state it, so every label took the column default: the instant
the row was written. An analyst traced a compound over imagery captured 2026-04-21 and the
database recorded:

    valid_from  2026-08-25T13:14:32      <- when SAVE was pressed
    capture_id  NULL

A well-formed, entirely plausible, completely false claim about when a building existed.
Nothing could catch it, because there is no such thing as an invalid timestamp. Every
as-of query, every change-detection pass and every training snapshot inherits it silently.

The fix is not "add a date field to the form". A date field the analyst must fill in by
hand for every polygon is a date field that will be wrong, because a person drawing their
two-hundredth compound of the afternoon is not re-reading the scene metadata each time.
The fix is a default that is RIGHT BY CONSTRUCTION -- taken from the imagery they are
actually looking at -- with an override for the cases where the imagery is not the whole
story.

WHY THE TOP LAYER, AND WHY VISIBILITY MATTERS

A label is traced from what is drawn on the canvas. Whatever raster sits topmost and
visible is what the analyst's eyes are on, so its acquisition instant is what the polygon
is a claim about. A layer that is present but unchecked is not being traced from and must
not vote -- otherwise the default silently follows a scene nobody is looking at.

STICKINESS, AND THE ONE RULE THAT MAKES IT SAFE

Re-deriving from scratch for every polygon would be correct and useless: the analyst who
overrode the date on polygon 1 would have to override it again on polygons 2 through 200.
So a choice sticks.

It sticks TO A RASTER STACK, never to a session. The fingerprint below is what "the same
imagery" means, and the moment the analyst brings a different scene to the top the sticky
value is abandoned and the default re-derives. That boundary is the whole safety argument:
a remembered timestamp can only ever be applied to the imagery it was chosen for.

WHY OVERRIDES ARE KEYED THE SAME WAY

"This compound was finished in March even though I am looking at the April scene" is a
statement about the April scene. Carry it to the October scene and it becomes nonsense.
So an override is remembered per stack, and switching imagery does not carry it across --
but switching BACK restores it, because the analyst's reasoning about that scene has not
changed just because they looked elsewhere for a minute.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

#: How many per-stack decisions to carry. Bounded because this is persisted, and an
#: unbounded map keyed on layer ids grows for the life of a profile. Ten is far more
#: scenes than anyone flips between in one sitting.
MEMORY_LIMIT = 10

#: How many distinct instants to offer in the "recently used" list. The analyst asked
#: for a short list of what they have been typing, not a history.
RECENT_LIMIT = 8


class Source(str, Enum):
    """Where a resolved ``valid_from`` came from.

    Carried alongside the value because the UI has to be able to SAY which of these
    happened. A date box showing 2026-03-01 with no explanation is indistinguishable
    from a date box showing 2026-03-01 because something broke.
    """

    #: Derived from the acquisition time of the topmost visible dated raster.
    CAPTURE = "capture"
    #: The same imagery as last time, so the previous choice was carried forward.
    STICKY = "sticky"
    #: The analyst said otherwise for this imagery, and it was remembered.
    OVERRIDE = "override"
    #: Nothing on top carries a date. There is no honest default.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CaptureRef:
    """One raster layer, and the instant its imagery was acquired.

    ``captured_at`` is ``None`` for a layer that is not imagery, or is imagery the
    backend could not date. Both are ordinary: a basemap has no acquisition time and
    must not be treated as though it did.
    """

    layer_id: str
    layer_name: str = ""
    captured_at: datetime | None = None
    capture_id: str | None = None
    stac_id: str | None = None


@dataclass(frozen=True)
class RasterStack:
    """The dated imagery on the canvas, topmost first, visible only.

    Built by the QGIS-facing caller, which is the only part that knows about layer trees.
    Everything below is pure so that the rules can be tested without a running QGIS.
    """

    layers: tuple[CaptureRef, ...] = ()

    @classmethod
    def of(cls, layers: Iterable[CaptureRef]) -> RasterStack:
        return cls(tuple(layers))

    def top_dated(self) -> CaptureRef | None:
        """The topmost layer that actually knows when it was taken."""
        for layer in self.layers:
            if layer.captured_at is not None:
                return layer
        return None

    def fingerprint(self) -> str:
        """Identity of "the imagery I am looking at", for stickiness.

        Built from layer ids and their acquisition instants rather than from names or
        sources. Names are editable and sources change on every signed-URL refresh --
        the plugin re-points every raster at a fresh URL at session start, so a
        source-based fingerprint would go stale daily and silently discard the analyst's
        remembered choices every morning.

        ORDER IS PART OF IT. Dragging the October scene above the April one changes what
        is being traced, so it must change the fingerprint even though the same two
        layers are loaded.
        """
        parts = [f"{layer.layer_id}@{_iso(layer.captured_at) or '-'}" for layer in self.layers]
        return "|".join(parts)


@dataclass(frozen=True)
class Decision:
    """What was used for one raster stack, and whether a person chose it."""

    value: datetime
    overridden: bool = False


@dataclass(frozen=True)
class Memory:
    """What the plugin carries between saves. Persisted, so it must stay small.

    ``by_stack`` is ordered most-recently-used first and capped at :data:`MEMORY_LIMIT`,
    which is what makes flipping between two scenes restore each one's decision without
    the map growing for the life of a profile.
    """

    by_stack: tuple[tuple[str, Decision], ...] = ()
    recent: tuple[datetime, ...] = ()

    def decision_for(self, fingerprint: str) -> Decision | None:
        for key, decision in self.by_stack:
            if key == fingerprint:
                return decision
        return None


@dataclass(frozen=True)
class Resolution:
    """A proposed ``valid_from``, and enough context to explain and undo it."""

    value: datetime | None
    source: Source
    fingerprint: str
    capture: CaptureRef | None = None
    #: What the imagery says, when that differs from :attr:`value`. Non-``None`` only
    #: for an override, and it is what "revert" goes back to.
    derived: datetime | None = None

    @property
    def can_revert(self) -> bool:
        """Whether offering "revert override" would do anything.

        False when there is nothing to revert TO -- an override standing over imagery
        that carries no date has nowhere to go, and a button that silently does nothing
        is worse than a button that is not there.
        """
        return self.source is Source.OVERRIDE and self.derived is not None

    def describe(self) -> str:
        """One line for the form, naming both numbers when they disagree.

        The override case shows the analyst's value AND the imagery's, because the whole
        point of an override is that they diverge, and a person reviewing the label later
        needs to see that a human decided this rather than that the default misfired.
        """
        if self.source is Source.UNKNOWN:
            return "No dated imagery on top — set the date by hand"
        shown = _human(self.value)
        if self.source is Source.CAPTURE:
            name = self.capture.layer_name if self.capture else ""
            return f"{shown} — from {name}" if name else f"{shown} — from the imagery"
        if self.source is Source.STICKY:
            return f"{shown} — same imagery as the last label"
        derived = _human(self.derived)
        if self.derived is None:
            return f"{shown} — you set this"
        return f"{shown} — you set this; the imagery says {derived}"


def resolve(stack: RasterStack, memory: Memory | None = None) -> Resolution:
    """Propose a ``valid_from`` for a label about to be drawn.

    Precedence, and each step is a rule somebody asked for:

    1. **A decision already made for this exact imagery** wins -- that is stickiness,
       and it is what stops the analyst re-typing a date two hundred times.
    2. **Otherwise the topmost dated raster**, because that is what is being traced.
    3. **Otherwise nothing.** Not ``now()``. A guess that looks like an answer is the
       defect this module exists to prevent, and the honest move is to say so and let
       the form insist.
    """
    memory = memory or Memory()
    fingerprint = stack.fingerprint()
    top = stack.top_dated()
    derived = top.captured_at if top else None

    decision = memory.decision_for(fingerprint)
    if decision is not None:
        return Resolution(
            value=decision.value,
            source=Source.OVERRIDE if decision.overridden else Source.STICKY,
            fingerprint=fingerprint,
            capture=top,
            # Only meaningful for an override: it is what revert goes back to, and
            # what describe() contrasts the analyst's value against.
            derived=derived if decision.overridden else None,
        )

    if derived is not None:
        return Resolution(
            value=derived,
            source=Source.CAPTURE,
            fingerprint=fingerprint,
            capture=top,
        )

    return Resolution(value=None, source=Source.UNKNOWN, fingerprint=fingerprint)


def remember(
    memory: Memory | None,
    resolution: Resolution,
    chosen: datetime | None,
) -> Memory:
    """Record what was actually saved, so the next polygon inherits it.

    ``chosen`` is what the form ended up holding -- which may be the proposal untouched,
    or a value the analyst typed over it. The difference is what marks a decision as an
    override, and an override is the thing that survives into
    :meth:`Resolution.describe` and gates the revert control.

    A choice equal to what the imagery says is NOT an override, even when the analyst
    typed it themselves. Recording it as one would put a permanent "you set this" on a
    value indistinguishable from the default, and offer a revert that changes nothing.
    """
    memory = memory or Memory()
    if chosen is None:
        return memory

    baseline = resolution.derived if resolution.source is Source.OVERRIDE else None
    if baseline is None and resolution.source is Source.CAPTURE:
        baseline = resolution.value
    if baseline is None and resolution.capture is not None:
        baseline = resolution.capture.captured_at

    overridden = baseline is None or not _same_instant(chosen, baseline)

    entries = [
        (key, decision) for key, decision in memory.by_stack if key != resolution.fingerprint
    ]
    entries.insert(0, (resolution.fingerprint, Decision(chosen, overridden)))

    return Memory(
        by_stack=tuple(entries[:MEMORY_LIMIT]),
        recent=_push_recent(memory.recent, chosen),
    )


def revert_override(memory: Memory | None, stack: RasterStack) -> tuple[Memory, Resolution]:
    """Drop the remembered decision for this imagery and re-derive from the scene.

    "Attempts to auto find it", as asked for -- and it can fail. If the imagery carries
    no date there is nothing to fall back to, and the returned resolution says
    :attr:`Source.UNKNOWN` rather than inventing one. The caller shows that; it does not
    quietly leave the old value in place, which would make the button look broken.

    The recently-used list is deliberately NOT pruned. The analyst may well want that
    instant back from the dropdown a moment later; reverting one label's date is not a
    statement that the value was never useful.
    """
    memory = memory or Memory()
    fingerprint = stack.fingerprint()
    kept = tuple((key, decision) for key, decision in memory.by_stack if key != fingerprint)
    pruned = Memory(by_stack=kept, recent=memory.recent)
    return pruned, resolve(stack, pruned)


def recently_used(memory: Memory | None) -> tuple[datetime, ...]:
    """Distinct instants the analyst has recently saved, newest first.

    Offered beside the date box for the case the whole override mechanism exists to
    serve: a handful of labels in this scene are known to date from some other moment,
    and re-picking it should be one click rather than a re-typed timestamp.
    """
    return (memory or Memory()).recent


def to_payload(memory: Memory | None) -> dict[str, object]:
    """Flatten to something ``QgsSettings`` can hold across a restart.

    JSON-able primitives only. ``QgsSettings`` round-trips through an INI file on some
    platforms, where anything richer than a string comes back as a string -- so the shape
    written here is the shape :func:`from_payload` is prepared to read back, and both
    sides use ISO instants rather than epoch numbers so a settings file stays legible to
    a person debugging one.
    """
    memory = memory or Memory()
    return {
        "by_stack": [
            {
                "stack": key,
                "value": _iso(decision.value),
                "overridden": decision.overridden,
            }
            for key, decision in memory.by_stack
        ],
        "recent": [_iso(value) for value in memory.recent],
    }


def from_payload(payload: object) -> Memory:
    """Rebuild from settings, discarding anything that does not parse.

    Tolerant by design. This is persisted state read at startup, and a profile carrying a
    half-written or older-format entry must degrade to "no memory" -- which merely
    re-derives the default from the imagery -- rather than raising on the path that
    installs the field default and leaving the analyst with no default at all.
    """
    if not isinstance(payload, Mapping):
        return Memory()

    entries: list[tuple[str, Decision]] = []
    raw_stacks = payload.get("by_stack")
    if isinstance(raw_stacks, Sequence) and not isinstance(raw_stacks, (str, bytes)):
        for item in raw_stacks:
            if not isinstance(item, Mapping):
                continue
            key = item.get("stack")
            value = _parse(item.get("value"))
            if not isinstance(key, str) or not key or value is None:
                continue
            entries.append((key, Decision(value, bool(item.get("overridden")))))

    recent: list[datetime] = []
    raw_recent = payload.get("recent")
    if isinstance(raw_recent, Sequence) and not isinstance(raw_recent, (str, bytes)):
        for item in raw_recent:
            parsed = _parse(item)
            if parsed is not None:
                recent.append(parsed)

    return Memory(
        by_stack=tuple(entries[:MEMORY_LIMIT]),
        recent=tuple(recent[:RECENT_LIMIT]),
    )


def _parse(value: object) -> datetime | None:
    """Read an instant back, accepting the literal ``Z`` that RFC 3339 permits."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def _push_recent(
    recent: Sequence[datetime], value: datetime, limit: int = RECENT_LIMIT
) -> tuple[datetime, ...]:
    """Most-recent-first, de-duplicated by instant, bounded."""
    out = [value]
    for item in recent:
        if not _same_instant(item, value) and not any(_same_instant(item, seen) for seen in out):
            out.append(item)
    return tuple(out[:limit])


def _same_instant(left: datetime | None, right: datetime | None) -> bool:
    """Equality by the instant named, not by how it was spelled.

    A naive datetime from a form widget and an aware one from the backend can denote the
    same moment; comparing them with ``==`` raises or returns False depending on which
    side is which. Naive values are read as UTC, which is what every timestamp crossing
    this plugin's wire already is.
    """
    if left is None or right is None:
        return left is right
    return _utc(left) == _utc(right)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def _human(value: datetime | None) -> str:
    """A date a person reads, not an instant a machine parses.

    Deliberately drops the time unless it is not midnight. Satellite acquisition times
    carry a meaningless-looking 03:40:14, and showing it in a form invites the analyst to
    wonder whether the seconds matter. They do not, for the question being asked.
    """
    if value is None:
        return "—"
    moment = _utc(value)
    if (moment.hour, moment.minute, moment.second) == (0, 0, 0):
        return moment.strftime("%-d %b %Y")
    return moment.strftime("%-d %b %Y %H:%M")
