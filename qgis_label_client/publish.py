"""Reading local vector layers and sending them to the backend, off the main thread.

WHAT THIS FILE OWNS AND WHAT IT DELIBERATELY DOES NOT

It owns the parts that need QGIS: finding the layers, reprojecting them, turning a
``QgsFeature`` into plain values and a GeoJSON geometry, and driving the POSTs from inside
a ``QgsTask``. Every decision -- which class, which attribute key, whether a name is
damaged, whether a geometry can be reshaped -- is made in :mod:`.core.publish` and
:mod:`.core.legacy`, where it can be tested without QGIS running. That split is the point:
the decisions are the risk, and the risk has to live where CI can reach it.

THREADING, WHICH IS WHERE THIS KIND OF CODE USUALLY GOES WRONG

``QgsTask.run()`` is a worker thread. It may not touch widgets, ``iface`` or
``QgsProject``, and a ``QgsVectorLayer`` is not safe to read from it either. The QGIS
answer is :class:`QgsVectorLayerFeatureSource`: constructed on the main thread from a
layer, it is an independent, thread-safe iterator over that layer's features, and it is
what the Processing framework uses for exactly this. So :func:`prepare` runs on the main
thread and captures everything the worker will need -- feature source, field names,
coordinate transform, bounding box -- and :func:`publish` runs on the worker and touches
none of it.

The coordinate transform is built on the main thread too, because it needs the project's
transform context. Once built it is a value object and crosses the boundary safely. Log
lines are the one main-thread-looking thing the worker does; ``QgsMessageLog`` is
explicitly thread-safe and queues to the main thread itself.

TWO WAYS OF SENDING, AND WHY BOTH ARE CORRECT

Measured: 1,807 features at one POST each takes about fifteen minutes, and almost none of
that is the network. The auth edge caps writes at two a second per principal, and
1807 / 2 is 903 seconds. The round trips are not the cost; the limiter is.

A backend advertising an atomic bulk create in ``/v1/capabilities`` gets chunks --
:func:`_send_chunk` -- and the same import becomes a handful of requests. A backend
without one gets exactly what it always got, one feature per request, which is not a
legacy path but the correct behaviour against a deployment that has not been updated:
:func:`_send` still buys "every refusal names its row" and "nothing is ever sent twice"
without asking anything of the server.

The two paths report into the same counters and neither is allowed to be optimistic. What
gets credited as published is what the server SAID it created, never the length of what
was sent, and a chunk whose fate is unknown says so rather than resolving itself in either
direction -- because the repair for a wrong answer here is a second publish, and identity
is server-assigned, so nothing afterwards can tell the two copies apart.
"""

from __future__ import annotations

import json
import time
import traceback
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsFeature,
    QgsFeatureRequest,
    QgsFeedback,
    QgsGeometry,
    QgsMapLayer,
    QgsProject,
    QgsVariantUtils,
    QgsVectorLayer,
    QgsVectorLayerFeatureSource,
    QgsWkbTypes,
)

from . import client
from . import layers as layer_tools
from .core import bulk
from .core.bulk import BulkCapability
from .core.errors import BackendError, ConfigurationError
from .core.fields import COMPLETENESS_EXHAUSTIVE, CoreFields
from .core.legacy import FieldMapping, map_fields, name_columns, name_entries
from .core.names import is_damaged
from .core.publish import (
    PUBLISHED_PROPERTY,
    STORAGE_CRS,
    LayerOutcome,
    LayerPlan,
    PublishRecord,
    PublishReport,
    SourceLayer,
    build_draft,
    format_record,
    parse_record,
)
from .log import log, log_warning

#: How many features one damage scan reads before it stops and reports a floor rather than
#: a total. The preview runs on the main thread and must not freeze QGIS over a layer
#: nobody intends to publish; 20,000 is far above the 1,246 this exists for and far below
#: the point where the scan is noticeable.
NAME_SCAN_LIMIT = 20000

#: Providers whose features come off local disk, and are therefore safe to read from the
#: main thread while the preview is being built.
#:
#: The cap above bounds the number of rows, not the cost of a row. A PostGIS or WFS layer
#: in the same project would have 20,000 rows pulled over the network before the dialog
#: appears, with QGIS showing "Not responding" and nothing on screen saying why. A layer
#: on a remote provider is still listed and still publishable -- it is only the *scan* that
#: is skipped, and :meth:`~.core.publish.LayerPlan.notes` says so, because a scan that
#: silently returned zero would read as "checked, and clean".
LOCAL_PROVIDERS = frozenset(
    {"ogr", "gdal", "delimitedtext", "spatialite", "memory", "virtual", "gpx", "gpkg"}
)


def is_local_provider(layer: QgsVectorLayer) -> bool:
    """True when this layer's features come from a local file."""
    try:
        provider = str(layer.providerType() or "")
    except (AttributeError, TypeError):  # pragma: no cover - binding shape, not logic
        return False
    return provider.lower() in LOCAL_PROVIDERS


def _target_crs() -> QgsCoordinateReferenceSystem:
    return QgsCoordinateReferenceSystem(STORAGE_CRS)


def python_value(value: Any) -> Any:
    """Normalise one QGIS attribute value to something the pure core can reason about.

    An empty DBF cell arrives as a NULL QVariant, which is not ``None`` and, depending on
    the binding, not reliably falsy either. Publishing one would write a JSON ``null`` into
    ``attrs`` -- a claim that somebody looked and found nothing, in a dataset where the
    truth is that nobody filled the column in at all.
    """
    if value is None:
        return None
    if QgsVariantUtils.isNull(value):
        return None
    return value


def feature_values(feature: QgsFeature, field_names: Sequence[str]) -> dict[str, Any]:
    """A feature's attributes as a plain mapping, NULLs normalised away."""
    return {name: python_value(feature.attribute(name)) for name in field_names}


#: Longest name put in front of a row's issue line. A report is read at a glance.
_SUBJECT_LIMIT = 40


