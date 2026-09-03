"""Creating and configuring the OGC API - Features layers.

HOW LITTLE OF THIS IS DATA ACCESS

None of it. QGIS's native ``OAPIF`` provider does the reading, the paging, the bbox
filtering and -- through Part 4 -- the create, update and delete. What the plugin adds is
the three things the provider has no way to know:

* which collection, with which credential and which as-of filter (:mod:`.core.uri`);
* **which history track**, which has to be in the layer's own data source rather than in
  a setting -- see :data:`TRACK_PROPERTY` and :func:`apply_canaries`;
* how the classes should look and read, which comes from the class registry rather than
  from anything compiled in;
* **which transaction-time instant**, when the layer is a view of a past belief rather
  than of the present -- see :data:`RECORDED_AT_PROPERTY` and :mod:`.core.recorded`;
* a custom property marking the layer as ours, so the as-of control can find its own
  layers again without guessing from names a user is free to rename.

Style preservation around ``setDataSource`` is the non-obvious part and is explained at
:func:`repoint_layer`.

WHERE THE TRACK GOES, AND WHY IN THREE PLACES

The provider makes the requests, including the Part 4 writes, so anything that must ride
on all of them has to be somewhere the provider looks. The track is put in all three:

* the ``X-Track`` header in the URI (:func:`build_oapif_uri`) -- the one that always
  survives, because it is attached per request rather than followed from a link;
* the same header on the credential (:mod:`.auth`), so it holds even if a URI is edited;
* a ``?track=`` query parameter on the landing URL, which is the weakest and is there for
  the landing request only -- see :func:`landing_url`.

None of the three is the isolation. The isolation is row-level security in the database;
these are how the database is told which track this session is, and
:func:`apply_canaries` is how the plugin notices if the telling failed.

WHERE THE TRANSACTION-TIME INSTANT GOES, AND WHY IT IS THE SAME ANSWER

A historical layer -- what the team *believed* at some instant -- carries an
``X-Recorded-At`` header in exactly the same place, for a stronger version of the same
reason. A track scopes a session; an instant scopes one layer, and an ``http-header:`` URI
parameter is per layer by construction. So a live layer and a historical one coexist over
one connection, two historical layers at different instants coexist too, and a saved
``.qgz`` reopens on the instant it was saved with rather than on whatever a setting says.

The instant is also what makes a layer **read-only**, and that is enforced rather than
documented: it is on the ``OPTIONS`` probe QGIS uses to decide whether a layer is editable
(``sendOPTIONS`` installs the URI's headers; ``computeCapabilities`` appends no query
parameters), the server answers without the write verbs, and :func:`mark_read_only` then
holds the layer read-only on this side as well so a stale capability decision cannot
reopen it. See :mod:`.core.recorded`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsDataProvider,
    QgsEditorWidgetSetup,
    QgsFeatureRenderer,
    QgsFeatureRequest,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsMapLayer,
    QgsMarkerSymbol,
    QgsPainting,
    QgsProject,
    QgsProperty,
    QgsRendererCategory,
    QgsSimpleMarkerSymbolLayerBase,
    QgsSymbolLayer,
    QgsUnitTypes,
    QgsVectorDataProvider,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtXml import QDomDocument

from .core import asof, recorded, routing, stylecapture, styling
from .core import tracks as track_tools
from .core.asof import AsOfMechanism
from .core.errors import BackendError, MixedGeometryError
from .core.expressions import all_of, identifier
from .core.fields import DEFAULT_FIELDS, CoreFields
from .core.registry import ClassRegistry, LabelClass
from .core.tracks import TRACK_HEADER, Track
from .core.uri import build_oapif_uri
from .core.urls import normalise_base_url, with_query
from .log import log, log_warning
from .settings import PluginSettings
from .validtime import install_default

#: Marks a layer as created by this plugin, and records which collection it shows.
COLLECTION_PROPERTY = "cvi/collection_id"

#: Which history track a layer was loaded from.
#:
#: On the layer rather than in settings, and that is the whole value of it: a ``.qgz``
#: saved while working on one track and opened by a colleague whose setting names another
#: would otherwise redirect their edits silently. With this, the panel can compare what
#: is loaded against what is selected and say so.
TRACK_PROPERTY = "cvi/track"

#: The transaction-time instant a layer is a view of, or absent for a live layer.
#:
#: On the layer for the same reason the track is, and one more: every re-point rebuilds the
#: URI from scratch, so :func:`repoint_for` reads the instant back from *here* rather than
#: from a caller who might forget. A track switch that silently converted a historical
#: layer into a live one -- same name, same styling, present-day data -- is precisely the
#: populated-and-wrong failure this feature exists to avoid.
RECORDED_AT_PROPERTY = "cvi/recorded_at"

#: OAPIF provider key. QGIS registers it from the WFS provider library.
OAPIF_PROVIDER = "OAPIF"


def landing_url(settings: PluginSettings, track: Track | None = None, recorded_at: str = "") -> str:
    """The URL handed to the provider as ``url``, carrying everything a parameter can.

    THIS QUERY STRING IS NOT DECORATION. It is the only channel a plugin has into the
    requests QGIS's native OAPIF provider builds for itself, and that was established by
    capture rather than by reading: the provider re-applies this URL's query string to the
    landing page, ``/openapi``, ``/collections/{id}`` and both ``/items`` fetches, while it
    drops the URI's ``http-header:`` parameters entirely. See :mod:`.core.recorded`.

    Three things ride here:

    ``datetime``
        the valid-time as-of, when the mechanism is ``datetime`` rather than ``cql2`` --
        see :mod:`.core.asof` for why both exist.
    ``recorded_at``
        the transaction-time pin. **The transport, not a convenience.** Absent from the
        ``OPTIONS`` editability probe, which costs nothing because the historical
        collection is read-only on the server anyway.
    ``track``
        the *weakest* of the three ways the track reaches the backend, and the one that
        matters least: the credential carries ``X-Track`` (:mod:`..auth`) and that is what
        has always done the work. Kept because it makes the landing request explicit and
        costs nothing.
    """
    base = normalise_base_url(settings.api_base_url)
    params: dict[str, object] = {}
    as_of = settings.as_of
    if as_of is not None and settings.as_of_mechanism is AsOfMechanism.DATETIME:
        params.update(asof.datetime_query(as_of))
    if recorded_at:
        params[recorded.RECORDED_AT_QUERY] = recorded_at
    if track is not None:
        params["track"] = track.name
    return with_query(base, params) if params else base


def track_filter_for(
    layer: QgsVectorLayer | None, track: Track | None, registry: ClassRegistry | None
) -> str | None:
    """The canary clause for this layer, or ``None`` if it cannot carry one.

    Only layers that actually expose ``track_id`` get it. Several collections are shared
    between tracks by design -- the class registry describes both datasets, and one
    GeoTIFF serves both -- so they have no such column, and filtering on a column a layer
    does not have makes the layer *invalid*: no layer at all, rather than an unfiltered
    one. That is why this is asked of a loaded layer rather than guessed from a
    collection id.
    """
    if layer is None or track is None:
        return None
    fields = registry.fields if registry else None
    name = fields.track_id if fields else "track_id"
    if name not in {field.name() for field in layer.fields()}:
        return None
    return track_tools.canary_filter(track, fields) if fields else track_tools.canary_filter(track)


def verify_recorded_echo(
    layer: QgsVectorLayer | None, recorded_at: str, registry: ClassRegistry | None
) -> None:
    """Raise unless `layer` really is showing the instant it asked for.

    THE CANARY. It reads one loaded feature's echo column and compares INSTANTS.

    Not a subset filter, and the difference is the whole point. This was a filter --
    ``"recorded_at" = '<asked>'`` ANDed into the layer's subset -- until it was measured
    against QGIS 3.44.13, which types that column by sniffing its value, decided it was a
    DateTime field, and compiled the clause into ``?datetime=``: the VALID-time parameter.
    So the canary verified nothing, silently pinned the Temporal Controller to one valid
    instant, and a deliberately wrong canary still returned features. Reading values back
    cannot be rewritten by anything.

    Silent in two cases, both of them correct answers rather than gaps:

    * an unpinned layer, which is making no claim to check;
    * a pinned layer with no rows, which is what a historical view legitimately looks like
      before the first label was drawn. The pin's arrival is unobservable there because
      there is nothing to observe it on -- and an empty layer cannot mislead anybody about
      what the team believed.
    """
    if layer is None or not recorded_at:
        return
    fields = registry.fields if registry else DEFAULT_FIELDS
    if not recorded.exposes_recorded_axis((field.name() for field in layer.fields()), fields):
        return
    index = layer.fields().indexOf(fields.recorded_at)
    if index < 0:
        return
    for feature in layer.getFeatures():
        problem = recorded.echo_mismatch(recorded_at, feature.attribute(index))
        if problem:
            raise BackendError(problem)
        return


def build_layer_uri(
    settings: PluginSettings,
    collection_id: str,
    registry: ClassRegistry | None,
    track: Track | None = None,
    track_filter: str | None = None,
    recorded_at: str = "",
) -> str:
    """Data-source URI for one collection, honouring the as-of state, track and instant.

    `track_filter` is passed in rather than derived, because deriving it needs a *loaded*
    layer -- see :func:`track_filter_for`. On first load there is none, so the layer opens
    unfiltered and :func:`apply_canaries` re-points it once its fields are known.

    THE TRANSACTION-TIME PIN IS NOT A FILTER and never re-points anything. It rides the
    landing URL's query string, so it is part of the data source from the first request,
    and it is checked afterwards by reading the echo back (:func:`verify_recorded_echo`).
    A filter was tried and was silently compiled onto the valid-time axis; see
    :mod:`.core.recorded`.

    `recorded_at` is the already-rendered wire instant, never a date. One conversion, in
    :func:`.core.recorded.instant`, so the query parameter, the header, the layer name and
    the layer's stored property all carry the same text.
    """
    as_of = settings.as_of
    cql = None
    if as_of is not None and settings.as_of_mechanism is AsOfMechanism.CQL2:
        fields = registry.fields if registry else None
        cql = asof.cql2_filter(as_of, fields) if fields else asof.cql2_filter(as_of)
    headers: dict[str, str] = {}
    if track is not None:
        headers[TRACK_HEADER] = track.name
    headers.update(recorded.headers(recorded_at))
    return build_oapif_uri(
        landing_url=landing_url(settings, track, recorded_at),
        collection_id=collection_id,
        authcfg=settings.authcfg_for(track.name if track else "") or None,
        page_size=int(settings.get("page_size")),
        restrict_to_request_bbox=bool(settings.get("restrict_to_canvas")),
        # Bracketed and ANDed rather than concatenated: cql2_filter already contains a
        # bare OR, and appending to it would rebind that OR and change which features come
        # back -- a filter that is wrong rather than absent.
        cql_filter=all_of(cql, track_filter) or None,
        headers=headers or None,
    )


def mixes_geometry(layer: QgsVectorLayer, registry: ClassRegistry | None = None) -> bool:
    """Does this collection serve more than one geometry family in one layer?

    Answered from the layer's FIELDS, not from its name. ``geom_family`` is on the mixed
    views and only on them -- so a deployment that names its collections differently, or
    adds a mixed one later, is handled without this plugin knowing anything about it.
    Putting a list of collection ids here would be exactly the hardcoded vocabulary the
    class registry design exists to remove.

    The coupling that keeps this true is worth stating, because it is the one way this
    goes wrong: a TYPED collection must not expose ``geom_family``. If one does, it is
    refused here -- loudly, at load, with the sentence below -- rather than shown as a
    subset, which is the safe direction to fail in but still a deployment defect to fix.
    """
    fields = registry.fields if registry else DEFAULT_FIELDS
    try:
        return layer.fields().indexOf(fields.geom_family) >= 0
    except (AttributeError, TypeError):  # pragma: no cover - binding shape, not logic
        return False


def mixed_geometry_refusal(collection_id: str, fields: CoreFields = DEFAULT_FIELDS) -> str:
    """Why a mixed collection is refused outright instead of loaded.

    A LAYER THAT SILENTLY SHOWS A SUBSET IS THE FAILURE THIS EXISTS TO REMOVE. QGIS types
    an OAPIF layer by SAMPLING features -- OAPIF cannot declare a collection's geometry
    type and pygeoapi answers ``{"format": "geometry-any"}`` -- so a collection holding
    points, lines and polygons becomes whichever shape sampled first and drops the rest
    with no message anywhere. Measured on the real corpus: 872 of 1,246 features
    invisible, and nobody goes looking for what they cannot see is missing.

    FILTERING THE LAYER TO ONE FAMILY WAS TRIED AND MEASURED TO FAIL. Do not re-add it.
    The subset filter DOES reach the provider -- the URI carries
    ``filter='"geom_family" = 'Point''`` -- but QGIS types the layer BEFORE applying it,
    so the result is a layer filtered to points and typed as polygons: it draws nothing at
    all. Wrong in a new way rather than fixed.

    What remains is a refusal. The server publishes one collection per geometry type
    (``db/migrations/016`` did it for the editable ones), each carrying a concrete PostGIS
    typmod, and those are what a person should load. This names the problem and points
    there without naming any collection id: ids are a deployment's choice, discovered from
    ``/collections`` at runtime, and a deployment that lists none of them is one that has
    not applied the migration yet -- which is exactly what this sentence tells them.
    """
    return (
        f"{collection_id!r} serves points, lines and polygons in ONE collection, and QGIS "
        "types a layer from whichever shape it samples first -- OGC API - Features has no "
        "way to declare a geometry type. The layer would draw that one shape and hide "
        "every other one with no error at all, so it is refused rather than loaded "
        "showing a subset. Load this deployment's per-geometry collections instead: one "
        "each for points, lines and polygons, which QGIS types correctly because each "
        "holds a single shape. If the collection list offers none, the backend has not "
        f"published them yet -- a collection carrying a {fields.geom_family!r} column is "
        "one of the mixed ones."
    )


def create_layer(
    settings: PluginSettings,
    collection_id: str,
    display_name: str,
    registry: ClassRegistry | None = None,
    track: Track | None = None,
    recorded_at: str = "",
) -> QgsVectorLayer:
    """Build a vector layer for one collection. Raises if the provider rejects it.

    A collection that MIXES geometry types is refused here -- see
    :func:`mixed_geometry_refusal`. That refusal is the guard for every caller at once,
    which is why it lives beside the construction rather than in the panel that happens to
    have asked first.

    With `recorded_at` set the layer is a view of a past belief, and three things follow
    that a live layer does not get: the echo canary, :func:`mark_read_only`, and
    auto-refresh switched off. All three are applied *after* the canary re-point, because
    ``setDataSource`` rebuilds the provider and recomputes the layer's read-only state from
    the new provider's capabilities.
    """
    uri = build_layer_uri(settings, collection_id, registry, track, recorded_at=recorded_at)
    layer = QgsVectorLayer(uri, display_name, OAPIF_PROVIDER)
    if not layer.isValid():
        raise BackendError(
            f"QGIS could not open collection {collection_id!r}. "
            f"{layer.error().summary() or 'The provider gave no reason.'}"
        )
    fields = registry.fields if registry else DEFAULT_FIELDS
    if mixes_geometry(layer, registry):
        # Before anything else the layer is used for, because everything downstream --
        # the styling, the QA checks, the analyst's own eyes -- would be reading a layer
        # that shows one shape and hides the rest. Discarding it costs one request; not
        # discarding it costs 872 features nobody can see are gone.
        raise MixedGeometryError(mixed_geometry_refusal(collection_id, fields))
    names = [field.name() for field in layer.fields()]
    if recorded_at and not recorded.exposes_recorded_axis(names, fields):
        # Refused rather than loaded unverified. The edge refuses this too, from its own
        # allowlist, but a deployment can be running an older edge and the failure it would
        # otherwise produce is the bad one: a full layer of present-day features under a
        # name asserting a past instant.
        raise BackendError(recorded.cannot_be_pinned(collection_id, fields))
    layer.setCustomProperty(COLLECTION_PROPERTY, collection_id)
    if track is not None:
        layer.setCustomProperty(TRACK_PROPERTY, track.name)
    if recorded_at:
        layer.setCustomProperty(RECORDED_AT_PROPERTY, recorded_at)
    apply_canaries(layer, settings, registry, track, recorded_at)
    # AFTER the re-point, not before: apply_canaries swaps the data source, and the check
    # has to be made against the requests the layer will actually keep making.
    verify_recorded_echo(layer, recorded_at, registry)
    if recorded_at:
        configure_historical_layer(layer, registry, recorded_at)
    else:
        # Live layers only. A historical layer is read-only by construction, and putting
        # a creation default on one would propose a valid time for a feature that can
        # never be created -- harmless, but it would appear in the field configuration
        # of a layer whose whole contract is "you cannot write here".
        install_default(layer, fields)
    return layer


def apply_canaries(
    layer: QgsVectorLayer,
    settings: PluginSettings,
    registry: ClassRegistry | None,
    track: Track | None,
    recorded_at: str = "",
) -> bool:
    """Re-point `layer` with the track canary in its filter, if it can carry one.

    Costs one extra provider round trip and buys the difference between an empty layer and
    a wrong one: row-level security already scopes every read to a track, but if the
    ``X-Track`` header ever stops arriving, ``app.track()`` falls back to the deployment's
    *default* track and answers with somebody else's polygons. Silent, plausible data. A
    filter on a value the server itself supplies is the cheapest possible detector.

    ONLY THE TRACK, and the asymmetry is deliberate. ``track_id`` is a UUID, which QGIS
    types as a string, so a filter on it stays a filter. The transaction-time echo is an
    instant, and a filter on *that* was compiled into ``?datetime=`` and quietly changed
    which axis the layer was filtering on -- so it is verified by
    :func:`verify_recorded_echo` instead, after the layer loads. `recorded_at` is still
    threaded through because the re-point must not drop the pin off the landing URL.

    Returns whether the layer was re-pointed, so the caller can say so.
    """
    track_clause = track_filter_for(layer, track, registry)
    if not track_clause:
        return False
    repoint_layer(
        layer,
        build_layer_uri(
            settings,
            collection_of(layer),
            registry,
            track,
            track_filter=track_clause,
            recorded_at=recorded_at,
        ),
    )
    return True


def repoint_for(
    layer: QgsVectorLayer,
    settings: PluginSettings,
    registry: ClassRegistry | None,
    track: Track | None,
) -> None:
    """Re-point an already-loaded layer at the current track and as-of state. Once.

    The canary clauses are worked out from the layer's *existing* fields, before the source
    is swapped, which is what makes this one provider round trip rather than two: the field
    set does not change between tracks -- it is the same collection either way -- so there
    is nothing to be learned by re-pointing first and asking afterwards.

    THE INSTANT IS READ BACK OFF THE LAYER, not taken from a caller. Every caller here is
    changing some *other* axis -- the track, or the valid-time as-of -- and would have to
    remember to carry this one through. Forgetting would turn a historical layer into a
    live one under an unchanged name and unchanged styling, which is the failure mode this
    whole feature is built to prevent; making it impossible to forget is worth a lookup.

    :func:`create_layer` still needs the two-step form, because on a first load there is no
    layer to ask.
    """
    recorded_at = recorded_at_of(layer)
    repoint_layer(
        layer,
        build_layer_uri(
            settings,
            collection_of(layer),
            registry,
            track,
            track_filter=track_filter_for(layer, track, registry),
            recorded_at=recorded_at,
        ),
    )
    if track is not None:
        layer.setCustomProperty(TRACK_PROPERTY, track.name)
    if recorded_at:
        # setDataSource rebuilt the provider, and QGIS recomputes a layer's read-only state
        # from the new provider's capabilities when it does. Without this, a track switch
        # would hand back an editable historical layer.
        mark_read_only(layer)
        # And the new data source is a new set of requests, so the pin's arrival is a fresh
        # question. A track switch that dropped the parameter would otherwise leave a layer
        # still named for a past instant and quietly showing the present.
        verify_recorded_echo(layer, recorded_at, registry)


def is_plugin_layer(layer: QgsMapLayer) -> bool:
    return bool(layer.customProperty(COLLECTION_PROPERTY, ""))


def collection_of(layer: QgsMapLayer) -> str:
    return str(layer.customProperty(COLLECTION_PROPERTY, "") or "")


def track_of(layer: QgsMapLayer) -> str:
    """The track name this layer was loaded from, or ``""`` if it predates tracks."""
    return str(layer.customProperty(TRACK_PROPERTY, "") or "")


def recorded_at_of(layer: QgsMapLayer) -> str:
    """The instant this layer is a view of, or ``""`` when it shows the present.

    Stored on the layer, so it survives a project save and a reload: the panel can find its
    historical layers again in a ``.qgz`` somebody else opens, and :func:`repoint_for` can
    carry the instant through a change on the other axis without being told.
    """
    return str(layer.customProperty(RECORDED_AT_PROPERTY, "") or "")


def is_historical(layer: QgsMapLayer) -> bool:
    """True for a layer pinned to a past belief. Read-only, by construction."""
    return bool(recorded_at_of(layer))


def historical_layers(project: QgsProject | None = None) -> list[QgsVectorLayer]:
    """Every historical layer in the project, in load order."""
    return [layer for layer in plugin_layers(project) if is_historical(layer)]


def live_layers(project: QgsProject | None = None) -> list[QgsVectorLayer]:
    """Every plugin layer showing the present.

    The set the collection list compares against. A historical layer must NOT count as
    "this collection is already loaded": the whole use case is having the live layer and
    one or more historical ones open at once, and two historical layers at different
    instants are the case the header-versus-query decision turns on.
    """
    return [layer for layer in plugin_layers(project) if not is_historical(layer)]


def mark_read_only(layer: QgsVectorLayer) -> None:
    """Hold a historical layer read-only on this side too, and say so in its metadata.

    Belt to the server's braces. QGIS decides editability by issuing ``OPTIONS`` to
    ``/collections/{id}/items`` and looking for ``POST`` in the ``Allow`` header; the header
    the URI carries is on that probe, so the server answers a pinned request without the
    write verbs and the pencil greys out by itself. This is the case where that has not
    happened -- an older edge, a proxy that dropped the header, a capability decision QGIS
    cached before something changed -- and it costs one call.

    ``setReadOnly`` is stronger than hiding a button: ``startEditing()`` returns false, so
    there is no edit buffer to lose and no commit to refuse. Note that ``setDataSource``
    recomputes this from the provider, which is why :func:`repoint_for` re-applies it.

    The abstract is set because a person who finds a greyed-out pencil needs somewhere to
    read *why*, and layer properties is where they will look.
    """
    layer.setReadOnly(True)
    layer.setAbstract(recorded.read_only_reason(recorded_at_of(layer)))


def provider_advertises_writes(layer: QgsVectorLayer) -> bool:
    """True when the provider thinks this layer can be written to.

    On a historical layer that is a server-side misconfiguration, not a client bug, and it
    is worth surfacing rather than silently papering over with :func:`mark_read_only`: it
    means the ``OPTIONS`` probe came back with the write verbs on a request that named an
    instant, so some *other* client -- one that does not know about any of this -- would
    have been allowed to edit a past belief.

    Answers ``False`` rather than raising when the provider cannot be asked. A check must
    never break the thing it is checking, and a false alarm here would send somebody after a
    server misconfiguration that does not exist.
    """
    # QGIS 4 moves this onto Qgis.VectorProviderCapability. Resolved by name rather than
    # written once, for the same reason as _stroke_colour_property: this plugin builds
    # against both lines.
    capability = getattr(
        getattr(QgsVectorDataProvider, "Capability", None),
        "AddFeatures",
        getattr(getattr(Qgis, "VectorProviderCapability", None), "AddFeatures", None),
    )
    if capability is None:
        return False
    try:
        return bool(layer.dataProvider().capabilities() & capability)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not raise
        log_warning(f"Could not read provider capabilities on {layer.name()!r}: {exc}")
        return False


def configure_historical_layer(
    layer: QgsVectorLayer, registry: ClassRegistry | None, recorded_at: str
) -> None:
    """Everything a historical layer needs beyond its URI.

    AUTO-REFRESH OFF, and this is a real saving rather than tidiness: the project ships a
    90-second ``ReloadData`` tick so annotators see each other's work, and a past belief
    cannot change. Re-downloading an answer that is provably constant is a canvas stutter
    every ninety seconds for nothing.

    TEMPORAL PROPERTIES SET EXPLICITLY, from the valid-time columns. The collection carries
    both axes, and only the valid-time pair is DateTime-typed -- transaction time is text on
    the wire precisely so QGIS's filter compiler cannot mistake it for ``datetime=`` -- so
    the Temporal Controller's field picker cannot be aimed at the wrong axis even by
    accident. Naming the fields here rather than leaving the mode unset means the controller
    slides over what a historical layer legitimately contains: every valid-time state that
    was believed at the pinned instant.
    """
    fields = registry.fields if registry else DEFAULT_FIELDS
    names = {field.name() for field in layer.fields()}

    layer.setAutoRefreshMode(Qgis.AutoRefreshMode.Disabled)
    mark_read_only(layer)

    if fields.valid_from in names and fields.valid_to in names:
        temporal = layer.temporalProperties()
        temporal.setMode(Qgis.VectorTemporalMode.FeatureDateTimeStartAndEndFromFields)
        temporal.setStartField(fields.valid_from)
        temporal.setEndField(fields.valid_to)
        temporal.setIsActive(True)

    layer.setOpacity(styling.HISTORICAL_OPACITY)
    log(f"Historical layer {layer.name()!r} pinned to {recorded_at} and held read-only.")


def first_feature_track(layer: QgsVectorLayer, registry: ClassRegistry | None) -> object:
    """The ``track_id`` of one feature, or ``None`` when the layer cannot answer.

    Deliberately one feature and no geometry: this is a check, not a scan, and it runs on
    the main thread right after a layer loads. A layer with no features answers ``None``,
    which is not a mismatch -- an empty layer is a perfectly ordinary thing to have.
    """
    fields = registry.fields if registry else None
    name = fields.track_id if fields else "track_id"
    index = layer.fields().indexOf(name)
    if index < 0:
        return None
    request = QgsFeatureRequest()
    request.setLimit(1)
    request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
    request.setSubsetOfAttributes([index])
    for feature in layer.getFeatures(request):
        return feature.attribute(index)
    return None


def track_mismatch(
    layer: QgsVectorLayer, registry: ClassRegistry | None, track: Track | None
) -> str:
    """A sentence when a loaded layer is showing another track's features, else ``""``."""
    return track_tools.mismatch(track, first_feature_track(layer, registry))


