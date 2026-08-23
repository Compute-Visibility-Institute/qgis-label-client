"""The bootstrap publish: what will be sent, and what the person sending it is claiming.

WHY THIS EXISTS AT ALL

The first user of the platform has an empty backend and 1,246 features in Esri Shapefiles
that have been version-controlled by copying a dated folder. Until now the only way to get
them in was ``tools/load_snapshot.py`` -- a command-line loader against a PostgreSQL DSN,
which is a thing an analyst will never run. Moving it into the plugin is the whole point:
the replacement has to be no slower than copying a folder, or people keep copying folders.

WHAT IS PURE HERE AND WHY

Everything except talking to QGIS. The class guess, the field mapping, the geometry
reshaping, the plan and the report are all decided in this module against plain
dictionaries, because this is where a silent mistake is permanent. A bad OAPIF URI fails
loudly on the next request; a feature published under the wrong class, or with a name
missing its final character, or without a survey extent, is indistinguishable from a
correct one the moment it lands.

THE THREE CLAIMS A PUBLISH MAKES

1. **These features are of this class.** Guessed from the layer name, confirmed by a human,
   never inferred silently -- see :mod:`.legacy`.
2. **These names are the names.** At least 52% of the Chinese names in the source have lost
   their final character to a truncated UTF-7 escape run. Publishing them makes the damage
   authoritative -- see :mod:`.names`.
3. **This is where somebody looked.** The one nobody remembers to make. 872 cooling units
   sit on a single 1.0 x 0.8 km campus and 187 other compounds have none in the data; with
   no ``labeled_extent`` the export pipeline cannot tell "none here" from "nobody looked",
   and every unlabeled unit at those 187 sites becomes supervised background. The plan
   states this claim explicitly so it is refused deliberately rather than by omission.

Nothing here invents an identity. ``label.label_id`` is ``uuid DEFAULT gen_random_uuid()``
and the server assigns it; the source's own ``id`` column is 0% populated across all 1,246
features and is not a fallback, it is the defect being fixed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .fields import DEFAULT_FIELDS, CoreFields
from .legacy import ClassGuess, FieldMapping, build_attrs, guess_class, map_fields, name_entries
from .names import NameSet, build_names
from .registry import ClassRegistry, LabelClass

#: Layer custom property recording that this layer has already been published, and what
#: was sent. Lives on the layer rather than in settings so it travels with the project
#: file -- the thing the analyst actually copies between machines.
PUBLISHED_PROPERTY = "cvi/published"

#: ``label_class.geom_type`` value meaning the class accepts anything.
ANY_GEOMETRY = "Any"

#: Storage CRS. Everything is reprojected to it before publishing; nothing is *measured*
#: in it, which would produce degrees and is the mistake finding 7 of the practice
#: analysis exists to prevent.
STORAGE_CRS = "EPSG:4326"

_SINGLE_TO_MULTI = {
    "Point": "MultiPoint",
    "LineString": "MultiLineString",
    "Polygon": "MultiPolygon",
}
_MULTI_TO_SINGLE = {multi: single for single, multi in _SINGLE_TO_MULTI.items()}


# ---------------------------------------------------------------------------
# Idempotency: what happened here last time
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishRecord:
    """What a previous publish of this layer sent.

    A bootstrap is a one-way door and re-running it is the obvious thing to do after a
    partial failure, so "have I already sent this?" has to have an answer that is visible
    before the second run rather than discoverable after it.

    This is a *warning* mechanism, not a deduplicating one. The server assigns identity, so
    the plugin cannot tell whether a given feature already exists; what it can do is refuse
    to let a second publish be an accident.
    """

    published_at: str = ""
    collection_id: str = ""
    class_id: str = ""
    feature_count: int = 0

    def describe(self) -> str:
        when = self.published_at or "at an unrecorded time"
        where = f" to {self.collection_id}" if self.collection_id else ""
        return (
            f"Already published{where} {when}: {self.feature_count} feature(s)"
            f"{f' as {self.class_id}' if self.class_id else ''}. Publishing again "
            "creates a SECOND copy - the server assigns identity, so nothing here can "
            "recognise the first."
        )


def parse_record(raw: Any) -> PublishRecord | None:
    """Read a :data:`PUBLISHED_PROPERTY` value back.

    Anything unreadable is treated as "no record" rather than as an error: a corrupt
    custom property must not block a publish, and the warning it would have produced is
    the only thing lost.
    """
    if isinstance(raw, PublishRecord):
        return raw
    if not raw or not isinstance(raw, str):
        return None
    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(document, Mapping):
        return None
    count = document.get("feature_count")
    return PublishRecord(
        published_at=str(document.get("published_at") or ""),
        collection_id=str(document.get("collection_id") or ""),
        class_id=str(document.get("class_id") or ""),
        feature_count=int(count) if isinstance(count, (int, float)) else 0,
    )


def format_record(record: PublishRecord) -> str:
    """Serialise a record for storage in a layer custom property."""
    return json.dumps(
        {
            "published_at": record.published_at,
            "collection_id": record.collection_id,
            "class_id": record.class_id,
            "feature_count": record.feature_count,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def conform_geometry(
    geometry: Mapping[str, Any] | None,
    want: str,
) -> tuple[Mapping[str, Any] | None, str | None]:
    """Reshape a GeoJSON geometry to the type the class declares.

    The server compares ``ST_GeometryType`` against ``label_class.geom_type`` exactly, so a
    Polygon offered to a MultiPolygon class is rejected outright -- and shapefiles hand back
    both spellings for the same drawing depending on the layer.

    Promotion is lossless and is done silently. Demotion is attempted only for a
    single-part multi-geometry, where it is also lossless; a genuine multi-part geometry
    offered to a single-part class is a modelling disagreement and is refused rather than
    quietly losing parts.
    """
    if geometry is None:
        return None, "no geometry"
    have = geometry.get("type")
    if not isinstance(have, str):
        return None, "geometry has no type"
    if want == ANY_GEOMETRY or have == want:
        return geometry, None
    if _SINGLE_TO_MULTI.get(have) == want:
        return {"type": want, "coordinates": [geometry.get("coordinates")]}, None
    if _MULTI_TO_SINGLE.get(have) == want:
        parts = geometry.get("coordinates") or []
        if len(parts) == 1:
            return {"type": want, "coordinates": parts[0]}, None
        return None, (
            f"{have} with {len(parts)} parts cannot become {want} without discarding geometry"
        )
    return None, f"cannot reshape {have} into {want}"


def was_promoted(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> bool:
    """True when :func:`conform_geometry` changed the geometry's type."""
    if not before or not after:
        return False
    return before.get("type") != after.get("type")