def _subject(
    values: Mapping[str, Any],
    mappings: Sequence[FieldMapping],
    index: int,
) -> str:
    """How one feature is referred to in the report.

    The source ``id`` column is 0% populated and the server assigns identity, so there is
    no key to quote -- but the name columns are in hand at the moment anything goes wrong,
    and a position in the layer always is. Between them they turn "190 rejected" into
    something the operator can look up in the attribute table.
    """
    for _language, raw in name_entries(values, mappings):
        text = "" if raw is None else str(raw).strip()
        if text:
            if len(text) > _SUBJECT_LIMIT:
                text = text[: _SUBJECT_LIMIT - 1] + "…"
            return f"row {index} {text!r}"
    return f"row {index}"


def geometry_as_geojson(geometry: QgsGeometry | None) -> Mapping[str, Any] | None:
    """A GeoJSON geometry object, or ``None`` when there is nothing to send."""
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return None
    try:
        parsed = json.loads(geometry.asJson())
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) and parsed.get("type") else None


def geometry_type_name(layer: QgsVectorLayer) -> str:
    """The layer's WKB type, spelled the way ``label_class.geom_type`` spells it."""
    try:
        return str(QgsWkbTypes.displayString(layer.wkbType()) or "")
    except (AttributeError, TypeError):  # pragma: no cover - binding shape, not logic
        return ""


# ---------------------------------------------------------------------------
# Describing the project's local layers (main thread)
# ---------------------------------------------------------------------------


def count_damaged_names(layer: QgsVectorLayer, limit: int = NAME_SCAN_LIMIT) -> tuple[int, int]:
    """Count features whose name carries the UTF-7 truncation signature.

    Returns ``(damaged, scanned)``. Geometry is not requested and neither are the other
    columns, so this is a cheap pass over two strings per row even on the 872-feature
    layer.

    Counted before the preview rather than during the publish on purpose: the number is
    what makes the choice real, and a choice offered after the data has been sent is not a
    choice.
    """
    fields = layer.fields()
    columns = list(name_columns(f.name() for f in fields))
    if not columns:
        return 0, 0

    request = QgsFeatureRequest()
    # Neither the geometry nor the other columns are decoded. The scan is two strings per
    # row, which is what keeps it cheap enough to run on the main thread in the preview.
    request.setFlags(Qgis.FeatureRequestFlag.NoGeometry)
    request.setSubsetOfAttributes(columns, fields)

    damaged = 0
    scanned = 0
    for feature in layer.getFeatures(request):
        scanned += 1
        values = feature_values(feature, columns)
        if any(is_damaged(str(value)) for value in values.values() if value is not None):
            damaged += 1
        if scanned >= limit:
            break
    return damaged, scanned


def local_vector_layers(project: QgsProject | None = None) -> list[QgsVectorLayer]:
    """Vector layers in the project that did not come from the backend.

    Layers this plugin loaded are excluded. Publishing a collection back into itself would
    duplicate every feature in it, and the native OAPIF provider already edits those in
    place through Part 4 -- there is nothing there to bootstrap.
    """
    project = project or QgsProject.instance()
    return [
        layer
        for layer in project.mapLayers().values()
        if layer.type() == Qgis.LayerType.Vector
        and not layer_tools.is_plugin_layer(layer)
        and layer.isValid()
    ]


def describe_layer(layer: QgsVectorLayer, scan_names: bool | None = None) -> SourceLayer:
    """Everything the preview dialog needs about one local layer.

    `scan_names` defaults to "only if reading this layer is cheap" -- see
    :data:`LOCAL_PROVIDERS`.
    """
    if scan_names is None:
        scan_names = is_local_provider(layer)
    damaged, scanned = count_damaged_names(layer) if scan_names else (0, 0)
    crs = layer.crs()
    # QGIS answers -1 from featureCount() for a provider that cannot say in advance.
    # That is "I do not know yet", not "there is nothing here", and the difference matters
    # because the preview makes an empty layer a blocking problem.
    counted = int(layer.featureCount())
    return SourceLayer(
        layer_id=layer.id(),
        name=layer.name(),
        geometry_type=geometry_type_name(layer),
        # description() rather than nothing when there is no authority code, so the CRS
        # column says which CRS a WKT-only definition is instead of looking empty.
        crs_authid=str(crs.authid() or crs.description() or ""),
        crs_valid=bool(crs.isValid()),
        feature_count=max(counted, 0),
        count_known=counted >= 0,
        field_names=tuple(f.name() for f in layer.fields()),
        damaged_names=damaged,
        scanned=scanned,
        previous=parse_record(layer.customProperty(PUBLISHED_PROPERTY, "")),
    )


def describe_layers(project: QgsProject | None = None) -> list[SourceLayer]:
    """Describe every publishable layer in the project."""
    return [describe_layer(layer) for layer in local_vector_layers(project)]


# ---------------------------------------------------------------------------
# Preparing for the worker thread (main thread)
# ---------------------------------------------------------------------------


@dataclass
class PreparedLayer:
    """One layer's plan plus the thread-safe handles the worker will use.

    Everything in here was captured on the main thread, and nothing in here is a
    ``QgsVectorLayer``: the worker must never see one.
    """

    plan: LayerPlan
    source: QgsVectorLayerFeatureSource
    field_names: tuple[str, ...]
    mappings: tuple[FieldMapping, ...]
    transform: QgsCoordinateTransform | None = None
    extent_geojson: Mapping[str, Any] | None = None
    extent_problem: str = ""
    #: Which collection this layer's features are created in, decided from the layer's
    #: geometry type in :mod:`.core.routing` and settled here on the main thread. Per
    #: layer rather than per run: one geometry family per collection means a project of
    #: compounds, cooling units and powerlines writes to three of them in one publish.
    #: Empty falls back to :attr:`PublishRequest.collection_id`, which is what a
    #: deployment serving a single untyped collection resolves to.
    collection_id: str = ""