def layers_on_other_tracks(
    track: Track | None, project: QgsProject | None = None
) -> list[QgsVectorLayer]:
    """Plugin layers whose stored track disagrees with the selected one.

    The shared-project case: a ``.qgz`` saved on one track, opened by somebody whose
    setting names another. Their layers keep talking to the track they were saved on --
    which is correct, because the track is in each layer's own data source -- and the only
    thing that could go wrong is nobody being told. This is how they are told.
    """
    wanted = track.name if track else ""
    return [
        layer for layer in plugin_layers(project) if track_of(layer) and track_of(layer) != wanted
    ]


def dirty_layers(project: QgsProject | None = None) -> list[QgsVectorLayer]:
    """Plugin layers with unsaved edits in their buffer.

    Switching tracks re-points every layer, and ``setDataSource`` on a layer with a dirty
    edit buffer discards those edits without a prompt. Refusing the switch is the only
    honest option: the alternative is silently throwing away work somebody did.
    """
    return [layer for layer in plugin_layers(project) if layer.isEditable() and layer.isModified()]


def plugin_layers(project: QgsProject | None = None) -> list[QgsVectorLayer]:
    """Every vector layer this plugin loaded, in the current project."""
    project = project or QgsProject.instance()
    return [
        layer
        for layer in project.mapLayers().values()
        if layer.type() == Qgis.LayerType.Vector and is_plugin_layer(layer)
    ]