# ---------------------------------------------------------------------------
# One feature
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureDraft:
    """One feature, ready to POST. No id: the server assigns identity."""

    class_id: str
    geometry: Mapping[str, Any]
    names: Mapping[str, str] = field(default_factory=dict)
    attrs: Mapping[str, Any] = field(default_factory=dict)

    def to_geojson(self, fields: CoreFields = DEFAULT_FIELDS) -> dict[str, Any]:
        """The GeoJSON Feature an OGC API - Features Part 4 create takes.

        ``names`` and ``attrs`` are omitted when empty rather than sent as ``{}``: both
        columns default to an empty object server-side, and an omitted key cannot be
        mistaken for a client asserting emptiness.
        """
        properties: dict[str, Any] = {fields.class_id: self.class_id}
        if self.names:
            properties[fields.names] = dict(self.names)
        if self.attrs:
            properties[fields.attrs] = dict(self.attrs)
        return {
            "type": "Feature",
            "geometry": dict(self.geometry),
            "properties": properties,
        }


@dataclass(frozen=True)
class DraftResult:
    """A drafted feature, or the reason there is not one."""

    draft: FeatureDraft | None = None
    issues: tuple[str, ...] = ()
    #: Set when the geometry had to be promoted to the class's multi-part type.
    promoted: bool = False
    #: Language keys whose name carries the UTF-7 truncation signature.
    damaged_names: tuple[str, ...] = ()
    #: Damaged names left out because the plan asked for that.
    omitted_names: tuple[str, ...] = ()

    @property
    def published(self) -> bool:
        return self.draft is not None