def _extent_polygon(
    layer: QgsVectorLayer, transform: QgsCoordinateTransform | None
) -> tuple[Mapping[str, Any] | None, str]:
    """The layer's bounding box as a MultiPolygon in the storage CRS.

    A bounding box is a weak claim -- it says "somewhere in this rectangle" where a drawn
    sweep says "this shape" -- which is exactly why the checkbox that reaches this function
    defaults to off, and why the caveat written alongside the extent says so in the record
    itself.
    """
    extent = layer.extent()
    if extent is None or extent.isEmpty():
        return None, "the layer has no extent to derive a survey polygon from"
    geometry = QgsGeometry.fromRect(extent)
    if transform is not None:
        try:
            geometry.transform(transform)
        except QgsCsException as exc:
            return None, f"the bounding box could not be reprojected to {STORAGE_CRS}: {exc}"
    as_json = geometry_as_geojson(geometry)
    if as_json is None:
        return None, "the bounding box produced no usable geometry"
    if as_json.get("type") == "Polygon":
        # labeled_extent.geom is geometry(MultiPolygon, 4326) and the server compares the
        # type exactly, so promote here rather than let the write be rejected.
        return {"type": "MultiPolygon", "coordinates": [as_json.get("coordinates")]}, ""
    return as_json, ""


def prepare(plans: Iterable[LayerPlan], project: QgsProject | None = None) -> list[PreparedLayer]:
    """Capture, on the main thread, everything the worker thread will need.

    Raises :class:`ConfigurationError` when a selected layer has left the project between
    the preview and the confirmation. Rare, and much better as a message than as a null
    dereference on a worker thread, where the traceback would be swallowed.
    """
    project = project or QgsProject.instance()
    target = _target_crs()
    prepared: list[PreparedLayer] = []

    for plan in plans:
        if not plan.publish or plan.label_class is None:
            continue
        layer = project.mapLayer(plan.source.layer_id)
        if layer is None:
            raise ConfigurationError(
                f"Layer {plan.source.name!r} is no longer in the project. Re-open the "
                "preview so the plan matches what is actually loaded."
            )

        crs = layer.crs()
        if not crs.isValid():
            # The preview blocks this, so reaching it means the layer changed underneath
            # the dialog. Refused rather than reprojected because QgsCoordinateTransform
            # short-circuits to a no-op when either CRS is invalid: the geometries would
            # be written to a 4326 column unchanged, with nothing raising and nothing to
            # find them by afterwards.
            raise ConfigurationError(
                f"Layer {plan.source.name!r} has no valid coordinate reference system, so "
                f"its coordinates cannot be converted to {STORAGE_CRS}. Set the layer CRS "
                "and re-open the preview."
            )

        transform = None
        if crs != target:
            # Built here because it needs the project's transform context. Afterwards it
            # is a value object and crosses the thread boundary safely.
            transform = QgsCoordinateTransform(crs, target, project.transformContext())

        extent_geojson: Mapping[str, Any] | None = None
        extent_problem = ""
        if plan.choice.declare_extent:
            extent_geojson, extent_problem = _extent_polygon(layer, transform)

        field_names = tuple(f.name() for f in layer.fields())
        prepared.append(
            PreparedLayer(
                plan=plan,
                # The documented way to read a layer's features from another thread.
                source=QgsVectorLayerFeatureSource(layer),
                field_names=field_names,
                mappings=map_fields(field_names, plan.label_class),
                transform=transform,
                extent_geojson=extent_geojson,
                extent_problem=extent_problem,
                # Carried from the plan rather than recomputed: the collection shown in the
                # preview and the collection written to have to be the same one, and the
                # only way to guarantee that is for there to be one decision.
                collection_id=plan.collection_id,
            )
        )
    return prepared


def stamp_published(
    plans: Iterable[LayerPlan],
    report: PublishReport,
    collection_id: str,
    project: QgsProject | None = None,
    track: str = "",
) -> None:
    """Record on each layer that it has been published. Main thread only.

    Written after the run rather than before, and written even for a partial run, because
    the warning it produces next time is "some of this is already up there" -- which is
    exactly the state a partial run leaves behind.

    The track is recorded with it, because "already published" is only a warning about a
    duplicate when the second publish would go to the same dataset. Publishing the same
    shapefile into a test track and then into the analysts' track is not a duplicate, it
    is the normal way a test dataset gets populated.

    The project is marked dirty afterwards. A custom property is in memory until the
    project file is saved, and QGIS does not treat a plugin setting one as a change worth
    prompting about -- so without this the analyst closes QGIS without being asked, reopens
    the same shapefiles, and the entire founding dataset is pre-ticked for a second publish
    with no warning anywhere. Marking it dirty is what makes the prompt appear.
    """
    project = project or QgsProject.instance()
    published_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stamped = False

    # Paired by position, not by layer name: two layers in one project may share a name,
    # and one outcome is appended per prepared layer in order. A cancelled run has fewer
    # outcomes than plans, which zip handles by stopping at the shorter -- the layers that
    # were never reached correctly get no stamp.
    for plan, outcome in zip(plans, report.outcomes, strict=False):
        if not outcome.published:
            continue
        layer: QgsMapLayer | None = project.mapLayer(plan.source.layer_id)
        if layer is None:
            continue
        # Only counts published to the SAME track accumulate. A layer sent to a test
        # track and then to the analysts' track has not been published twice into either,
        # and adding the two would make the next preview warn about a duplicate that does
        # not exist -- which is how a warning stops being read.
        previous = plan.source.previous
        carried = previous.feature_count if previous and not previous.on_another_track(track) else 0
        layer.setCustomProperty(
            PUBLISHED_PROPERTY,
            format_record(
                PublishRecord(
                    published_at=published_at,
                    # The layer's own collection, not the run's: a publish now fans out
                    # across collections by geometry, and a stamp naming a collection this
                    # layer never went to would put a false destination in the warning the
                    # next preview shows -- which is worse than no warning, because it is
                    # read as fact. `collection_id` remains the fallback for a deployment
                    # that routes everything to one untyped collection.
                    collection_id=plan.collection_id or collection_id,
                    class_id=outcome.class_id,
                    feature_count=carried + outcome.published,
                    track=track or report.track,
                )
            ),
        )
        stamped = True

    if stamped:
        project.setDirty(True)


# ---------------------------------------------------------------------------
# Doing the work (worker thread)
# ---------------------------------------------------------------------------


