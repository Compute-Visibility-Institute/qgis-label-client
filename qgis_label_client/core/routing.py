"""Choosing WHICH collection a layer's features are created in.

WHY THERE IS MORE THAN ONE COLLECTION TO CHOOSE BETWEEN

``label.geom`` was ``geometry(Geometry, 4326)`` -- deliberately untyped, because a cooling
unit is a Point and a powerline is a LineString. OGC API - Features has no way to declare a
geometry type on a collection, so pygeoapi publishes ``{"format": "geometry-any"}`` in
``/collections/label/schema``, which is honest and useless: QGIS infers a layer's geometry
type by SAMPLING features, an empty collection has nothing to sample, and a layer QGIS
believes has no geometry offers "Add Record" where a polygon layer offers "Add Polygon
Feature". Every digitizing tool disappears -- on a deployment whose label collection is
empty, which is every deployment on its first day, and the day the tools are most needed.

The backend's answer is one typed collection per geometry family. That moves a decision
here: a QGIS vector layer has exactly one geometry type, so a polygon layer publishes to
the polygon collection and a point layer to the point one, and the publish path has to
work out which is which before it sends anything.

WHAT THIS MODULE REFUSES TO HARDCODE, AND WHY

The collection ids. The standing rule in this plugin is that collection ids and class
vocabulary come from the backend at runtime -- see :mod:`.collections`, and the hygiene
tests that enforce the class half of it. A deployment that names its collections something
else is a deployment, not a bug, and a plugin that only works against ids compiled into it
turns every rename into a release. So the routes are resolved against the ids
``/collections`` actually lists, by reading the geometry word out of each id, and a
deployment offering something this cannot place is refused with a sentence rather than
routed on a guess.

CLASS STAYS AN ATTRIBUTE, NEVER A LAYER

There are three geometry families and there will be three no matter how many classes the
registry grows. Adding a class remains one row in ``label_class``: no migration, no new
collection, no plugin release. Nothing here keys on a class id and nothing here may start
to -- the split is by geometry, of which there is a closed set, not by class, of which
there is not.

WHY A WRONG ROUTE IS WORSE THAN NO ROUTE

``app.label_check()`` compares ``ST_GeometryType`` against the collection's own column
type, feature by feature. A point layer sent to the polygon collection is refused 872
times, each refusal costing a round trip, and the report that comes back reads like a
backend outage rather than like a routing mistake. So an unroutable layer is refused
before anything is sent, by name and by geometry type.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

#: WKB display-string suffixes marking extra ordinates: ``PolygonZ``, ``PointZM``.
#:
#: Public, and read by :mod:`.publish` as well: the suffix is what tells that module a
#: layer carries a third ordinate the two-dimensional ``geom`` column has nowhere to put,
#: and one spelling of "this is a dimensionality suffix" is enough for both.
DIMENSION_SUFFIXES = ("Z", "M")


def base_geometry_type(name: str) -> str:
    """A WKB display string with its dimensionality suffix removed.

    ``QgsWkbTypes.displayString`` spells a Z-enabled polygon layer ``PolygonZ``, which is
    the same *shape* as ``Polygon`` for the purpose of matching a class or a collection.
    """
    base = (name or "").strip()
    while base and base[-1] in DIMENSION_SUFFIXES:
        base = base[:-1]
    return base


#: The geometry families a collection can be typed on. Named for the single-part spelling
#: because that is how ``label_class.geom_type`` and PostGIS both spell the family, and
#: because a family is not a WKB type: ``Polygon`` here covers ``MultiPolygon`` too.
POINT = "Point"
LINE = "LineString"
POLYGON = "Polygon"

_FAMILY_OF_TYPE: Mapping[str, str] = {
    "Point": POINT,
    "MultiPoint": POINT,
    "LineString": LINE,
    "MultiLineString": LINE,
    "Polygon": POLYGON,
    "MultiPolygon": POLYGON,
}


def geometry_family(geometry_type: str) -> str:
    """Which family a geometry type belongs to, or ``""`` when it belongs to none.

    Empty for the cases that must not be routed on a guess: a ``GeometryCollection``, a
    curve type, the "Unknown (any)" OGR reports for a mixed layer, and a layer with no
    geometry at all. Each of those would have to be split before it could be published,
    and :meth:`CollectionRoutes.refusal` says so rather than picking a family for it.
    """
    return _FAMILY_OF_TYPE.get(base_geometry_type(geometry_type), "")


#: Words a deployment may spell a geometry family with inside a collection id. Read as
#: whole tokens rather than as substrings, so ``labeled_extent`` is not mistaken for a
#: line collection and ``endpoint_log`` is not mistaken for a point one.
_FAMILY_TOKENS: Mapping[str, str] = {
    "point": POINT,
    "points": POINT,
    "multipoint": POINT,
    "multipoints": POINT,
    "line": LINE,
    "lines": LINE,
    "linestring": LINE,
    "linestrings": LINE,
    "multilinestring": LINE,
    "multilinestrings": LINE,
    "polygon": POLYGON,
    "polygons": POLYGON,
    "multipolygon": POLYGON,
    "multipolygons": POLYGON,
}

_SEPARATORS = re.compile(r"[^0-9a-z]+")


def _tokens(collection_id: str) -> list[str]:
    return [token for token in _SEPARATORS.split(str(collection_id).lower()) if token]


def _typed(collection_id: str) -> tuple[str, str] | None:
    """``(family, stem)`` for a collection id that names one geometry family, else None.

    Exactly one family word, because two of them (``label_point_polygon``) says nothing
    about which one the collection stores, and choosing between them here would be a coin
    toss with permanent consequences.
    """
    tokens = _tokens(collection_id)
    found = [
        (index, _FAMILY_TOKENS[token])
        for index, token in enumerate(tokens)
        if token in _FAMILY_TOKENS
    ]
    if len(found) != 1:
        return None
    index, family = found[0]
    return family, "_".join(tokens[:index] + tokens[index + 1 :])


def stem_of(collection_id: str) -> str:
    """A collection id with its geometry word removed, normalised.

    ``label_point``, ``label-polygon`` and ``label`` all stem to ``label``. The stem is
    how the three typed collections are recognised as siblings, and how a preference
    remembered from a pre-split deployment ("the labels are in ``label``") still points at
    the right group afterwards.
    """
    typed = _typed(collection_id)
    return typed[1] if typed else "_".join(_tokens(collection_id))


@dataclass(frozen=True)
class CollectionRoutes:
    """Which collection each geometry family publishes into, for one backend."""

    #: Geometry family -> collection id. Empty when nothing was resolved.
    by_family: Mapping[str, str] = field(default_factory=dict)
    #: A collection that accepts any geometry, or ``""``. This is what a deployment that
    #: still serves the single untyped collection resolves to, and it is also the fallback
    #: for a mixed-geometry layer on a deployment that offers one alongside the typed
    #: three. Never a guess: it is only ever a collection the backend listed.
    untyped: str = ""
    #: The shared stem the routed collections were recognised by. Reported, not obeyed.
    stem: str = ""
    #: Every collection id considered, in the order the backend listed them. Carried so a
    #: refusal can say what WAS offered, which is the difference between a message that
    #: ends an investigation and one that starts it.
    offered: tuple[str, ...] = ()
    #: Stems of the typed groups seen when there was more than one and nothing said which
    #: group holds the labels. Non-empty means "resolved nothing, on purpose".
    ambiguous: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.by_family or self.untyped)

    def collection_for(self, geometry_type: str) -> str:
        """Where a layer of this geometry type publishes, or ``""`` if nowhere."""
        family = geometry_family(geometry_type)
        if family:
            routed = self.by_family.get(family)
            if routed:
                return routed
        return self.untyped

    def refusal(self, layer_name: str, geometry_type: str) -> str:
        """Why this layer cannot be published at all, or ``""``.

        Names the layer and its geometry type in every branch. A publish blocked by a
        message that says neither leaves the analyst looking at a table of seven layers
        with no way to tell which one the sentence is about.
        """
        if self.collection_for(geometry_type):
            return ""
        shown = geometry_type or "no declared"
        if not self:
            listed = ", ".join(self.offered) or "nothing"
            if self.ambiguous:
                return (
                    f"{layer_name} ({shown} geometry) has nowhere to go: this backend "
                    f"offers more than one set of geometry-typed collections "
                    f"({', '.join(self.ambiguous)}) and nothing says which of them holds "
                    "labels. Choose the collection explicitly before publishing."
                )
            return (
                f"{layer_name} ({shown} geometry) has nowhere to go: this backend lists no "
                f"collection that stores labels. Collections offered: {listed}."
            )
        family = geometry_family(geometry_type)
        if not family:
            return (
                f"{layer_name} holds {shown} geometry, which is not a point, a line or a "
                "polygon, so nothing here can say which collection it belongs in. Each "
                "collection stores ONE geometry type - a mixed layer has to be split by "
                "geometry type and each part published separately."
            )
        return (
            f"{layer_name} holds {geometry_type} geometry and this backend lists no "
            f"collection that stores {family}. Publishing it into "
            f"{', '.join(sorted(set(self.by_family.values())))} would be refused feature "
            "by feature by the server's own geometry check."
        )

    def targets(self) -> tuple[str, ...]:
        """Every collection these routes can send to, sorted."""
        return tuple(sorted({*self.by_family.values(), *([self.untyped] if self.untyped else [])}))

    def describe(self) -> str:
        """One line saying where things go, for a status message or a log entry."""
        if self.by_family:
            parts = [f"{family} -> {where}" for family, where in sorted(self.by_family.items())]
            if self.untyped:
                parts.append(f"anything else -> {self.untyped}")
            return ", ".join(parts)
        if self.untyped:
            return f"everything -> {self.untyped}"
        return "nothing resolved"


def single(collection_id: str) -> CollectionRoutes:
    """Routes for a deployment that publishes everything into one collection.

    The honest degradation, and the pre-split behaviour exactly: one untyped collection
    named by a human, every layer sent to it. Reached when the collection list cannot be
    read as a set of geometry-typed siblings -- see :func:`build_routes` -- and the plugin
    asks which collection holds the labels instead of guessing one.
    """
    collection_id = str(collection_id or "")
    return CollectionRoutes(
        untyped=collection_id,
        stem=stem_of(collection_id),
        offered=(collection_id,) if collection_id else (),
    )


def build_routes(collection_ids: Iterable[str], preferred: str = "") -> CollectionRoutes:
    """Resolve geometry family -> collection against what the backend actually lists.

    `preferred` is the collection id this deployment was last known to hold labels in.
    It is a hint about WHICH group, never about which id: its stem is compared against the
    stems of the typed groups, so a preference of ``label`` remembered from before the
    split still selects ``label_point``/``label_line``/``label_polygon`` afterwards, with
    no migration of the stored setting and no re-prompt.

    Returns empty routes -- falsy -- when the answer is genuinely unknown, which is the
    only safe answer: two unrelated groups of typed collections with nothing saying which
    holds labels is a question for the person publishing, not for a tie-break rule here.
    """
    offered = tuple(dict.fromkeys(str(c) for c in collection_ids if c))

    groups: dict[str, dict[str, str]] = {}
    for collection_id in offered:
        typed = _typed(collection_id)
        if typed is None:
            continue
        family, stem = typed
        # First listing wins. Two collections claiming the same family under the same stem
        # is a deployment nothing here can choose between, and quietly taking the later one
        # would make the choice invisible in a flow whose writes cannot be undone.
        groups.setdefault(stem, {}).setdefault(family, collection_id)

    wanted = stem_of(preferred) if preferred else ""
    untyped_preferred = preferred if preferred in offered and _typed(preferred) is None else ""

    if wanted and wanted in groups:
        stem = wanted
    elif untyped_preferred:
        # A deployment still serving the single untyped collection the preference names.
        # Its own stem does not match any typed group, and routing labels into some other
        # deployment's typed collections because they were the only ones listed would be a
        # guess about which data belongs where.
        return CollectionRoutes(untyped=untyped_preferred, stem=wanted, offered=offered)
    elif len(groups) == 1:
        stem = next(iter(groups))
    else:
        return CollectionRoutes(offered=offered, ambiguous=tuple(sorted(groups)))

    # An untyped sibling, where the deployment still serves one during the transition: a
    # mixed-geometry layer has somewhere honest to go, and a geometry family the typed set
    # does not cover does not become unpublishable.
    untyped = next(
        (c for c in offered if _typed(c) is None and stem_of(c) == stem),
        "",
    )
    return CollectionRoutes(
        by_family=dict(groups[stem]), untyped=untyped, stem=stem, offered=offered
    )