def build_draft(
    values: Mapping[str, Any],
    geometry: Mapping[str, Any] | None,
    label_class: LabelClass,
    mappings: Sequence[FieldMapping],
    *,
    skip_damaged_names: bool = False,
) -> DraftResult:
    """Turn one source feature into a :class:`FeatureDraft`.

    Geometry first: a feature that cannot be reshaped is not published, and finding that
    out before the attributes are built keeps the issue list about the actual blocker.
    """
    conformed, problem = conform_geometry(geometry, label_class.geom_type)
    if problem or conformed is None:
        return DraftResult(issues=(f"geometry: {problem}",))

    names: NameSet = build_names(name_entries(values, mappings), skip_damaged=skip_damaged_names)
    attributes = build_attrs(values, mappings, label_class)

    return DraftResult(
        draft=FeatureDraft(
            class_id=label_class.class_id,
            geometry=conformed,
            names=names.names,
            attrs=attributes.attrs,
        ),
        issues=attributes.issues,
        promoted=was_promoted(geometry, conformed),
        damaged_names=names.damaged,
        omitted_names=names.omitted,
    )


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceLayer:
    """The subset of a local vector layer this module reasons about."""

    layer_id: str
    name: str
    #: GeoJSON-style geometry type: Point, LineString, Polygon or a Multi* form.
    geometry_type: str = ""
    crs_authid: str = ""
    feature_count: int = 0
    field_names: tuple[str, ...] = ()
    #: Features whose name carries the UTF-7 truncation signature, counted by a scan of
    #: the name columns before anything is sent.
    damaged_names: int = 0
    #: How many features that scan actually read. Less than :attr:`feature_count` means
    #: the count above is a floor, not a total.
    scanned: int = 0
    previous: PublishRecord | None = None

    @property
    def needs_reprojection(self) -> bool:
        return bool(self.crs_authid) and self.crs_authid.upper() != STORAGE_CRS


@dataclass(frozen=True)
class LayerChoice:
    """What the user decided about one layer in the preview dialog."""

    layer_id: str
    publish: bool = False
    class_id: str | None = None
    #: Declare a ``labeled_extent`` for this class from the layer's bounding box.
    #: Defaults off, and stays off unless a human ticks it: a bounding box is a claim
    #: about where somebody looked, and only a human can make that claim honestly.
    declare_extent: bool = False
    #: Omit names carrying the truncation signature instead of publishing them.
    skip_damaged_names: bool = False


@dataclass(frozen=True)
class LayerPlan:
    """One layer, the class it will be published as, and how its columns map."""

    source: SourceLayer
    choice: LayerChoice
    label_class: LabelClass | None = None
    mappings: tuple[FieldMapping, ...] = ()
    guess: ClassGuess = field(default_factory=ClassGuess)

    @property
    def publish(self) -> bool:
        return self.choice.publish and self.label_class is not None

    @property
    def class_id(self) -> str:
        return self.label_class.class_id if self.label_class else ""

    def problems(self) -> tuple[str, ...]:
        """Reasons this layer cannot be published as chosen. Blocking."""
        if not self.choice.publish:
            return ()
        if self.label_class is None:
            return (f"{self.source.name}: no class chosen.",)
        if not self.label_class.active:
            return (
                f"{self.source.name}: class {self.label_class.class_id!r} is retired and "
                "the server refuses new labels on it.",
            )
        if not self.source.feature_count:
            return (f"{self.source.name}: no features to publish.",)
        return ()

    def notes(self) -> tuple[str, ...]:
        """Things worth saying before publishing. Advisory, never blocking."""
        notes: list[str] = []
        if self.source.previous is not None:
            notes.append(self.source.previous.describe())
        if self.source.needs_reprojection:
            notes.append(f"{self.source.crs_authid} will be reprojected to {STORAGE_CRS}.")
        if self.source.damaged_names:
            floor = "at least " if self.source.scanned < self.source.feature_count else ""
            action = "omitted" if self.choice.skip_damaged_names else "PUBLISHED AS THEY ARE"
            notes.append(
                f"{floor}{self.source.damaged_names} name(s) have lost their final "
                f"character to the UTF-7 truncation; they will be {action}."
            )
        if self.guess.tied_with:
            notes.append(self.guess.describe())
        unmapped = [m.describe() for m in self.mappings if m.target is None]
        if unmapped:
            notes.append(
                f"{len(unmapped)} column(s) map to nothing in this class's schema: "
                + "; ".join(unmapped)
            )
        return tuple(notes)