@dataclass
class PublishRequest:
    """Everything :func:`publish` needs. Built on the main thread, read on the worker."""

    base_url: str
    #: Where a layer goes when it carries no collection of its own.
    #:
    #: The run-wide fallback, not the destination: geometry-typed collections mean the
    #: destination belongs to the layer (:attr:`PreparedLayer.collection_id`). This is what
    #: a deployment serving a single untyped collection resolves to, and what a caller
    #: reaching this module without a routed plan supplies.
    collection_id: str
    authcfg: str
    layers: list[PreparedLayer] = field(default_factory=list)
    extent_collection: str = ""
    fields: CoreFields = field(default_factory=CoreFields)
    #: History track name, sent as ``X-Track`` on every request this run makes.
    #:
    #: A NAME, never a ``track_id`` in the feature body. ``label.track_id`` defaults to
    #: ``app.writable_track_id()``, resolved server-side from this header, and the write
    #: policy would refuse a client-supplied value anyway. Sending a name asks; sending an
    #: id would be a client asserting which dataset a row joins, which is exactly the
    #: decision row-level security exists to take away from it.
    #:
    #: Empty means "name no track", which the edge refuses for a write with 403 NoTrack --
    #: correctly. :func:`publish` refuses first, so the refusal is a sentence rather than
    #: 1,246 identical HTTP errors.
    track: str = ""
    #: How many times one feature is re-offered after the edge asks us to slow down.
    #: Only a 429 is retried; see :func:`_send_one`.
    max_throttle_retries: int = 6
    #: Where to ask what this deployment can do, relative to :attr:`base_url`.
    #:
    #: EMPTY MEANS DO NOT ASK, and therefore one feature per request. That is the safe
    #: default rather than an oversight: this module is reachable without the panel, and a
    #: caller that has not opted into the bulk path gets the slower one that has always
    #: worked, not a probe it did not ask for.
    capabilities_path: str = ""
    #: Features per bulk request, before the deployment's own cap is applied. Zero means
    #: "as many as the backend allows"; see the ``publish_chunk_size`` setting for why the
    #: panel asks for fewer than that.
    chunk_size: int = 0
    #: Identifies this run inside every chunk's ``X-Edit-Reason``. Minted per request
    #: object, because the reason has to be unique per chunk *across the run* for the
    #: recovery read in :mod:`.core.bulk` to mean anything.
    run_id: str = field(default_factory=bulk.new_run_id)

    def total_features(self) -> int:
        return sum(prepared.plan.source.feature_count for prepared in self.layers)

    def target_for(self, prepared: PreparedLayer) -> str:
        """The collection one prepared layer's features are created in."""
        return prepared.collection_id or self.collection_id

    def unrouted(self) -> list[str]:
        """Names of prepared layers with nowhere to go. See :func:`publish`."""
        return [
            prepared.plan.source.name for prepared in self.layers if not self.target_for(prepared)
        ]


#: Longest single wait after a 429 when the server sent no ``Retry-After``. The edge
#: refills its bucket at two writes a second, so a few seconds is always enough; the cap
#: exists so a misconfigured header cannot park the task for an hour.
MAX_BACKOFF_SECONDS = 30.0

#: Granularity of that wait. A cancelled task must stop within a slice, not at the end of
#: the sleep, or "cancel" means "cancel in half a minute".
_BACKOFF_SLICE = 0.25


def _wait(seconds: float, feedback: QgsFeedback | None) -> bool:
    """Sleep, in slices, until the time is up or the run is cancelled.

    Returns True if the wait completed. **Worker thread only** -- this blocks, which is
    fine here and would freeze QGIS anywhere else.
    """
    remaining = min(max(seconds, 0.0), MAX_BACKOFF_SECONDS)
    while remaining > 0:
        if feedback is not None and feedback.isCanceled():
            return False
        slice_length = min(_BACKOFF_SLICE, remaining)
        time.sleep(slice_length)
        remaining -= slice_length
    return feedback is None or not feedback.isCanceled()


def _send_one(
    request: PublishRequest,
    collection_id: str,
    feature: Mapping[str, Any],
    feedback: QgsFeedback | None,
) -> tuple[int, str | None]:
    """POST one feature, waiting out the edge's rate limiter rather than failing on it.

    Returns ``(published, error)``; ``(0, None)`` means the run was cancelled and nobody
    refused anything.

    A 429 is not a refusal of the content, it is "not yet". The auth edge caps writes at a
    couple per second per principal and sends ``Retry-After`` saying how long its bucket
    needs. Counting that as a rejection would end a 1,246-feature bootstrap with most of
    the dataset reported as refused, a layer stamped as published, and no way to tell which
    rows landed -- a half-written system of record, produced by the tool that exists to
    found it. So it waits, and only gives up after that stops working.

    Every other error is returned as-is. Retrying a *content* refusal cannot help, and
    retrying an ambiguous failure is how duplicates are made: a save here is not atomic and
    the server assigns identity, so nothing on this side can tell a lost response from a
    refused write.
    """
    attempts = max(1, request.max_throttle_retries + 1)
    for attempt in range(attempts):
        if feedback is not None and feedback.isCanceled():
            return 0, None
        try:
            client.create_feature(
                request.base_url,
                collection_id,
                feature,
                request.authcfg,
                feedback,
                track=request.track,
            )
        except BackendError as exc:
            if feedback is not None and feedback.isCanceled():
                # The socket was aborted because the user pressed cancel. Nobody refused
                # this feature, and reporting it as a rejection makes a deliberate stop
                # look like a backend problem in the one report that gets acted on.
                return 0, None
            if not exc.throttled or attempt == attempts - 1:
                return 0, str(exc)
            delay = exc.retry_after if exc.retry_after else 2.0 * (attempt + 1)
            log_warning(
                f"Rate limited by the auth edge; waiting {min(delay, MAX_BACKOFF_SECONDS):.0f}s "
                f"before retrying (attempt {attempt + 1} of {attempts})."
            )
            if not _wait(delay, feedback):
                return 0, None
            continue
        return 1, None
    return 0, None  # pragma: no cover - the loop always returns first


