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

THE FOUR CLAIMS A PUBLISH MAKES

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
4. **This is the dataset they belong in.** The newest one, and the only one of the four
   whose failure the annotator can see coming: a publish into the wrong history track puts
   1,246 features into the dataset the analysts are building, or -- worse, because nobody
   would look -- into the one somebody was kicking the tyres with. Neither can be undone:
   identity is server-assigned, so nothing here can find those rows again. So the track is
   in the plan, in the preview, in the confirmation and on the stamp, and a plan with no
   track selected is *blocking* rather than merely worrying.

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
from .legacy import (
    ClassGuess,
    FieldMapping,
    FieldRole,
    build_attrs,
    guess_class,
    map_fields,
    name_columns,
    name_entries,
)
from .names import NameSet, build_names
from .registry import ClassRegistry, LabelClass
from .routing import DIMENSION_SUFFIXES, CollectionRoutes, base_geometry_type
from .tracks import Track

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

#: Ordinates per position in storage. ``label.geom`` is ``geometry(Geometry, 4326)``, and
#: a PostGIS typmod fixes dimensionality as well as SRID: a three-ordinate position is
#: refused with "Geometry has 3 dimensions but column has 2". Esri tooling writes
#: Z-enabled shapefiles by default and the QGIS layer looks identical, so without
#: :func:`strip_elevation` a publish of one fails on every single row with a message that
#: names neither the layer nor the fix.
STORAGE_DIMENSIONS = 2

_SINGLE_TO_MULTI = {
    "Point": "MultiPoint",
    "LineString": "MultiLineString",
    "Polygon": "MultiPolygon",
}
_MULTI_TO_SINGLE = {multi: single for single, multi in _SINGLE_TO_MULTI.items()}

#: Geometry type names this module can reason about before seeing a feature. Anything else
#: -- a curve type, or a layer OGR reports as "Unknown (any)" -- is decided per feature by
#: :func:`conform_geometry` instead of being pre-judged in the preview.
_KNOWN_GEOMETRY_TYPES = frozenset(_SINGLE_TO_MULTI) | frozenset(_MULTI_TO_SINGLE)


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
    #: The history track it went to. Empty on a record written before tracks existed, and
    #: that emptiness is honest: nobody recorded it, so nothing here may claim one.
    track: str = ""

    def describe(self) -> str:
        when = self.published_at or "at an unrecorded time"
        where = f" to {self.collection_id}" if self.collection_id else ""
        # The track goes in the same sentence as the count, not in a footnote. A second
        # publish is only obviously wrong when you can see it would land somewhere else --
        # and "already published, 872 features" reads as reassurance until you notice the
        # dataset named is not the one you have selected.
        track = f" on track {self.track!r}" if self.track else ""
        return (
            f"Already published{where}{track} {when}: {self.feature_count} feature(s)"
            f"{f' as {self.class_id}' if self.class_id else ''}. Publishing again "
            "creates a SECOND copy - the server assigns identity, so nothing here can "
            "recognise the first."
        )

    def on_another_track(self, track: str) -> bool:
        """True when this layer was last published somewhere other than `track`.

        Not a problem in itself -- publishing the same shapefile into two tracks is a
        perfectly reasonable thing to do, and is exactly what setting up a test dataset
        looks like. It is worth *saying*, because the "you have published this before"
        warning otherwise implies a duplicate that would not actually be one.
        """
        return bool(self.track) and self.track != track


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
        # Absent in a record written before tracks existed. Read as "" rather than as the
        # current track: nobody recorded which dataset those features went to, and
        # inventing an answer here would be a claim the data cannot support.
        track=str(document.get("track") or ""),
    )