def find_layers_with_fields(
    required: Iterable[str],
    project: QgsProject | None = None,
    excluding: Iterable[str] = (),
) -> list[QgsVectorLayer]:
    """Every plugin layer carrying all of `required` and none of `excluding`.

    Plural because labels are served as one collection per geometry type: a project with
    compounds, cooling units and powerlines open has three label layers, all equally the
    label layer. A caller that takes the first of them silently answers its question about
    a third of the data -- see :func:`find_label_layers`.
    """
    wanted = {name for name in required if name}
    unwanted = {name for name in excluding if name}
    if not wanted:
        return []
    return [
        layer
        for layer in live_layers(project)
        if wanted.issubset({field.name() for field in layer.fields()})
        and not (unwanted & {field.name() for field in layer.fields()})
    ]


def find_layer_with_fields(
    required: Iterable[str],
    project: QgsProject | None = None,
    excluding: Iterable[str] = (),
) -> QgsVectorLayer | None:
    """First plugin layer carrying all of `required` and none of `excluding`.

    Layers are identified by the fields they expose, not by collection name. A deployment
    is free to call its collections whatever it likes, and this way the QA tools keep
    working when it does -- the field names themselves come from the registry, so even
    those are not compiled in.

    `excluding` exists because "carries these fields" is not always discriminating on its
    own; see :func:`find_label_layer`.

    Historical layers are never candidates. They answer a different question, they cannot
    be edited, and every caller of this wants the layer the annotator is working on.
    """
    found = find_layers_with_fields(required, project, excluding)
    return found[0] if found else None