def _send(
    request: PublishRequest,
    collection_id: str,
    feature: Mapping[str, Any],
    subject: str,
    outcome: LayerOutcome,
    feedback: QgsFeedback | None,
) -> None:
    """Publish one drafted feature and fold the result into the layer's outcome.

    ONE FEATURE PER REQUEST: WHEN THIS RUNS, AND WHY IT IS NOT DEAD CODE

    This is what a backend without the atomic bulk endpoint gets, and what a *bulkable*
    backend's non-bulkable collections get. It is not a legacy path: it is the correct
    behaviour against a deployment that has not been updated, and it is the behaviour every
    property below was chosen to guarantee.

    A ``FeatureCollection`` POSTed to ``/items`` was here once and was removed, because
    three facts made re-sending a refused batch unsafe rather than merely slow: a save is
    **not atomic**, so the first rejection aborts the rest *after* earlier rows are
    committed; there is no ETag and no If-Match and identity is the server's, so nothing
    here can ask "did that one land?"; and the create handler takes a single ``Feature``.
    A partly-applied batch got re-sent whole, and the founding dataset gained duplicate
    rows nothing could tell apart. The batch was also *credited* on faith -- a non-raising
    POST counted as ``len(features)`` published with nothing verifying the server had
    created that many.

    None of that argues against a batch that the **server** applies in one transaction,
    which is what :func:`_send_chunk` uses when the backend offers one: all-or-nothing
    removes partial application, and a response stating what was created removes the
    faith. What it does argue is that this path stays, unchanged, for everything else --
    because one request per feature buys the two properties the bootstrap actually needs,
    and buys them without asking anything of the server: every refusal names its row, and
    nothing is ever sent twice.
    """
    if feedback is not None and feedback.isCanceled():
        # Already stopping. Sending would be one more write the user asked not to make.
        outcome.not_sent += 1
        return
    published, error = _send_one(request, collection_id, feature, feedback)
    if published:
        outcome.published += 1
    elif error is None:
        # Cancelled while the request was in flight: refused by nobody. Counting it as a
        # failure is the one report that makes the re-run decision unanswerable.
        outcome.not_sent += 1
    else:
        outcome.failed += 1
        outcome.note(f"the server refused a feature: {error}", subject)


# ---------------------------------------------------------------------------
# Many features per request, when the server can apply them as one transaction
# ---------------------------------------------------------------------------


@dataclass
class _BulkRun:
    """The bulk endpoint this run found, and the chunk ordinal it is up to.

    The ordinal counts across the WHOLE run rather than per layer, because it is half of
    the ``reason`` that makes an ambiguous chunk recoverable: two chunks sharing a reason
    would put the recovery read back where it started.
    """

    capability: BulkCapability
    run_id: str
    chunks: int = 0

    def next_reason(self) -> str:
        reason = bulk.chunk_reason(self.run_id, self.chunks)
        self.chunks += 1
        return reason


@dataclass
class _Chunk:
    """Drafted features waiting for the one request they will travel in.

    Both limits are the deployment's own, from its capability document. Chunking against
    a locally chosen number is how a founding import discovers a 413 on its first
    request -- and the byte limit is the one that actually binds here, because a single
    compound boundary is far larger than a cooling unit and a fixed feature count says
    nothing about how many bytes it is.
    """

    max_features: int
    max_bytes: int
    subjects: list[str] = field(default_factory=list)
    features: list[Mapping[str, Any]] = field(default_factory=list)
    used: int = bulk.ENVELOPE_BYTES

    def __len__(self) -> int:
        return len(self.features)

    def full_for(self, size: int) -> bool:
        """True when this chunk must be sent before a feature of `size` bytes is added.

        An empty chunk is never full, however large the feature: one that exceeds the body
        limit on its own travels alone and is refused by the server *naming that row*,
        which is a report the operator can act on. Silently dropping it here, or growing a
        second size rule to catch it, would produce a feature that is missing from the
        dataset with nothing anywhere saying which one.
        """
        if not self.features:
            return False
        return len(self.features) >= self.max_features or self.used + size + 1 > self.max_bytes

    def add(self, subject: str, feature: Mapping[str, Any], size: int) -> None:
        self.subjects.append(subject)
        self.features.append(feature)
        # The separator is counted with the feature rather than between them: one byte
        # over-counted on the first is a budget that binds slightly early, which is the
        # side of the line to be wrong on.
        self.used += size + 1

    def clear(self) -> None:
        self.subjects.clear()
        self.features.clear()
        self.used = bulk.ENVELOPE_BYTES