def format_record(record: PublishRecord) -> str:
    """Serialise a record for storage in a layer custom property."""
    return json.dumps(
        {
            "published_at": record.published_at,
            "collection_id": record.collection_id,
            "class_id": record.class_id,
            "feature_count": record.feature_count,
            "track": record.track,
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


def _is_position(value: Any) -> bool:
    """True for a GeoJSON position -- a flat list of ordinates rather than a nesting."""
    return (
        isinstance(value, (list, tuple))
        and bool(value)
        and isinstance(value[0], (int, float))
        and not isinstance(value[0], bool)
    )


def has_elevation(coordinates: Any) -> bool:
    """True when any position in a GeoJSON coordinate tree carries a third ordinate."""
    if not isinstance(coordinates, (list, tuple)):
        return False
    if _is_position(coordinates):
        return len(coordinates) > STORAGE_DIMENSIONS
    return any(has_elevation(part) for part in coordinates)


def _flatten(coordinates: Any) -> Any:
    if not isinstance(coordinates, (list, tuple)):
        return coordinates
    if _is_position(coordinates):
        return list(coordinates[:STORAGE_DIMENSIONS])
    return [_flatten(part) for part in coordinates]


def strip_elevation(
    geometry: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any] | None, bool]:
    """Drop any third ordinate, returning ``(geometry, dropped)``.

    Not a modelling choice: see :data:`STORAGE_DIMENSIONS`. The schema has nowhere to put
    an elevation, so the alternative to dropping it is every row of a Z-enabled layer
    being refused by PostGIS. It is *counted* and reported rather than done quietly,
    because the analyst may not know their shapefile carries a Z at all.
    """
    if geometry is None:
        return None, False
    coordinates = geometry.get("coordinates")
    if not has_elevation(coordinates):
        return geometry, False
    return {**geometry, "coordinates": _flatten(coordinates)}, True


def geometry_mismatch(source_type: str, want: str) -> str | None:
    """Why a layer of `source_type` could publish nothing as a class wanting `want`.

    Answered from the layer's declared WKB type, before a single feature is read, because
    the alternative is a run that reads 872 features, sends none of them and explains why
    afterwards. A type this module cannot place -- a curve, or the "Unknown (any)" OGR
    reports for a mixed layer -- yields ``None`` and is decided per feature instead; a
    guess that blocks a legitimate publish is worse than a run that reports skips.
    """
    have = base_geometry_type(source_type)
    if want == ANY_GEOMETRY or have not in _KNOWN_GEOMETRY_TYPES:
        return None
    if have == want or _SINGLE_TO_MULTI.get(have) == want:
        return None
    if _MULTI_TO_SINGLE.get(have) == want:
        # Lossless for the single-part features that dominate shapefile "multi" layers,
        # refused per feature for the rest. A note, not a blocker.
        return None
    return f"holds {have} geometry but this class stores {want}; no feature could be published"


def demotion_warning(source_type: str, want: str) -> str | None:
    """Whether publishing this layer as `want` means discarding multi-part features."""
    have = base_geometry_type(source_type)
    if have in _KNOWN_GEOMETRY_TYPES and _MULTI_TO_SINGLE.get(have) == want:
        return (
            f"{have} features with more than one part cannot become {want} without "
            "discarding geometry; those are skipped and reported."
        )
    return None


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
    #: Set when a third ordinate was dropped so PostGIS would accept the geometry.
    flattened: bool = False
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
    # After the reshape, so a promoted geometry's freshly nested coordinates are covered
    # by the same pass.
    conformed, flattened = strip_elevation(conformed)

    names: NameSet = build_names(name_entries(values, mappings), skip_damaged=skip_damaged_names)
    attributes = build_attrs(values, mappings, label_class)

    return DraftResult(
        draft=FeatureDraft(
            class_id=label_class.class_id,
            geometry=conformed,
            names=names.names,
            attrs=attributes.attrs,
        ),
        # A dropped name is data loss, exactly like a refused attribute, and belongs in
        # the same list rather than in the silence between two dictionary writes.
        issues=attributes.issues + names.collisions,
        promoted=was_promoted(geometry, conformed),
        flattened=flattened,
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
    #: False when QGIS could not work out what the coordinates mean -- a shapefile with
    #: no ``.prj`` beside it, most often. See :meth:`LayerPlan.problems`.
    crs_valid: bool = True
    feature_count: int = 0
    #: False when the provider answered "I cannot tell you yet" -- QGIS returns -1 from
    #: ``featureCount()`` for providers that do not know in advance. Collapsing that into
    #: a count of zero turns "unknown" into "empty", and :meth:`LayerPlan.problems` makes
    #: empty a blocker: the Publish button greys out against a layer visibly full of
    #: features, with nothing on the screen explaining the contradiction.
    count_known: bool = True
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
        """True when the coordinates have to be moved before they can be stored.

        Keyed on the CRS being *valid* rather than on it having an authority code. A CRS
        defined only by WKT -- which QGIS reports with an empty ``authid()`` -- is a real
        CRS that is almost certainly not EPSG:4326, and reading an empty authid as "no
        reprojection needed" would publish its coordinates verbatim.
        """
        return self.crs_valid and self.crs_authid.upper() != STORAGE_CRS

    @property
    def has_elevation(self) -> bool:
        """True when the layer's WKB type carries a Z or M ordinate.

        Read from the declared type rather than from the features, because it is needed
        in the preview -- see :data:`STORAGE_DIMENSIONS` for why it matters at all.
        """
        return self.geometry_type.endswith(DIMENSION_SUFFIXES)


@dataclass(frozen=True)
class LayerChoice:
    """What the user decided about one layer in the preview dialog."""

    layer_id: str
    publish: bool = False
    class_id: str | None = None
    #: ``labeled_extent.completeness`` to declare for this layer's bounding box, or ``""``
    #: to declare nothing. Empty by default, and stays empty unless a human picks a value.
    #:
    #: A single value rather than a flag plus a constant, because the two halves of this
    #: claim are not separable. ``exhaustive`` is the only value that licenses the export
    #: pipeline to treat unlabeled ground inside the polygon as negative, and it is the
    #: whole reason the collection was read-only until this workflow existed. A tool that
    #: writes it whenever a checkbox is ticked has not asked the question; it has answered
    #: it, in the direction that poisons every model trained from the result.
    extent_completeness: str = ""
    #: Omit names carrying the truncation signature instead of publishing them.
    skip_damaged_names: bool = False

    @property
    def declare_extent(self) -> bool:
        """True when this layer will write a ``labeled_extent`` row."""
        return bool(self.extent_completeness)


@dataclass(frozen=True)
class LayerPlan:
    """One layer, the class it will be published as, and how its columns map."""

    source: SourceLayer
    choice: LayerChoice
    label_class: LabelClass | None = None
    mappings: tuple[FieldMapping, ...] = ()
    guess: ClassGuess = field(default_factory=ClassGuess)
    #: The collection this layer's features would be created in, chosen from its geometry
    #: type -- see :mod:`.routing`. Empty when the caller supplied no routes, which is not
    #: the same as "nowhere": it means nobody asked, and the publish path always does.
    collection_id: str = ""
    #: Why this layer has no collection to go to, or ``""``. Blocking; see
    #: :meth:`problems`.
    routing_problem: str = ""

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
        if self.source.count_known and not self.source.feature_count:
            return (f"{self.source.name}: no features to publish.",)
        if not self.source.crs_valid:
            # Blocking, and the only geometry problem here that is. A reprojection needs
            # a source CRS; without one QGIS builds a transform that silently does
            # nothing, so projected metres would be stored as if they were degrees of
            # longitude. label.geom has no range check to catch that, ST_GeometryType
            # still matches the class, and the features land somewhere in the Gulf of
            # Guinea looking exactly like valid data.
            return (
                f"{self.source.name}: QGIS does not know what this layer's coordinates "
                "mean - there is no valid CRS, which usually means a shapefile with no "
                ".prj beside it. Set it in Layer Properties > Source before publishing; "
                "guessing here would store the numbers as if they were degrees.",
            )
        # Checked here rather than discovered per feature: the class combo is the one
        # control on this screen that can be wrong in a way that publishes nothing at
        # all, and the preview exists to say so before the run rather than after it.
        mismatch = geometry_mismatch(self.source.geometry_type, self.label_class.geom_type)
        if mismatch:
            return (f"{self.source.name} {mismatch}.",)
        # Last, because it is the least specific of the geometry complaints: a point layer
        # set to a polygon class is told about the class first, which is the thing the
        # person can fix on this screen. Blocking all the same -- a layer with no
        # collection to go to would otherwise be sent to whichever one the request
        # happened to name, and app.label_check() would refuse it feature by feature in a
        # report that reads like a server fault.
        if self.routing_problem:
            return (self.routing_problem,)
        return ()

    def mapping_lines(self) -> tuple[str, ...]:
        """Every source column and where its values would go, one line each.

        Shown in the preview rather than only in the report, because the matcher is
        structural: it maps a column onto whichever declared attribute its concept is a
        subset of, and it has no way to know that a particular column is *wrong* for
        reasons outside the schema. The source ``Area`` column is the standing example --
        it matches an area attribute perfectly well, and any value in it was computed in
        EPSG:4326 and is therefore square degrees in a field that means square metres. It
        is empty in every one of the 1,246 features today, which is luck rather than
        safety.

        The plugin cannot hold a list of columns to distrust; that list is exactly the
        second copy of the vocabulary the class registry exists to prevent. What it can do
        is put the whole mapping in front of the person publishing, which is the same
        answer this screen gives to damaged names and to missing survey coverage: not a
        rule, a visible choice.

        For that to work the line has to carry what the *registry* says about the target,
        not just its name. ``Area -> attribute area_m2`` looks correct and is exactly the
        mapping that is wrong; the sentence that gives it away -- "Computed in a projected
        CRS. NEVER in EPSG:4326." -- is already in the class's own schema, and dropping it
        here leaves the human with nothing to catch the mistake with. It costs nothing to
        show and it is not a second copy of anything: it arrives at runtime with the class.
        """
        lines: list[str] = []
        for mapping in self.mappings:
            line = mapping.describe()
            if (
                self.label_class is not None
                and mapping.role is FieldRole.ATTRIBUTE
                and mapping.target
            ):
                detail = self.label_class.attribute(mapping.target).summary()
                # summary() opens with the attribute name, which the line already has.
                detail = detail[len(mapping.target) :].strip()
                if detail:
                    line = f"{line}  {detail}"
            lines.append(line)
        return tuple(lines)

    def mapping_summary(self) -> str:
        """One cell's worth of "where do this layer's columns go?"."""
        if self.label_class is None:
            return ""
        counts: dict[str, int] = {}
        for mapping in self.mappings:
            key = "unmapped" if mapping.target is None else f"-> {mapping.role.value}"
            counts[key] = counts.get(key, 0) + 1
        if not counts:
            return "no columns"
        return ", ".join(f"{count} {key}" for key, count in sorted(counts.items()))

    def notes(self) -> tuple[str, ...]:
        """Things worth saying before publishing. Advisory, never blocking."""
        notes: list[str] = []
        if self.source.previous is not None:
            notes.append(self.source.previous.describe())
        if not self.source.count_known:
            notes.append(
                "This layer's provider does not report a feature count in advance, so the "
                "count shown is unknown rather than zero and the progress bar cannot move."
            )
        if self.source.needs_reprojection:
            notes.append(f"{self.source.crs_authid} will be reprojected to {STORAGE_CRS}.")
        if self.source.has_elevation:
            notes.append(
                "The geometries carry a third ordinate. Label geometry is stored in two "
                "dimensions, so it will be dropped; nothing else in the schema records it."
            )
        if self.label_class is not None:
            demotion = demotion_warning(self.source.geometry_type, self.label_class.geom_type)
            if demotion:
                notes.append(demotion)
        if not self.source.scanned and name_columns(self.source.field_names):
            # A scan that did not happen returns zero, and a zero on this screen reads as
            # "checked, and clean". Only the sentence below distinguishes the two, and the
            # difference is 81 names published as authoritative.
            notes.append(
                "The name columns were NOT scanned for the UTF-7 truncation, so nothing "
                "here says whether these names are damaged. Layers that do not come from "
                "a local file are left unscanned: reading them to count would freeze QGIS "
                "while the preview is built."
            )
        if self.source.damaged_names:
            floor = "at least " if self.source.scanned < self.source.feature_count else ""
            action = "omitted" if self.choice.skip_damaged_names else "PUBLISHED AS THEY ARE"
            notes.append(
                f"{floor}{self.source.damaged_names} name(s) look like they have lost "
                f"their final character to the UTF-7 truncation; they will be {action}. "
                "That is an upper bound, not a measurement: a two-character site "
                "designator after a Chinese character cannot be told apart from a cut "
                "escape run, so omitting these may destroy an intact name."
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
    #: Which history track everything below would land in. ``None`` means the plugin does
    #: not know, which is blocking -- see :meth:`track_problems`.
    track: Track | None = None

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

    def collections(self) -> tuple[str, ...]:
        """Every collection this publish would create features in, sorted.

        Plural, because the destination is now a property of each layer rather than of the
        run: one geometry family per collection means a project holding compounds, cooling
        units and powerlines writes to three of them in a single publish.
        """
        return tuple(sorted({plan.collection_id for plan in self.selected() if plan.collection_id}))

    def republished(self) -> tuple[LayerPlan, ...]:
        """Selected layers that have been published before."""
        return tuple(plan for plan in self.selected() if plan.source.previous is not None)

    def classes_without_extent(self) -> tuple[str, ...]:
        """Classes being published for which this run declares no survey extent.

        Not "no extent exists anywhere" -- this plugin cannot know that without asking the
        server, and on a bootstrap into an empty backend the answer is the same either way.
        It is the narrower and always-true statement: *this* publish records what was found
        and not where anybody looked.

        Asked per *layer*, not per class, and then reduced to class names. The claim an
        extent makes is about an area, so one layer's Ulanqab sweep says nothing about a
        second layer covering the rest of the country -- and subtracting class ids would
        let the first silence the warning for the second, which is the one place this
        warning is most needed.
        """
        return tuple(
            sorted(
                {
                    plan.class_id
                    for plan in self.selected()
                    if plan.class_id and not plan.choice.declare_extent
                }
            )
        )

    @property
    def track_name(self) -> str:
        return self.track.name if self.track else ""

    def track_problems(self) -> tuple[str, ...]:
        """Reasons this publish must not start at all, on track grounds. Blocking.

        Blocking rather than advisory, which is a departure from how this module treats
        every other *claim*: a missing survey extent is a warning, an unrecoverable one,
        and still only a warning. The difference is that the extent warning describes
        something the publish fails to say, and this describes the publish saying it in
        the wrong place. There is no version of "publish these 1,246 features into a
        dataset nobody has named" that is a defensible choice, and the person clicking
        cannot tell afterwards which one it went to -- so the button is off.
        """
        if self.track is None:
            return (
                "No history track is selected, so there is no way to say which dataset "
                "these features would join. Connect, then choose a track in the panel.",
            )
        if self.track.archived:
            return (
                f"Track {self.track.name!r} is archived. The database refuses every write "
                "to an archived track, so this publish would fail feature by feature "
                "after the first request. Choose an active track.",
            )
        return ()

    def republished_elsewhere(self) -> tuple[LayerPlan, ...]:
        """Selected layers last published to a *different* track.

        Split out from :meth:`republished` because the two need opposite sentences. A
        second publish to the same track duplicates rows; a first publish to a different
        track is how you seed a test dataset, and calling it a duplicate would train
        people to click through the warning that catches the real one.
        """
        return tuple(
            plan
            for plan in self.selected()
            if plan.source.previous is not None
            and plan.source.previous.on_another_track(self.track_name)
        )

    def problems(self) -> tuple[str, ...]:
        return self.track_problems() + tuple(
            problem for plan in self.layers for problem in plan.problems()
        )

    def track_claim(self) -> str:
        """The sentence naming the dataset this publish would create rows in.

        Rendered whether or not anything is wrong, and that is the point: the survey and
        damaged-name warnings appear only when there is something to warn about, so a
        clean preview has nothing on it that says where the features are going. This
        always does.
        """
        if self.track is None:
            return ""
        chosen = self.selected()
        count = self.total_features()
        where = self.track.describe()
        if not chosen:
            return f"Nothing selected. The track these would join is {where}."
        return (
            f"{count} feature(s) will be created on history track {where}. "
            "They join that dataset and no other, and this cannot be undone: the server "
            "assigns each one its identity, so nothing here can find them again to move "
            "or remove them."
        )

    def summary(self) -> str:
        """One sentence stating what is about to happen."""
        chosen = self.selected()
        if not chosen:
            return "Nothing selected. No features will be published."
        classes = sorted({plan.class_id for plan in chosen})
        where = f" on track {self.track_name}" if self.track_name else ""
        # The collections are in the one-line summary as well as in the table's own
        # column, because this sentence is what the confirmation and the status line
        # repeat. A publish now fans out across collections by geometry, and a summary
        # that named only the track would leave the fan-out visible nowhere but a column
        # the eye skips.
        collections = self.collections()
        into = f", into {', '.join(collections)}" if collections else ""
        return (
            f"{self.total_features()} feature(s) from {len(chosen)} layer(s), "
            f"as {len(classes)} class(es): {', '.join(classes)}{where}{into}."
        )


def build_plan(
    sources: Iterable[SourceLayer],
    registry: ClassRegistry,
    choices: Mapping[str, LayerChoice] | None = None,
    track: Track | None = None,
    routes: CollectionRoutes | None = None,
) -> PublishPlan:
    """Build the plan the preview dialog renders and the task executes.

    Where the caller has made no choice, the class is guessed from the layer name and the
    layer is pre-selected -- except when it has been published before, which flips the
    default off. The user has to reach for the checkbox to publish a second copy.

    A layer previously published to a *different* track keeps its pre-selection: sending
    the same shapefile into a second dataset is not a duplicate, it is how a test track
    gets populated. It is still announced -- see :meth:`PublishPlan.republished_elsewhere`.

    `routes` decides WHERE each layer goes, from its geometry type -- see :mod:`.routing`.
    It is optional so that the plan stays testable and renderable without a backend, and
    ``None`` means "nobody asked", not "nowhere": a plan built without routes states no
    destination and refuses nothing. The publish path always passes them, so the only way
    to reach a send is through a plan that named a collection per layer.
    """
    decisions = dict(choices or {})
    plans: list[LayerPlan] = []

    track_name = track.name if track else ""
    for source in sources:
        choice = decisions.get(source.layer_id)
        guess = guess_class(source.name, registry)
        if choice is None:
            already_here = source.previous is not None and not source.previous.on_another_track(
                track_name
            )
            choice = LayerChoice(
                layer_id=source.layer_id,
                publish=not already_here and guess.confident,
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
                collection_id=routes.collection_for(source.geometry_type) if routes else "",
                routing_problem=routes.refusal(source.name, source.geometry_type) if routes else "",
            )
        )

    return PublishPlan(layers=tuple(plans), registry=registry, track=track)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


#: How many named rows one deduplicated issue keeps. Enough to start looking with, far
#: short of the wall an undeduplicated list would be. See :meth:`LayerOutcome.note`.
MAX_ISSUE_SUBJECTS = 5


@dataclass
class Issue:
    """One distinct complaint, how often it happened, and to which rows.

    The count alone is what turns a partial run into a support ticket: "190 rejected" is a
    number, and the operator's next question -- *which* 190, and what is left to re-send --
    has no answer anywhere. The subjects are that answer, bounded so the report stays a
    report.
    """

    count: int = 0
    subjects: list[str] = field(default_factory=list)
    #: Subjects past :data:`MAX_ISSUE_SUBJECTS`, counted rather than listed.
    unnamed: int = 0

    def add(self, subject: str = "") -> None:
        self.count += 1
        if not subject:
            return
        if len(self.subjects) < MAX_ISSUE_SUBJECTS:
            self.subjects.append(subject)
        else:
            self.unnamed += 1

    def describe(self, message: str) -> str:
        line = message
        if self.count > 1:
            line += f"  [x{self.count}]"
        if self.subjects:
            named = "; ".join(self.subjects)
            more = f" and {self.unnamed} more" if self.unnamed else ""
            line += f"  ({named}{more})"
        return line


@dataclass
class LayerOutcome:
    """What actually happened to one layer. Mutated by the worker as it goes."""

    layer_name: str
    class_id: str
    #: Where this layer's features were sent. On the outcome rather than on the report,
    #: because one run now writes to several collections -- and after a partial run, "what
    #: still needs sending, and to where" is the only question the report has to answer.
    collection_id: str = ""
    #: Features the layer was expected to hold, from the plan. Zero when the provider
    #: could not say in advance.
    expected: int = 0
    read: int = 0
    published: int = 0
    failed: int = 0
    skipped_no_geometry: int = 0
    skipped_invalid_geometry: int = 0
    skipped_unshapeable: int = 0
    #: Drafted but never sent, because the run was cancelled between drafting and the
    #: POST. Distinct from :attr:`failed`: nobody refused these, so calling them failures
    #: would report a deliberate stop as a server problem.
    not_sent: int = 0
    promoted: int = 0
    #: Features whose third ordinate was dropped to fit the two-dimensional geom column.
    flattened: int = 0
    reprojected: bool = False
    damaged_names: int = 0
    omitted_names: int = 0
    extent_declared: bool = False
    extent_problem: str = ""
    issues: dict[str, Issue] = field(default_factory=dict)

    def note(self, message: str, subject: str = "") -> None:
        """Record one issue, deduplicated with a count and a few named rows.

        A 1,246-feature run produces the same complaint hundreds of times. An undeduplicated
        list is not a report, it is a wall, and a wall is not read. But a deduplicated
        *count* is not actionable either: a save here is not atomic and there is no ETag,
        so after a partial run the only way to know what still needs sending is to know
        which rows were refused. `subject` is that -- a name, or a position in the layer --
        and it is bounded so the wall does not come back.
        """
        issue = self.issues.get(message)
        if issue is None:
            issue = Issue()
            self.issues[message] = issue
        issue.add(subject)

    @property
    def skipped(self) -> int:
        return self.skipped_no_geometry + self.skipped_invalid_geometry + self.skipped_unshapeable

    @property
    def never_reached(self) -> int:
        """Features of this layer the run never even read, because it stopped first."""
        return max(0, self.expected - self.read)

    def line(self) -> str:
        into = f" in {self.collection_id}" if self.collection_id else ""
        bits = [f"{self.layer_name} -> {self.class_id}{into}: {self.published} published"]
        if self.failed:
            bits.append(f"{self.failed} rejected by the server")
        if self.skipped:
            bits.append(f"{self.skipped} skipped")
        if self.not_sent:
            bits.append(f"{self.not_sent} never sent (cancelled)")
        if self.never_reached:
            # Otherwise a stopped run reports only what it did, and how much of the layer
            # is still on this machine has to be remembered from the previous dialog.
            bits.append(f"{self.never_reached} never read (stopped first)")
        if self.promoted:
            bits.append(f"{self.promoted} promoted to multi-part")
        if self.flattened:
            bits.append(f"{self.flattened} flattened to two dimensions")
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
    #: The history track everything in this run went to. Carried on the report rather than
    #: looked up afterwards: by the time this is read the panel's selection may have
    #: changed, and the question the report answers is where these features actually
    #: landed, not where the next publish would go.
    track: str = ""
    #: Set when the run stopped on an error nobody anticipated. The counts above are still
    #: what reached the server, which is the only reason this is a field and not an
    #: exception: an unhandled failure on row 900 has already written 899 rows, and the
    #: user needs the report of those far more than they need a traceback in the log.
    error: str = ""

    def outcome_for(
        self,
        layer_name: str,
        class_id: str,
        expected: int = 0,
        collection_id: str = "",
    ) -> LayerOutcome:
        outcome = LayerOutcome(
            layer_name=layer_name,
            class_id=class_id,
            collection_id=collection_id,
            expected=expected,
        )
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
        return not self.failed and not self.skipped and not self.cancelled and not self.error

    @property
    def _where(self) -> str:
        """The ``on track X`` clause, or empty. Appended to every count this report states.

        A partial run is the case this matters for. "Cancelled after publishing 400
        feature(s)" leaves somebody deciding whether to re-run, and that decision is
        different depending on which dataset the 400 are in -- so the answer travels with
        the number rather than sitting in a different dialog.
        """
        return f" on track {self.track}" if self.track else ""

    def summary(self) -> str:
        if self.error:
            return (
                f"Stopped by an unexpected error after publishing {self.published} "
                f"feature(s){self._where}: {self.error} What was already sent is on the "
                "server; re-running publishes the rest AND a second copy of these, because "
                "the server assigns identity."
            )
        if self.cancelled:
            return (
                f"Cancelled after publishing {self.published} feature(s){self._where}. What "
                "was already sent is on the server; re-running publishes the rest AND a "
                "second copy of these, because the server assigns identity."
            )
        bits = [f"{self.published} feature(s) published{self._where}"]
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
            for message, issue in sorted(outcome.issues.items(), key=lambda kv: -kv[1].count):
                lines.append(f"{outcome.layer_name}: {issue.describe(message)}")
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
        where = f" on track {self.track}" if self.track else ""
        return (
            f"These classes were published{where} with no labeled_extent declared for "
            "them:\n  - "
            + "\n  - ".join(self.classes_without_extent)
            + "\n\nYou have recorded WHAT was found. Nothing yet records WHERE ANYONE "
            "LOOKED, and those are different facts. Ground outside a declared exhaustive "
            "extent is UNKNOWN to the export pipeline, never negative.\n\n"
            "This cannot be reconstructed later: the knowledge is in the surveyor's memory "
            "and it decays weekly. Ask now which sites were exhaustively swept, for which "
            "classes, on what date, and against which imagery capture."
        )