def find_label_layer(
    registry: ClassRegistry, project: QgsProject | None = None
) -> QgsVectorLayer | None:
    """The layer holding labels: it has both an identity and a class.

    TWO OTHER COLLECTIONS CARRY BOTH, and picking either of them is silent.

    ``label_history`` is keyed on the same ``label_id`` and carries the ``class_id`` of
    each superseded state, so identity and class alone do not tell it apart from the label
    collection, and ``QgsProject.mapLayers()`` returns them in an order nobody controls.
    Picking it would run the coverage check over every historical revision of every label,
    counting a label once per edit and classifying geometry no longer on the map.

    The transaction-time view carries both as well -- it *is* the label set, as believed at
    some past instant -- and picking that one is worse: the coverage report, the history
    dialog and the selection they drive would all be about a belief the team has since
    revised, including labels deleted long ago, with nothing on screen to say so. Two
    independent exclusions catch it, because the two fail in different circumstances: the
    echo column names it by shape (and comes from the registry, so a deployment that
    renames the column is still covered), and :func:`live_layers` names it by the property
    this plugin stamped on it (which holds even for a deployment whose historical view
    exposes different columns entirely).
    """
    found = find_label_layers(registry, project)
    return found[0] if found else None


def find_label_layers(
    registry: ClassRegistry, project: QgsProject | None = None
) -> list[QgsVectorLayer]:
    """EVERY loaded label layer, in project order.

    There is more than one, and that is the whole reason this exists. Labels are served as
    one collection per geometry type, so an analyst working on compounds and cooling units
    has two label layers open and both are the label layer. A QA check that took the first
    of them would examine the polygons, find nothing wrong with the points it never read,
    and report a clean project -- the failure mode that makes a check worse than none,
    because its silence is believed.

    The exclusions are :func:`find_label_layer`'s, and they matter more here rather than
    less: with several genuine label layers in a project, "the first one that matches" no
    longer accidentally protects against matching the history collection too.
    """
    return find_layers_with_fields(
        (registry.fields.label_id, registry.fields.class_id),
        project,
        excluding=(
            registry.fields.history_id,
            registry.fields.operation,
            registry.fields.recorded_at,
            registry.fields.superseded,
        ),
    )