def _discover_bulk(request: PublishRequest, feedback: QgsFeedback | None) -> _BulkRun | None:
    """Ask the backend whether it can take a batch. **Never fails the run.**

    Every outcome that is not a usable capability document degrades to one feature per
    request: a 404 from a backend that predates the endpoint, a document this cannot read,
    a timeout, an unreachable host. None of them is an error and none of them is worth a
    message bar -- the fallback is correct, only slower, and a capability probe that could
    abort a publish would make the run less reliable than it was before the optimisation
    existed.

    Support is never inferred from a bulk POST that happened not to fail, which is why
    this asks. The endpoint lives inside the backend's ``v1/`` namespace precisely so that
    a backend without it answers a plain 404 rather than having the request proxied to the
    feature service, where "unknown path" and "endpoint failed" look alike.
    """
    if not request.capabilities_path:
        return None
    try:
        document = client.fetch_capabilities(
            request.base_url,
            request.capabilities_path,
            request.authcfg,
            feedback,
            track=request.track,
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring: the run must survive this
        log(f"No bulk create advertised at {request.capabilities_path!r} ({exc}).")
        return None

    capability = bulk.parse_capabilities(document)
    if capability is None:
        log("The backend advertises no atomic bulk create; publishing one feature per request.")
        return None
    log(
        f"Publishing in batches of up to {capability.chunk_features(request.chunk_size)} "
        f"feature(s) through the backend's atomic bulk create."
    )
    return _BulkRun(capability=capability, run_id=request.run_id)


def _post_chunk(
    request: PublishRequest,
    collection_id: str,
    chunk: _Chunk,
    reason: str,
    run: _BulkRun,
    feedback: QgsFeedback | None,
) -> bulk.ChunkVerdict:
    """POST one chunk, waiting out the edge's rate limiter rather than failing on it.

    The 429 retry is safe in a way that retrying anything else is not, and the reason is
    the same one that makes this endpoint acceptable at all: the limiter refuses the
    request *before* the transaction is opened, so a throttled chunk certainly created
    nothing. Every retry re-uses the SAME ``reason``, so the recovery read still asks one
    question about one chunk however many attempts it took.
    """
    attempts = max(1, request.max_throttle_retries + 1)
    for attempt in range(attempts):
        if feedback is not None and feedback.isCanceled():
            return bulk.ChunkVerdict(state=bulk.NOT_SENT)
        try:
            payload = client.create_features(
                request.base_url,
                collection_id,
                chunk.features,
                request.authcfg,
                feedback,
                track=request.track,
                reason=reason,
                path=run.capability.path,
            )
        except BackendError as exc:
            if feedback is not None and feedback.isCanceled():
                # The socket was aborted because the user pressed cancel -- but the
                # request may already have been committed on the other side, and unlike a
                # single feature there are up to a few hundred of them. Nobody refused
                # these and nobody can say they landed, which is exactly what UNKNOWN is
                # for; calling it "never sent" would invite the re-run that duplicates it.
                return bulk.ChunkVerdict(
                    state=bulk.UNKNOWN,
                    detail="the run was cancelled while this batch was in flight",
                )
            if exc.throttled:
                if attempt == attempts - 1:
                    # Still throttled after every attempt. Read as a refusal it would say
                    # the server rejected these features, which it did not: it never
                    # looked at them. Nothing was created, and nothing about the rows is
                    # wrong -- so they are not-created, and the sentence says the limiter.
                    return bulk.ChunkVerdict(
                        state=bulk.NOT_CREATED,
                        detail=(
                            f"the auth edge was still rate-limiting after {attempts} "
                            f"attempt(s), so this batch was never accepted: {exc}"
                        ),
                    )
                delay = exc.retry_after if exc.retry_after else 2.0 * (attempt + 1)
                log_warning(
                    f"Rate limited by the auth edge; waiting "
                    f"{min(delay, MAX_BACKOFF_SECONDS):.0f}s before offering the same "
                    f"batch again (attempt {attempt + 1} of {attempts})."
                )
                if not _wait(delay, feedback):
                    # Cancelled during the backoff. The 429 already established that
                    # nothing was written, so this is a batch that never landed and that
                    # nobody refused.
                    return bulk.ChunkVerdict(state=bulk.NOT_SENT)
                continue
            return bulk.read_failure(exc.status, exc.payload, str(exc), len(chunk))
        return bulk.read_success(payload, len(chunk))
    return bulk.ChunkVerdict(state=bulk.NOT_SENT)  # pragma: no cover - the loop returns first


def _send_chunk(
    request: PublishRequest,
    collection_id: str,
    chunk: _Chunk,
    outcome: LayerOutcome,
    run: _BulkRun,
    feedback: QgsFeedback | None,
) -> None:
    """Publish one chunk in a single transaction and fold the result into the outcome.

    THERE IS NO PER-FEATURE RETRY HERE, AND THAT IS THE POINT

    A refused chunk created nothing -- the server says so, in the response, as data -- and
    the fix is to correct the row it names and publish again. Re-offering the chunk one
    feature at a time is the exact loop that made duplicates, and it would also make the
    ``created`` count meaningless: whatever this reports as published has to be what the
    server said it created, not what this side hoped.

    So each of the five verdicts maps to exactly one counter, and none of them maps to
    "try again":

    * the server created them -- credit its count, not the chunk's length;
    * it refused, naming a row -- that row failed, the rest were not created;
    * it refused without naming one -- none were created, and its sentence says why;
    * it never went out -- nobody refused them and nothing landed;
    * nothing came back that establishes either -- unverified, and the report says how to
      find out with one read rather than by re-sending.
    """
    if not chunk:
        return
    count = len(chunk)
    if feedback is not None and feedback.isCanceled():
        # Already stopping. Sending would be up to a few hundred writes the user asked not
        # to make.
        outcome.not_sent += count
        return

    reason = run.next_reason()
    verdict = _post_chunk(request, collection_id, chunk, reason, run, feedback)

    if verdict.state == bulk.CREATED:
        outcome.published += verdict.created
        return
    if verdict.state == bulk.NOT_SENT:
        outcome.not_sent += count
        return
    if verdict.state == bulk.UNKNOWN:
        outcome.unverified += count
        outcome.note(
            f"{count} feature(s) were sent as one batch and the answer never arrived, so "
            "whether they were created is UNKNOWN: "
            f"{verdict.detail}. Do NOT simply publish this layer again - the server "
            "assigns identity, so a batch that did land cannot be recognised and would be "
            "duplicated. The batch was recorded under the edit reason "
            f"{reason!r}; a single query of the history collection for that exact reason "
            "answers it"
        )
        return
    if verdict.state == bulk.REFUSED and verdict.at is not None:
        subject = chunk.subjects[verdict.at]
        outcome.failed += 1
        outcome.note(f"the server refused a feature: {verdict.detail}", subject)
        if count > 1:
            outcome.not_created += count - 1
            outcome.note(
                "not created: the batch this feature was in is applied as one "
                "transaction, and another feature in it was refused. Nothing about this "
                "row was rejected; fix the refusal above and publish again",
                subject,
            )
        return
    # NOT_CREATED, or a refusal that named no row: nothing landed, and no feature in the
    # chunk is being blamed for it.
    outcome.not_created += count
    outcome.note(f"none of this batch was created: {verdict.detail}")


def _extent_refusal(outcome: LayerOutcome, completeness: str) -> str:
    """Why this layer's run has not earned the extent the user asked for, or ``""``.

    An extent is a claim about ground, and the run is the only evidence for it. Two
    separate checks, because the two values claim different things:

    * *nothing* published means there is no sweep to declare at all. The previous version
      declared the extent from the checkbox alone, so a layer whose features were 100%
      refused still wrote an exhaustive survey claim over its bounding box -- and
      ``classes_without_extent`` could not catch it, because that set is built from the
      classes that *did* publish.
    * ``exhaustive`` additionally means "everything of this class inside the polygon is
      labeled". A feature that was refused, or skipped as unshapeable, or drafted and never
      sent, is a thing on the ground that is not in the database, so the claim is false by
      inspection. A shapefile null shape is not: there was nothing on the ground to miss.
    """
    if not outcome.published:
        return (
            "no survey extent was declared: nothing from this layer was published, so "
            "there is no sweep to record"
        )
    if completeness != COMPLETENESS_EXHAUSTIVE:
        return ""
    unrecorded = outcome.failed + outcome.skipped_invalid_geometry + outcome.skipped_unshapeable
    unrecorded += outcome.not_sent
    # A feature whose batch was refused, and one whose batch got no answer, are both
    # "a thing on the ground that this publish cannot show in the database". The second
    # is the sharper case: an exhaustive claim resting on features that MIGHT be there is
    # a claim nobody can check, and it licenses the export pipeline to treat everything
    # else inside the box as background.
    unrecorded += outcome.not_created + outcome.unverified
    if unrecorded:
        return (
            f"no survey extent was declared: {unrecorded} feature(s) of this layer did not "
            "reach the database, so the ground inside the box holds labels this publish "
            "did not record and the sweep is not exhaustive. Fix those and re-declare, or "
            "declare the extent as partial"
        )
    return ""


def _declare_extent(
    request: PublishRequest,
    prepared: PreparedLayer,
    outcome: LayerOutcome,
    feedback: QgsFeedback | None,
) -> tuple[bool, str]:
    """Create the ``labeled_extent`` the user asked for, if the run earned it.

    ``completeness`` comes from the user's own choice and is never assumed here. It is the
    one field on this table that changes what a *training run* does: only ``exhaustive``
    licenses the export pipeline to sample unlabeled ground inside the polygon as negative,
    and the collection was read-only until this workflow existed precisely because of it.
    A tool that writes ``exhaustive`` whenever a box is ticked has answered the question
    rather than asked it -- over a bounding box, which for a layer spanning six provinces
    is millions of square kilometres of supervised background nobody swept.
    """
    if prepared.extent_geojson is None:
        return False, prepared.extent_problem
    if not request.extent_collection:
        return False, "no survey-extent collection is configured, so no extent was declared"

    completeness = prepared.plan.choice.extent_completeness
    if not completeness:  # pragma: no cover - callers check declare_extent first
        return False, ""
    refusal = _extent_refusal(outcome, completeness)
    if refusal:
        return False, refusal

    fields = request.fields
    feature = {
        "type": "Feature",
        "geometry": dict(prepared.extent_geojson),
        "properties": {
            fields.class_id: prepared.plan.class_id,
            fields.completeness: completeness,
            # The caveat is not decoration, and it is also not enough on its own: nothing
            # in the export pipeline reads it, so it qualifies the claim for humans while
            # `completeness` above is what machines act on. Recording that this particular
            # polygon is a bounding box rather than a drawn sweep is what a person reading
            # the row later needs in order to narrow it.
            fields.caveat: (
                "Derived from the bounding box of the source layer "
                f"{prepared.plan.source.name!r} during the bootstrap publish, not from a "
                "drawn survey boundary. Replace it with the area actually swept. No "
                "imagery capture is recorded: the shapefiles do not say which capture "
                "they were drawn against, and a sweep is only true of the imagery it was "
                "done on."
            ),
        },
    }
    try:
        client.create_feature(
            request.base_url,
            request.extent_collection,
            feature,
            request.authcfg,
            feedback,
            track=request.track,
        )
    except BackendError as exc:
        return False, f"the survey extent was refused by the server: {exc}"
    return True, ""


@dataclass
class _Progress:
    """Feature-level progress, reported through the feedback the task owns.

    Only whole percentage points are emitted. ``setProgress`` fires a signal that crosses
    a thread boundary, and doing that once per feature would put a thousand queued events
    in front of the main thread's event loop for no visible gain.

    Counted when a feature is ACCOUNTED FOR rather than when it is read, which is the
    difference between a bar and a decoration once features travel in batches: drafting
    1,807 rows off a local shapefile takes about a second, so a bar driven by reads would
    reach 100% almost immediately and then sit there for the whole publish.
    """

    total: int
    feedback: QgsFeedback | None = None
    done: int = 0
    last_percent: int = -1

    def advance(self, count: int = 1) -> None:
        self.done += count
        if self.feedback is None or self.total <= 0:
            return
        percent = min(100, int(100 * self.done / self.total))
        if percent != self.last_percent:
            self.last_percent = percent
            self.feedback.setProgress(float(percent))

    def step(self) -> None:
        self.advance(1)


def _draft_feature(
    request: PublishRequest,
    prepared: PreparedLayer,
    feature: QgsFeature,
    values: Mapping[str, Any],
    subject: str,
    outcome: LayerOutcome,
) -> Mapping[str, Any] | None:
    """One feature as the GeoJSON that will be sent, or ``None`` with the reason counted.

    Split out from the loop so that a feature is drafted in one place whether it is going
    to be sent on its own or in a batch. Everything it refuses, it refuses *by name*: a
    deduplicated count of 190 is not something anybody can act on, and after a partial run
    into a server that assigns identity it is the only way to work out what still needs
    sending.
    """
    geometry = QgsGeometry(feature.geometry())
    if geometry.isNull() or geometry.isEmpty():
        # A shapefile "Null shape": an attribute row with nothing on the ground.
        # label.geom is NOT NULL and there is nothing here to invent.
        outcome.skipped_no_geometry += 1
        return None
    if prepared.transform is not None:
        try:
            geometry.transform(prepared.transform)
        except QgsCsException as exc:
            outcome.skipped_unshapeable += 1
            outcome.note(f"could not be reprojected to {STORAGE_CRS}: {exc}", subject)
            return None
    if not geometry.isGeosValid():
        # The server's own trigger would reject it. Failing here names the feature
        # rather than letting the database refuse it as an untraceable HTTP 500. In a
        # batch it matters more: one invalid geometry would take its whole chunk with it.
        outcome.skipped_invalid_geometry += 1
        outcome.note("invalid geometry, rejected before sending", subject)
        return None

    result = build_draft(
        values,
        geometry_as_geojson(geometry),
        prepared.plan.label_class,
        prepared.mappings,
        skip_damaged_names=prepared.plan.choice.skip_damaged_names,
    )
    for message in result.issues:
        outcome.note(message, subject)
    if result.draft is None:
        outcome.skipped_unshapeable += 1
        return None

    outcome.promoted += int(result.promoted)
    outcome.flattened += int(result.flattened)
    outcome.damaged_names += int(bool(result.damaged_names))
    outcome.omitted_names += int(bool(result.omitted_names))
    return result.draft.to_geojson(request.fields)


def _publish_layer(
    request: PublishRequest,
    prepared: PreparedLayer,
    report: PublishReport,
    progress: _Progress,
    feedback: QgsFeedback | None,
    run: _BulkRun | None = None,
) -> LayerOutcome:
    """Publish one layer's features. Returns its outcome; never raises for a bad row."""
    plan = prepared.plan
    collection_id = request.target_for(prepared)
    outcome = report.outcome_for(
        plan.source.name, plan.class_id, plan.source.feature_count, collection_id
    )
    outcome.reprojected = prepared.transform is not None
    if plan.label_class is None:  # pragma: no cover - prepare() filters these out
        return outcome

    # Per layer, not per run. A chunk is one request to one collection, and this run may
    # be writing to several; a chunk spanning two layers would also have to carry two sets
    # of subjects, and the index a refusal names would stop meaning anything.
    chunk = None
    if run is not None and run.capability.serves(collection_id):
        chunk = _Chunk(
            max_features=run.capability.chunk_features(request.chunk_size),
            max_bytes=run.capability.max_body_bytes,
        )

    def flush() -> None:
        if not chunk:
            return
        pending = len(chunk)
        _send_chunk(request, collection_id, chunk, outcome, run, feedback)
        chunk.clear()
        progress.advance(pending)

    for feature in prepared.source.getFeatures():
        if feedback is not None and feedback.isCanceled():
            report.cancelled = True
            break
        outcome.read += 1

        # Named before anything can go wrong with it, so every complaint can say WHICH
        # row -- including a refusal that arrives later, attached to a batch, and is
        # matched back to this feature by its index in the chunk.
        values = feature_values(feature, prepared.field_names)
        subject = _subject(values, prepared.mappings, outcome.read)

        draft = _draft_feature(request, prepared, feature, values, subject, outcome)
        if draft is None:
            progress.step()
            continue

        if chunk is None:
            _send(request, collection_id, draft, subject, outcome, feedback)
            progress.step()
            continue

        size = bulk.encoded_size(draft)
        if chunk.full_for(size):
            flush()
        chunk.add(subject, draft, size)

    # Whatever is still queued goes now, including after a cancellation: _send_chunk sees
    # the cancelled feedback and counts them as never sent, which is what they are. Losing
    # them silently would leave a layer whose counts do not add up to what was read.
    flush()
    return outcome


def publish(request: PublishRequest, feedback: QgsFeedback | None = None) -> PublishReport:
    """Publish every prepared layer. **Worker thread.**

    Nothing here raises for a per-feature problem. A bootstrap that aborts on row 900 has
    published 899 features, told the user nothing about which, and left a backend that
    cannot be cleanly re-run into -- so every failure is counted, attributed and carried to
    the end. Only a caller error (no URL, no collection) raises, and it raises before
    anything is sent.

    That reasoning does not stop at the errors this code anticipated. A ``RuntimeError``
    from a layer the user removed mid-run, or any other unforeseen exception, has exactly
    the same property: some of it already happened, on a server. Letting it escape loses
    the whole report -- the task wrapper keeps the traceback and discards the result -- so
    the layer loop records the failure into the report and returns what did land.
    """
    if not request.base_url:
        raise ConfigurationError("No backend URL configured.")
    unrouted = request.unrouted()
    if unrouted:
        # Per layer, because the destination is per layer. Raised before anything is sent
        # rather than defaulted to whichever collection the request happened to name: a
        # point published into the polygon collection is refused by app.label_check()
        # feature by feature, and 872 of those read as a server fault rather than as a
        # layer that never had anywhere to go.
        raise ConfigurationError(
            "No collection chosen to publish into for: " + ", ".join(unrouted) + "."
        )
    if not request.track:
        # Refused here as well as in the preview, and the reason is the same one that put
        # the URL and collection checks here: this function is reachable without the
        # dialog. The alternative is 1,246 identical 403s, each of which has already
        # cost a round trip, and a report that reads like a backend outage.
        raise ConfigurationError(
            "No history track selected, so there is no way to say which dataset these "
            "features would join. Choose a track in the panel before publishing."
        )

    report = PublishReport(track=request.track)
    progress = _Progress(total=request.total_features(), feedback=feedback)
    # Once, before the run, and never inferred from a write that happened not to fail.
    # Every way of failing to get an answer here ends at the one-feature-per-request path,
    # which is correct against a backend that has not been updated -- see _discover_bulk.
    run = _discover_bulk(request, feedback)
    for prepared in request.layers:
        # The extent POST is inside the same guard as the features, because it has the
        # same property: by the time it runs, this layer's rows are already on the server,
        # and an exception that escapes here would discard the report of all of them.
        try:
            outcome = _publish_layer(request, prepared, report, progress, feedback, run)
            # _publish_layer only notices a cancellation between features, so a run stopped
            # during a layer's last request would otherwise finish reporting itself as a
            # complete one -- and would go on to declare a survey extent for a sweep that
            # was interrupted.
            if feedback is not None and feedback.isCanceled():
                report.cancelled = True
            if prepared.plan.choice.declare_extent:
                if report.cancelled:
                    outcome.extent_problem = (
                        "no survey extent was declared: the run was cancelled before this "
                        "layer finished, so the box was not swept by this publish"
                    )
                else:
                    outcome.extent_declared, outcome.extent_problem = _declare_extent(
                        request, prepared, outcome, feedback
                    )
            elif prepared.extent_problem:
                outcome.extent_problem = prepared.extent_problem
        except Exception as exc:  # noqa: BLE001 - see the docstring: the report survives
            log_warning(f"Publish stopped by an unexpected error:\n{traceback.format_exc()}")
            report.error = f"{type(exc).__name__}: {exc}"
            break
        if report.cancelled:
            break

    # Per layer, then reduced to class names. An extent covers an area, so a class that
    # got one from a campus-sized layer has told us nothing about a second layer of the
    # same class covering the rest of the country; subtracting class ids would let the
    # first silence the warning for the second.
    report.classes_without_extent = tuple(
        sorted(
            {
                outcome.class_id
                for outcome in report.outcomes
                if outcome.published and outcome.class_id and not outcome.extent_declared
            }
        )
    )
    log(f"Publish finished: {report.summary()}")
    return report