@dataclass(frozen=True)
class PublishPlan:
    """Every layer considered, and the aggregate claims the publish would make."""

    layers: tuple[LayerPlan, ...] = ()
    registry: ClassRegistry | None = None

    def __len__(self) -> int:
        return len(self.layers)

    def __iter__(self):
        return iter(self.layers)

    def selected(self) -> tuple[LayerPlan, ...]:
        return tuple(plan for plan in self.layers if plan.publish)

    def total_features(self) -> int:
        return sum(plan.source.feature_count for plan in self.selected())

    def damaged_name_count(self) -> int:
        return sum(plan.source.damaged_names for plan in self.selected())

    def republished(self) -> tuple[LayerPlan, ...]:
        """Selected layers that have been published before."""
        return tuple(plan for plan in self.selected() if plan.source.previous is not None)

    def classes_without_extent(self) -> tuple[str, ...]:
        """Classes being published for which this run declares no survey extent.

        Not "no extent exists anywhere" -- this plugin cannot know that without asking the
        server, and on a bootstrap into an empty backend the answer is the same either way.
        It is the narrower and always-true statement: *this* publish records what was found
        and not where anybody looked.
        """
        declared = {plan.class_id for plan in self.selected() if plan.choice.declare_extent}
        return tuple(
            sorted(
                {plan.class_id for plan in self.selected() if plan.class_id}
                - {class_id for class_id in declared if class_id}
            )
        )

    def problems(self) -> tuple[str, ...]:
        return tuple(problem for plan in self.layers for problem in plan.problems())

    def summary(self) -> str:
        """One sentence stating what is about to happen."""
        chosen = self.selected()
        if not chosen:
            return "Nothing selected. No features will be published."
        classes = sorted({plan.class_id for plan in chosen})
        return (
            f"{self.total_features()} feature(s) from {len(chosen)} layer(s), "
            f"as {len(classes)} class(es): {', '.join(classes)}."
        )


def build_plan(
    sources: Iterable[SourceLayer],
    registry: ClassRegistry,
    choices: Mapping[str, LayerChoice] | None = None,
) -> PublishPlan:
    """Build the plan the preview dialog renders and the task executes.

    Where the caller has made no choice, the class is guessed from the layer name and the
    layer is pre-selected -- except when it has been published before, which flips the
    default off. The user has to reach for the checkbox to publish a second copy.
    """
    decisions = dict(choices or {})
    plans: list[LayerPlan] = []

    for source in sources:
        choice = decisions.get(source.layer_id)
        guess = guess_class(source.name, registry)
        if choice is None:
            choice = LayerChoice(
                layer_id=source.layer_id,
                publish=source.previous is None and guess.confident,
                class_id=guess.class_id,
            )
        label_class = registry.get(choice.class_id) if choice.class_id else None
        mappings = map_fields(source.field_names, label_class) if label_class else ()
        plans.append(
            LayerPlan(
                source=source,
                choice=choice,
                label_class=label_class,
                mappings=mappings,
                guess=guess,
            )
        )

    return PublishPlan(layers=tuple(plans), registry=registry)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass
class LayerOutcome:
    """What actually happened to one layer. Mutated by the worker as it goes."""

    layer_name: str
    class_id: str
    read: int = 0
    published: int = 0
    failed: int = 0
    skipped_no_geometry: int = 0
    skipped_invalid_geometry: int = 0
    skipped_unshapeable: int = 0
    promoted: int = 0
    reprojected: bool = False
    damaged_names: int = 0
    omitted_names: int = 0
    extent_declared: bool = False
    extent_problem: str = ""
    issues: dict[str, int] = field(default_factory=dict)

    def note(self, message: str) -> None:
        """Record one issue, deduplicated with a count.

        A 1,246-feature run produces the same complaint hundreds of times. An undeduplicated
        list is not a report, it is a wall, and a wall is not read.
        """
        self.issues[message] = self.issues.get(message, 0) + 1

    @property
    def skipped(self) -> int:
        return self.skipped_no_geometry + self.skipped_invalid_geometry + self.skipped_unshapeable

    def line(self) -> str:
        bits = [f"{self.layer_name} -> {self.class_id}: {self.published} published"]
        if self.failed:
            bits.append(f"{self.failed} rejected by the server")
        if self.skipped:
            bits.append(f"{self.skipped} skipped")
        if self.promoted:
            bits.append(f"{self.promoted} promoted to multi-part")
        if self.reprojected:
            bits.append(f"reprojected to {STORAGE_CRS}")
        if self.omitted_names:
            bits.append(f"{self.omitted_names} damaged name(s) omitted")
        elif self.damaged_names:
            bits.append(f"{self.damaged_names} damaged name(s) published as-is")
        if self.extent_declared:
            bits.append("survey extent declared")
        return ", ".join(bits) + "."


@dataclass
class PublishReport:
    """Everything the summary dialog needs to say something true and actionable."""

    outcomes: list[LayerOutcome] = field(default_factory=list)
    cancelled: bool = False
    #: Classes published in this run with no ``labeled_extent`` declared for them.
    classes_without_extent: tuple[str, ...] = ()

    def outcome_for(self, layer_name: str, class_id: str) -> LayerOutcome:
        outcome = LayerOutcome(layer_name=layer_name, class_id=class_id)
        self.outcomes.append(outcome)
        return outcome

    @property
    def published(self) -> int:
        return sum(outcome.published for outcome in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(outcome.failed for outcome in self.outcomes)

    @property
    def skipped(self) -> int:
        return sum(outcome.skipped for outcome in self.outcomes)

    @property
    def damaged_names(self) -> int:
        return sum(outcome.damaged_names for outcome in self.outcomes)

    @property
    def clean(self) -> bool:
        return not self.failed and not self.skipped and not self.cancelled

    def summary(self) -> str:
        if self.cancelled:
            return (
                f"Cancelled after publishing {self.published} feature(s). What was already "
                "sent is on the server; re-running publishes the rest AND a second copy of "
                "these, because the server assigns identity."
            )
        bits = [f"{self.published} feature(s) published"]
        if self.failed:
            bits.append(f"{self.failed} rejected by the server")
        if self.skipped:
            bits.append(f"{self.skipped} skipped before sending")
        return ", ".join(bits) + "."

    def detail_lines(self) -> list[str]:
        lines = [outcome.line() for outcome in self.outcomes]
        for outcome in self.outcomes:
            if outcome.extent_problem:
                lines.append(f"{outcome.layer_name}: {outcome.extent_problem}")
            for message, count in sorted(outcome.issues.items(), key=lambda kv: -kv[1]):
                suffix = f"  [x{count}]" if count > 1 else ""
                lines.append(f"{outcome.layer_name}: {message}{suffix}")
        return lines

    def coverage_warning(self) -> str:
        """The sentence the loader printed in a terminal that nobody ran.

        Stated in consequences rather than counts, because the consequence is the part that
        is not obvious: unlabeled ground outside a declared extent is UNKNOWN to the export
        pipeline, and treating it as negative is what teaches a detector that cooling units
        are not cooling units.
        """
        if not self.classes_without_extent:
            return ""
        return (
            "These classes were published with no labeled_extent declared for them:\n  - "
            + "\n  - ".join(self.classes_without_extent)
            + "\n\nYou have recorded WHAT was found. Nothing yet records WHERE ANYONE "
            "LOOKED, and those are different facts. Ground outside a declared exhaustive "
            "extent is UNKNOWN to the export pipeline, never negative.\n\n"
            "This cannot be reconstructed later: the knowledge is in the surveyor's memory "
            "and it decays weekly. Ask now which sites were exhaustively swept, for which "
            "classes, on what date, and against which imagery capture."
        )