def find_extent_layer(
    registry: ClassRegistry, project: QgsProject | None = None
) -> QgsVectorLayer | None:
    """The layer holding survey extents: it says how complete a sweep was."""
    return find_layer_with_fields((registry.fields.class_id, registry.fields.completeness), project)


def repoint_layer(layer: QgsVectorLayer, uri: str) -> None:
    """Swap a layer's data source, keeping its styling and form configuration.

    ``setDataSource`` rebuilds the provider, and the renderer, field aliases and editor
    widgets do not reliably survive that. Losing them is not cosmetic: the categorized
    renderer and the ``class_id`` value map are the plugin's entire contribution to how
    the layer reads, so an as-of change would silently undo the configuration it was
    supposed to leave alone.

    Exporting to a QDomDocument and re-importing afterwards is the cheap, provider-
    agnostic way to hold on to all of it.
    """
    document = QDomDocument()
    layer.exportNamedStyle(document)

    options = QgsDataProvider.ProviderOptions()
    layer.setDataSource(uri, layer.name(), OAPIF_PROVIDER, options, False)

    restored, message = layer.importNamedStyle(document)
    if not restored:
        log(f"Could not restore style on {layer.name()!r} after re-pointing: {message}")
    layer.triggerRepaint()


def _layer_geometry_family(layer: QgsVectorLayer) -> str:
    """The layer's geometry family, or ``""`` when this QGIS cannot say.

    Empty is a real answer and not a failure: a mixed or unknown layer -- which is what an
    untyped collection with nothing in it to sample looks like -- has no one family, and
    the caller then falls back to styling each category by its own class. Guarded because
    ``wkbType`` is a binding detail and a stand-in layer in a test has no reason to have
    one; the styling is worth degrading for, never worth raising for.
    """
    try:
        return routing.geometry_family(str(QgsWkbTypes.displayString(layer.wkbType()) or ""))
    except (AttributeError, TypeError):  # pragma: no cover - binding shape, not logic
        return ""


# ── reading a renderer back into the registry's style vocabulary ─────────────
#
# The inverse of _symbol_for below, and of :mod:`.core.styling` generally: everything that
# decides whether a symbol can be captured, and what it converts to, lives in
# :mod:`.core.stylecapture` and is tested there without a QGIS import in sight. What
# follows only extracts values out of real QGIS objects into that module's plain
# dataclasses -- the same split :mod:`.core.styling` and this file already have on the way
# out, run backwards.


def describe_renderer(renderer: QgsFeatureRenderer) -> stylecapture.RendererDescription:
    """Everything a bare ``QgsFeatureRenderer`` can answer about itself.

    Deliberately not the whole picture. ``layer_opacity``, ``blend_mode``,
    ``scale_visibility`` and ``labels_enabled`` are ``QgsMapLayer`` properties -- a
    renderer object has no method that answers any of them, and asking one of its methods
    for something it does not track would come back with a default that looks like an
    honest zero. :func:`capture_layer_style` is what reads those off the layer and layers
    them on before handing the description to :mod:`.core.stylecapture`.

    Split out on its own so a ``QgsCategorizedSymbolRenderer`` test double is all a test
    needs -- no ``QgsVectorLayer`` double required to exercise the renderer half of this.
    """
    renderer_type = str(renderer.type() or "")
    symbol = None
    category_field = ""
    category_count = 0
    if renderer_type == stylecapture.SINGLE_SYMBOL:
        raw_symbol = renderer.symbol()
        if raw_symbol is not None:
            symbol = _describe_symbol(raw_symbol)
    elif renderer_type in stylecapture.ATTRIBUTE_RENDERERS:
        # Read even though stylecapture.py refuses every renderer of this shape: the
        # refusal's own wording ("styled into 5 symbols") is what tells an analyst this is
        # a "these are your classes" problem and not a bug, and only the renderer itself
        # can supply the 5.
        category_field = _category_field(renderer)
        category_count = _category_count(renderer)
    return stylecapture.RendererDescription(
        renderer_type=renderer_type,
        symbol=symbol,
        category_count=category_count,
        category_field=category_field,
    )


def capture_layer_style(layer: QgsVectorLayer) -> stylecapture.CaptureResult:
    """Capture a loaded layer's renderer as a ``label_class.style`` proposal, or refuse it.

    The one QGIS-facing entry point this reader exists for. Everything past
    :func:`describe_renderer` -- refusing a categorized renderer, halving a marker's
    diameter, converting millimetres, getting the colour byte order right -- is
    :mod:`.core.stylecapture`'s job, tested there with no QGIS import at all; this
    function's only job is to hand it a correct description of what the layer holds.
    """
    renderer = layer.renderer()
    if renderer is None:
        return stylecapture.CaptureResult(refusal=stylecapture.NO_SYMBOL)
    description = replace(
        describe_renderer(renderer),
        layer_opacity=layer.opacity(),
        blend_mode=_blend_mode_name(layer.blendMode()),
        scale_visibility=bool(layer.hasScaleBasedVisibility()),
        labels_enabled=bool(layer.labelsEnabled()),
    )
    return stylecapture.capture_style(description)


def _category_field(renderer) -> str:
    """The attribute an attribute-driven renderer classifies on, or ``""`` if it has none.

    ``classAttribute()`` is common to a categorized and a graduated renderer; a rule-based
    one has no single attribute at all. Guarded with ``getattr`` rather than branching on
    ``renderer_type`` so that renderer falls straight through to stylecapture.py's generic
    "by attribute value" wording instead of this reader calling a method that was never
    going to exist.
    """
    getter = getattr(renderer, "classAttribute", None)
    return str(getter()) if callable(getter) else ""


def _category_count(renderer) -> int:
    """How many symbols an attribute-driven renderer offers, or 0 if this reader cannot
    count them.

    Read for the refusal's own wording: "styled into 5 symbols" is the sentence that makes
    "these are five classes" land, and "several symbols" is what stylecapture.py says
    instead when this comes back 0 -- a rule-based renderer, say, which has no flat list of
    symbols to count.

    Two accessors, because ``classAttribute()``'s own docstring is right that a categorized
    and a graduated renderer are both attribute-driven, but they do not share a method for
    the list itself: ``QgsCategorizedSymbolRenderer.categories()`` returns
    ``QgsRendererCategory`` objects, ``QgsGraduatedSymbolRenderer`` has no ``categories()``
    at all and offers ``ranges()`` (``QgsRendererRange``) instead. Trying ``categories``
    first and falling back to ``ranges`` costs nothing on a renderer with neither -- both
    ``getattr``s come back ``None`` and this still returns 0, the same "cannot count them"
    answer a rule-based renderer already got.
    """
    for name in ("categories", "ranges"):
        getter = getattr(renderer, name, None)
        if not callable(getter):
            continue
        try:
            return len(getter())
        except TypeError:  # pragma: no cover - binding shape, not logic
            return 0
    return 0


def _describe_symbol(symbol) -> stylecapture.SymbolDescription:
    """A ``QgsSymbol``'s layers, in ``symbolLayers()``'s own order, plus its own opacity.

    Order is preserved rather than re-sorted because it has to be:
    :func:`.core.stylecapture._first_simple_layer` picks the FIRST layer this vocabulary
    can express and reports the rest as dropped, and "first" only means what an analyst
    expects if this reader has not reshuffled the stack getting here.
    """
    layers = tuple(_describe_symbol_layer(layer) for layer in symbol.symbolLayers())
    return stylecapture.SymbolDescription(
        layers=layers, opacity=symbol.opacity(), symbol_type=_symbol_type_name(symbol)
    )


def _symbol_type_name(symbol) -> str:
    """The symbol's own shape word -- "Fill", "Line" or "Marker" -- for a refusal's prose
    only, e.g. "this fill symbol is built from GradientFill". Never branched on, so a
    build where this fails answers ``""`` rather than losing the whole capture over one
    sentence.
    """
    try:
        return str(symbol.symbolTypeToString(symbol.type()) or "")
    except (AttributeError, TypeError):  # pragma: no cover - binding shape, not logic
        return ""


def _describe_symbol_layer(layer) -> stylecapture.SymbolLayerDescription:
    """One ``QgsSymbolLayer``, described without importing QGIS to describe it again.

    Every accessor past ``type_name`` and ``enabled`` is scoped to
    ``stylecapture.SIMPLE_LAYER_KINDS``: a ``QgsGradientFillSymbolLayer`` has none of
    ``fillColor``/``strokeColor``/``strokeWidth`` and calling them would raise, and
    stylecapture.py already knows what to do with a ``type_name`` it does not recognise --
    it only needs to be handed one, not have this reader interrogate a symbol it is about
    to refuse anyway.
    """
    type_name = layer.layerType()
    enabled = bool(layer.enabled())
    if type_name not in stylecapture.SIMPLE_LAYER_KINDS:
        return stylecapture.SymbolLayerDescription(type_name=type_name, enabled=enabled)

    is_line = type_name == "SimpleLine"
    # SimpleLine has one colour, not a fill and a stroke -- QgsSimpleLineSymbolLayer.color()
    # *is* the stroke. Reading it as a fill would leave every linear class with no visible
    # line at all; see stylecapture.py's own accessor table for the same trap on the way in.
    fill_color = None if is_line else _colour_string(layer.fillColor())
    stroke_color = _colour_string(layer.color() if is_line else layer.strokeColor())
    stroke_width = layer.width() if is_line else layer.strokeWidth()
    stroke_width_unit = _unit_name(layer.widthUnit() if is_line else layer.strokeWidthUnit())
    stroke_style = _pen_style_name(layer.penStyle() if is_line else layer.strokeStyle())

    size = None
    size_unit: stylecapture.Unit | str = stylecapture.Unit.MILLIMETRES
    marker_shape = stylecapture.CIRCLE
    if type_name == "SimpleMarker":
        # size is a DIAMETER -- stylecapture.capture_style halves it into a radius on the
        # way through. Handing the raw number across unconverted is the bug that draws 872
        # cooling units at twice their size; this reader's only job is not to touch it
        # before that halving happens.
        size = layer.size()
        size_unit = _unit_name(layer.sizeUnit())
        marker_shape = _marker_shape_name(layer.shape())

    brush_style = "solid" if is_line else _brush_style_name(layer.brushStyle())

    dash_pattern: tuple[float, ...] = ()
    dash_pattern_unit: stylecapture.Unit | str = stylecapture.Unit.MILLIMETRES
    if type_name != "SimpleMarker" and layer.useCustomDashPattern():
        # A "dash" pen style with an EMPTY vector is Qt's own built-in pattern, not a
        # custom one, and has no pixel lengths in it to read -- see stylecapture.py's
        # _dash_into. Reading the vector only when the flag says there is one keeps that
        # distinction intact instead of inventing a dash from nothing.
        dash_pattern = tuple(layer.customDashVector())
        dash_pattern_unit = _unit_name(layer.customDashPatternUnit())

    return stylecapture.SymbolLayerDescription(
        type_name=type_name,
        fill_color=fill_color,
        stroke_color=stroke_color,
        stroke_width=stroke_width,
        stroke_width_unit=stroke_width_unit,
        size=size,
        size_unit=size_unit,
        marker_shape=marker_shape,
        brush_style=brush_style,
        stroke_style=stroke_style,
        dash_pattern=dash_pattern,
        dash_pattern_unit=dash_pattern_unit,
        enabled=enabled,
        data_defined=_active_data_defined(layer),
    )


def _colour_string(color) -> str | None:
    """A ``QColor`` as ``"r,g,b,a"`` -- never ``.name(QColor.HexArgb)``.

    That form is ``#AARRGGBB``; it parses as a colour just as readily as CSS's
    ``#RRGGBBAA`` and is not the same one -- see stylecapture.py's module docstring.
    ``red()``/``green()``/``blue()``/``alpha()`` auto-convert from any colour spec and
    return 0-255 ints by contract, so there is nothing to round or clamp on this side, only
    the wrong four methods to call instead.
    """
    if color is None:
        return None
    return f"{color.red()},{color.green()},{color.blue()},{color.alpha()}"


def _unit_name(unit) -> str:
    """``QgsUnitTypes.encodeUnit()``'s own spelling -- "MM", "Pixel", "MapUnit", ....

    A stable string-serialization encoder, not an enum whose shape moved between QGIS 3.44
    and 4.x, so unlike :func:`_symbol_layer_property_key` there is nothing to resolve by
    name here: calling it directly already returns exactly what
    ``stylecapture.normalise_unit`` expects.
    """
    return QgsUnitTypes.encodeUnit(unit)


def _marker_shape_name(shape) -> str:
    """``QgsSimpleMarkerSymbolLayerBase.encodeShape()`` -- not ``.name()``.

    stylecapture.py's own docstring assumed ``.name()``; that method does not exist on
    QGIS 3.44.13. ``encodeShape`` is the real accessor, and -- like ``encodeUnit`` above --
    a stable string encoder with no by-name resolution to do.
    """
    return QgsSimpleMarkerSymbolLayerBase.encodeShape(shape)


def _qt_style_member(namespace: str, name: str):
    """One ``Qt`` enum member, resolved by name across PyQt5's flat aliases and PyQt6's
    scoped-only spellings.

    PyQt5 keeps a flat top-level alias for each member (unscoped on ``Qt`` directly) beside
    the scoped ``Qt.BrushStyle.SolidPattern`` form; PyQt6 -- which QGIS 4.x builds against
    -- drops the flat alias. Same shape as :func:`_symbol_layer_property_key` and for the
    same reason: this plugin builds against both lines, and neither spelling can be
    assumed gone.
    """
    scoped = getattr(Qt, namespace, None)
    value = getattr(scoped, name, None) if scoped is not None else None
    if value is not None:
        return value
    return getattr(Qt, name, None)


def _brush_style_name(style) -> str:
    """``"solid"``, ``"no"``, or a generic ``"pattern"``.

    stylecapture.py branches on exactly the first two -- ``"no"`` is how a polygon symbol
    says outline-only -- and treats everything else as prose in a note, so a hatch and a
    texture both read as ``"pattern"`` rather than needing a name each.
    """
    if style == _qt_style_member("BrushStyle", "NoBrush"):
        return "no"
    if style == _qt_style_member("BrushStyle", "SolidPattern"):
        return "solid"
    return "pattern"


#: Pen styles worth a name of their own in the report; everything else becomes "pattern".
#: stylecapture.py branches only on "solid"/"no" -- "dash"/"dot" exist purely to sharpen
#: the note it writes about anything else being a Qt built-in pattern with no pixel
#: lengths to read.
_PEN_STYLE_WORDS: tuple[tuple[str, str], ...] = (
    ("NoPen", "no"),
    ("SolidLine", "solid"),
    ("DashLine", "dash"),
    ("DotLine", "dot"),
)


def _pen_style_name(style) -> str:
    for name, word in _PEN_STYLE_WORDS:
        if style == _qt_style_member("PenStyle", name):
            return word
    return "pattern"


def _blend_mode_name(mode) -> str:
    """QGIS's own name for a layer's blend mode, or ``""`` if this build cannot say.

    ``QgsMapLayer.blendMode()`` answers in Qt's own compositing enum;
    ``QgsPainting.getBlendModeEnum`` converts it to ``Qgis.BlendMode``, whose member names
    are what stylecapture.py's note compares against
    (``renderer.blend_mode.lower() != "normal"``). Whether ``.name`` resolves cleanly on a
    live SIP-wrapped enum instance is unconfirmed -- see the design doc's risk list -- so
    "Normal", the one value actually branched on, is checked by identity first, and
    anything else falls back to a generic word rather than an ``AttributeError``.
    """
    try:
        qgis_mode = QgsPainting.getBlendModeEnum(mode)
    except (AttributeError, TypeError):  # pragma: no cover - binding shape, not logic
        return ""
    normal = getattr(getattr(Qgis, "BlendMode", None), "Normal", None)
    if normal is not None and qgis_mode == normal:
        return "Normal"
    name = getattr(qgis_mode, "name", None)
    return name if isinstance(name, str) else "custom"


def _symbol_layer_property_key(name: str):
    """One ``QgsSymbolLayer`` data-defined property key, across QGIS's enum spellings.

    QGIS moved these from a flat ``QgsSymbolLayer.Property<Name>`` to a scoped
    ``QgsSymbolLayer.Property`` enum and dropped the ``Property`` prefix on the way. The
    plugin supports both the 3.44 LTR and the 4.x line, so the key is resolved by name
    rather than written once and broken by whichever build the annotator has. ``None``
    means this QGIS has neither spelling for `name`.
    """
    scoped = getattr(QgsSymbolLayer, "Property", None)
    value = getattr(scoped, name, None) if scoped is not None else None
    if value is not None:
        return value
    return getattr(QgsSymbolLayer, f"Property{name}", None)


#: The four data-defined overrides stylecapture.py's vocabulary could otherwise have
#: expressed. QgsPropertyCollection holds around 70 kinds of override; naming only these
#: four keeps the report honest about what THIS vocabulary lost, rather than listing
#: overrides on properties -- label placement, geometry generators -- it was never going
#: to carry regardless of whether they are active.
_DATA_DEFINED_NAMES: tuple[str, ...] = ("FillColor", "StrokeColor", "StrokeWidth", "Size")


def _active_data_defined(layer) -> tuple[str, ...]:
    """Names of the vocabulary's four properties carrying an active data-defined override.

    An override that is set but not ACTIVE draws nothing different from the plain colour,
    so ``isActive`` is what is checked -- a merely-present-but-off override reported as a
    loss would be a false alarm the analyst cannot do anything about.
    """
    collection = layer.dataDefinedProperties()
    active = []
    for name in _DATA_DEFINED_NAMES:
        key = _symbol_layer_property_key(name)
        if key is not None and collection.isActive(key):
            active.append(name)
    return tuple(active)


def _symbol_for(label_class: LabelClass, historical: bool = False, geom_type: str = ""):
    """Build a symbol in the class's registry style, for `geom_type` if one is given.

    `geom_type` is the LAYER's geometry family, and it overrides the class's own because a
    categorized renderer's symbols have to match the layer, not the category. Labels now
    live in one collection per geometry type, so a point layer carries a category for every
    class in the registry -- including the polygon ones, which still have to appear, because
    a category dropped from the renderer renders its features invisible rather than merely
    unstyled. Given a fill symbol on a point layer QGIS draws nothing at all; given a
    marker in the polygon class's colours it draws something correct and recognisable.
    """
    effective = geom_type or label_class.geom_type
    properties = styling.symbol_properties(effective, label_class.style, historical)
    kind = styling.symbol_kind(effective)
    if kind == "marker":
        return QgsMarkerSymbol.createSimple(properties)
    if kind == "line":
        return QgsLineSymbol.createSimple(properties)
    return QgsFillSymbol.createSimple(properties)


def _stroke_colour_property():
    """The symbol-layer property key for a stroke colour specifically.

    A thin specialisation of :func:`_symbol_layer_property_key` (see the reader section
    above, which needs the same by-name resolution for three more property names). Kept as
    its own function because :func:`_apply_superseded_colour` calling ``_stroke_colour_property()``
    says what it is for at the call site in a way ``_symbol_layer_property_key("StrokeColor")``
    would not. ``None`` means this QGIS has neither spelling, in which case the superseded
    colouring is skipped and the layer is still correct -- just less legible.
    """
    return _symbol_layer_property_key("StrokeColor")


def _apply_superseded_colour(symbol, label_class: LabelClass, fields) -> None:
    """Colour a symbol's stroke by whether the belief has since ended.

    A data-defined property on the ordinary symbol, so it survives the style round trip
    :func:`repoint_layer` performs -- see :func:`.core.styling.superseded_stroke_expression`
    for why that rules out a rule-based renderer.
    """
    key = _stroke_colour_property()
    if key is None:
        return
    expression = styling.superseded_stroke_expression(label_class.style, fields)
    for index in range(symbol.symbolLayerCount()):
        symbol.symbolLayer(index).setDataDefinedProperty(
            key, QgsProperty.fromExpression(expression)
        )


def apply_registry(
    layer: QgsVectorLayer, registry: ClassRegistry, historical: bool = False
) -> None:
    """Configure a label layer from the class registry.

    Everything here is derived from what the server sent. No class name, attribute name
    or enum value is written into the plugin, which is what keeps QGIS and the web viewer
    from drifting apart when someone adds a class on a Tuesday.

    `historical` changes how the layer *reads*, never what it contains: dashed strokes, an
    alert colour on beliefs that have since ended, and one more line in the map tip. The
    categorized renderer on ``class_id`` stays exactly as it is, because the class colours
    are how people read a labels layer and changing them would make the two layers harder
    to compare -- which is the whole reason both are open at once.
    """
    fields = registry.fields
    names = [field.name() for field in layer.fields()]

    if fields.class_id in names:
        _apply_class_renderer(layer, registry, historical and fields.superseded in names)
        if not historical:
            # A value map on a read-only layer is a picker nobody can use. Worse, it makes
            # the attribute table show class ids that were retired since -- through a map
            # built from the classes the server accepts *today* -- as blanks.
            _apply_class_value_map(layer, registry)

    _apply_aliases(layer, registry, names)

    _lock_server_assigned(layer, registry)
    _apply_map_tip(layer, registry, names, historical)


def _apply_class_renderer(
    layer: QgsVectorLayer, registry: ClassRegistry, historical: bool = False
) -> None:
    """Categorize on ``class_id`` using each class's own style block.

    All classes live in one table with a ``class_id`` column, so one layer carries them
    all and a categorized renderer is the natural expression of that. Retired classes are
    included: historical labels still reference them, and dropping their category would
    render those features invisible rather than merely uneditable.

    Every category is built for the LAYER's geometry, not for the class's: a categorized
    renderer whose symbol type does not match the layer draws nothing at all.

    CATEGORIES ARE FILTERED TO THE CLASSES THAT CAN ACTUALLY OCCUR HERE, plus a catch-all.
    This function used to offer every class on every layer, on the grounds that dropping a
    category renders its features invisible and that filtering would break the day a
    deployment stored a class somewhere this plugin did not predict. The first half stopped
    being true when labels moved to one collection per geometry type: the views filter on
    the geometry stored, and app.label_check() refuses a `compound` that is not polygonal,
    so a compound feature CANNOT reach the lines layer and its category cannot hide one.
    What it can do is put five polygon classes in a line layer's legend, which is what an
    analyst reads when choosing what they are about to draw.

    The second half is still true, and is why the catch-all exists rather than why the
    filter does not. An unfiltered legend was defence against unexpected data by accident;
    the fallback category is the same defence on purpose, and it says "unexpected" out loud
    instead of hiding the surprise among eight legitimate entries.

    `Any` classes -- `unclassified` declares one -- match every layer, because a label with
    no class yet really can be any shape.
    """
    geom_type = _layer_geometry_family(layer)
    categories = []
    for label_class in registry:
        # No family means QGIS could not type the layer (a mixed or empty collection):
        # show everything rather than guess, which is the old behaviour and the right
        # one when there is nothing to filter against.
        if geom_type and not label_class.matches_geometry(geom_type):
            continue
        symbol = _symbol_for(label_class, historical, geom_type)
        if historical:
            _apply_superseded_colour(symbol, label_class, registry.fields)
        categories.append(
            QgsRendererCategory(label_class.class_id, symbol, label_class.display_name, True)
        )
    if not categories:
        return
    if geom_type:
        # QGIS treats a category with an empty value as "all other values". Anything this
        # plugin did not predict therefore still DRAWS, and draws as something a person
        # will ask about -- which is the outcome the old unfiltered legend was reaching for.
        catch_all = _symbol_for(registry.unclassified_or_first(), historical, geom_type)
        categories.append(QgsRendererCategory("", catch_all, "Other class (unexpected here)", True))
    layer.setRenderer(QgsCategorizedSymbolRenderer(registry.fields.class_id, categories))
    layer.triggerRepaint()


def _apply_class_value_map(layer: QgsVectorLayer, registry: ClassRegistry) -> None:
    """Turn ``class_id`` into a picker of the classes the server currently accepts.

    A list, not free text: ``label_class.class_id`` is a foreign key with a snake_case
    CHECK constraint, and a typo becomes a failed write at save time rather than a
    validation message at edit time. Only active classes are offered, because the
    database refuses new labels on a retired class.
    """
    index = layer.fields().indexOf(registry.fields.class_id)
    if index < 0:
        return
    entries = [{display: stored} for display, stored in registry.value_map()]
    if not entries:
        return
    layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup("ValueMap", {"map": entries}))


#: Columns the SERVER assigns, which an analyst must never be invited to type into.
#:
#: `id` is the one that prompted this. It is the OAPIF feature id -- a surrogate bigint,
#: GENERATED ALWAYS AS IDENTITY -- and it rendered as an empty editable box that TOOK
#: FOCUS when the attribute form opened, so the first thing an analyst saw on every new
#: polygon was a required-looking field with no correct answer. It is absent from the
#: collection's `immutable_properties` for a good reason (it is the feature id, not a
#: property), so nothing upstream was marking it.
#:
#: The rest are the provenance columns. They already arrive read-only from the provider
#: on a well-configured deployment; setting them here as well costs nothing and means the
#: form is right even against a deployment that has not.
SERVER_ASSIGNED_FIELDS = (
    "id",
    "label_id",
    "track_id",
    "recorded_from",
    "created_by",
    "created_at",
    "updated_by",
    "updated_at",
)


def _lock_server_assigned(layer: QgsVectorLayer, registry: ClassRegistry) -> None:
    """Mark server-assigned columns read-only in the attribute form.

    QgsEditFormConfig rather than the field's own editability: the provider decides what
    it will ACCEPT, this decides what the form OFFERS, and the second is what an analyst
    experiences. A field the server will reject is worse than a field that is not there,
    because it looks like a decision somebody has to make.
    """
    config = layer.editFormConfig()
    fields = layer.fields()
    names = {registry.fields.label_id, registry.fields.track_id} | set(SERVER_ASSIGNED_FIELDS)
    for name in names:
        index = fields.indexOf(name)
        if index >= 0:
            config.setReadOnly(index, True)
    layer.setEditFormConfig(config)


def _apply_aliases(layer: QgsVectorLayer, registry: ClassRegistry, names: list[str]) -> None:
    """Give the stable core columns readable names, and flag the JSON containers.

    Only the core columns are aliased. The contents of ``attrs`` are not fields at the
    OAPIF level -- they arrive inside one JSON value -- and inventing aliases for them
    would mean hardcoding attribute names, which is exactly the thing the schema design
    exists to avoid.
    """
    fields = registry.fields
    aliases = {
        fields.label_id: "Label ID (immutable)",
        fields.class_id: "Class",
        fields.name_zh: "Name (中文)",
        fields.name_en: "Name (English)",
        fields.names: "Names (JSON, all languages)",
        fields.attrs: "Attributes (JSON, see class schema)",
        fields.valid_from: "Valid from (on the ground)",
        fields.valid_to: "Valid to (on the ground)",
        fields.capture_id: "Drawn from capture",
        fields.updated_by: "Last edited by",
        fields.updated_at: "Last edited at",
    }
    for name, alias in aliases.items():
        if name in names:
            layer.setFieldAlias(layer.fields().indexOf(name), alias)

    for json_field in (fields.attrs, fields.names):
        index = layer.fields().indexOf(json_field)
        if index >= 0:
            layer.setEditorWidgetSetup(
                index, QgsEditorWidgetSetup("TextEdit", {"IsMultiline": True, "UseHtml": False})
            )


def _apply_map_tip(
    layer: QgsVectorLayer, registry: ClassRegistry, names: list[str], historical: bool = False
) -> None:
    """A hover tip that answers the three questions asked most while labeling.

    Chinese name first. 82.6% of compounds have a Chinese name and 8.9% an English one,
    so an English-first tip is blank most of the time.

    On a historical layer there is a fourth question, and it is the one the layer exists to
    answer: *when did we stop thinking this?* Shown only where the belief actually ended,
    so a label that is still believed today reads exactly as it does on the live layer.
    """
    fields = registry.fields
    rows: list[str] = []
    if fields.name_zh in names:
        rows.append(f'<b>[% "{fields.name_zh}" %]</b>')
    if fields.name_en in names:
        rows.append(f'[% "{fields.name_en}" %]')
    if fields.class_id in names:
        rows.append(f'<i>[% "{fields.class_id}" %]</i>')
    if fields.label_id in names:
        rows.append(f'<small>[% "{fields.label_id}" %]</small>')
    if historical and fields.superseded in names and fields.belief_to in names:
        ended, until = identifier(fields.superseded), identifier(fields.belief_to)
        rows.append(f"[% if({ended}, 'believed until ' || {until}, '') %]")
    if rows:
        layer.setMapTipTemplate("<br/>".join(rows))
