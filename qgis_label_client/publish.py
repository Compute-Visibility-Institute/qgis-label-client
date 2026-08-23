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
"""

from __future__ import annotations

import json
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
from .core.errors import BackendError, ConfigurationError
from .core.fields import COMPLETENESS_EXHAUSTIVE, CoreFields
from .core.legacy import FieldMapping, map_fields, name_columns
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


def describe_layer(layer: QgsVectorLayer, scan_names: bool = True) -> SourceLayer:
    """Everything the preview dialog needs about one local layer."""
    damaged, scanned = count_damaged_names(layer) if scan_names else (0, 0)
    return SourceLayer(
        layer_id=layer.id(),
        name=layer.name(),
        geometry_type=geometry_type_name(layer),
        crs_authid=str(layer.crs().authid() or ""),
        feature_count=max(int(layer.featureCount()), 0),
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

        transform = None
        if layer.crs() != target:
            # Built here because it needs the project's transform context. Afterwards it
            # is a value object and crosses the thread boundary safely.
            transform = QgsCoordinateTransform(layer.crs(), target, project.transformContext())

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
            )
        )
    return prepared


def stamp_published(
    plans: Iterable[LayerPlan],
    report: PublishReport,
    collection_id: str,
    project: QgsProject | None = None,
) -> None:
    """Record on each layer that it has been published. Main thread only.

    Written after the run rather than before, and written even for a partial run, because
    the warning it produces next time is "some of this is already up there" -- which is
    exactly the state a partial run leaves behind.
    """
    project = project or QgsProject.instance()
    published_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

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
        previous = plan.source.previous.feature_count if plan.source.previous else 0
        layer.setCustomProperty(
            PUBLISHED_PROPERTY,
            format_record(
                PublishRecord(
                    published_at=published_at,
                    collection_id=collection_id,
                    class_id=outcome.class_id,
                    feature_count=previous + outcome.published,
                )
            ),
        )


# ---------------------------------------------------------------------------
# Doing the work (worker thread)
# ---------------------------------------------------------------------------


@dataclass
class PublishRequest:
    """Everything :func:`publish` needs. Built on the main thread, read on the worker."""

    base_url: str
    collection_id: str
    authcfg: str
    layers: list[PreparedLayer] = field(default_factory=list)
    extent_collection: str = ""
    fields: CoreFields = field(default_factory=CoreFields)
    batch_size: int = 50
    #: Cleared for the rest of the run once the server is shown not to accept a
    #: FeatureCollection body. Mutable state on the request rather than a constant,
    #: because it is discovered rather than configured. See :func:`_send`.
    batch_supported: bool = True

    def total_features(self) -> int:
        return sum(prepared.plan.source.feature_count for prepared in self.layers)


def _send_one(
    request: PublishRequest,
    feature: Mapping[str, Any],
    feedback: QgsFeedback | None,
) -> tuple[int, list[str]]:
    try:
        client.create_feature(
            request.base_url, request.collection_id, feature, request.authcfg, feedback
        )
    except BackendError as exc:
        return 0, [str(exc)]
    return 1, []


def _send_individually(
    request: PublishRequest,
    features: Sequence[Mapping[str, Any]],
    feedback: QgsFeedback | None,
) -> tuple[int, list[str]]:
    published = 0
    errors: list[str] = []
    for feature in features:
        if feedback is not None and feedback.isCanceled():
            break
        sent, problems = _send_one(request, feature, feedback)
        published += sent
        errors += problems
    return published, errors


def _send(
    request: PublishRequest,
    features: Sequence[Mapping[str, Any]],
    feedback: QgsFeedback | None,
) -> tuple[int, list[str]]:
    """POST a batch, falling back to one at a time when the batch is refused.

    A rejected FeatureCollection names no feature, which turns "1,246 features failed" into
    a support ticket. Retrying the batch feature by feature costs one extra round trip per
    failing batch and buys an error message attached to a specific row -- the difference
    between a report somebody can act on and a number.

    If every feature of a refused batch then succeeds on its own, the batch was refused for
    its *shape* rather than its content -- the deployment does not accept a FeatureCollection
    body -- and the rest of the run goes one at a time rather than paying for a doomed batch
    before every retry.
    """
    if not features:
        return 0, []
    if len(features) == 1 or not request.batch_supported:
        return _send_individually(request, features, feedback)

    try:
        client.create_features(
            request.base_url, request.collection_id, features, request.authcfg, feedback
        )
    except BackendError as exc:
        log_warning(f"Batch of {len(features)} refused, retrying one at a time: {exc}")
    else:
        return len(features), []

    published, errors = _send_individually(request, features, feedback)
    if published == len(features):
        request.batch_supported = False
        log_warning(
            "The server accepted every feature of that batch individually, so it does not "
            "take a FeatureCollection body. Sending one feature per request from here on."
        )
    return published, errors


def _flush(
    request: PublishRequest,
    batch: list[Mapping[str, Any]],
    outcome: LayerOutcome,
    feedback: QgsFeedback | None,
) -> None:
    """Send one batch and fold the result into the layer's outcome."""
    if not batch:
        return
    sent, errors = _send(request, batch, feedback)
    outcome.published += sent
    outcome.failed += len(batch) - sent
    for message in errors:
        outcome.note(f"the server refused a feature: {message}")
    batch.clear()


def _declare_extent(
    request: PublishRequest,
    prepared: PreparedLayer,
    feedback: QgsFeedback | None,
) -> tuple[bool, str]:
    """Create the ``labeled_extent`` the user asked for, if they asked for one."""
    if prepared.extent_geojson is None:
        return False, prepared.extent_problem
    if not request.extent_collection:
        return False, "no survey-extent collection is configured, so no extent was declared"

    fields = request.fields
    feature = {
        "type": "Feature",
        "geometry": dict(prepared.extent_geojson),
        "properties": {
            fields.class_id: prepared.plan.class_id,
            fields.completeness: COMPLETENESS_EXHAUSTIVE,
            # The caveat is not decoration. An extent claims the ground inside it was swept
            # exhaustively, which licenses the export pipeline to treat every unlabeled
            # pixel in it as negative. Recording that this particular polygon is a bounding
            # box rather than a drawn sweep is what keeps that claim honest.
            fields.caveat: (
                "Derived from the bounding box of the source layer "
                f"{prepared.plan.source.name!r} during the bootstrap publish, not from a "
                "drawn survey boundary. Replace it with the area actually swept."
            ),
        },
    }
    try:
        client.create_feature(
            request.base_url, request.extent_collection, feature, request.authcfg, feedback
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
    """

    total: int
    feedback: QgsFeedback | None = None
    done: int = 0
    last_percent: int = -1

    def step(self) -> None:
        self.done += 1
        if self.feedback is None or self.total <= 0:
            return
        percent = min(100, int(100 * self.done / self.total))
        if percent != self.last_percent:
            self.last_percent = percent
            self.feedback.setProgress(float(percent))


def _publish_layer(
    request: PublishRequest,
    prepared: PreparedLayer,
    report: PublishReport,
    progress: _Progress,
    feedback: QgsFeedback | None,
) -> LayerOutcome:
    """Publish one layer's features. Returns its outcome; never raises for a bad row."""
    plan = prepared.plan
    outcome = report.outcome_for(plan.source.name, plan.class_id)
    outcome.reprojected = prepared.transform is not None
    if plan.label_class is None:  # pragma: no cover - prepare() filters these out
        return outcome

    batch: list[Mapping[str, Any]] = []
    batch_size = max(1, request.batch_size)

    for feature in prepared.source.getFeatures():
        if feedback is not None and feedback.isCanceled():
            report.cancelled = True
            break
        outcome.read += 1
        progress.step()

        geometry = QgsGeometry(feature.geometry())
        if geometry.isNull() or geometry.isEmpty():
            # A shapefile "Null shape": an attribute row with nothing on the ground.
            # label.geom is NOT NULL and there is nothing here to invent.
            outcome.skipped_no_geometry += 1
            continue
        if prepared.transform is not None:
            try:
                geometry.transform(prepared.transform)
            except QgsCsException as exc:
                outcome.skipped_unshapeable += 1
                outcome.note(f"could not be reprojected to {STORAGE_CRS}: {exc}")
                continue
        if not geometry.isGeosValid():
            # The server's own trigger would reject it. Failing here names the feature
            # instead of letting one bad row take a batch of fifty good ones down with it.
            outcome.skipped_invalid_geometry += 1
            outcome.note("invalid geometry, rejected before sending")
            continue

        result = build_draft(
            feature_values(feature, prepared.field_names),
            geometry_as_geojson(geometry),
            plan.label_class,
            prepared.mappings,
            skip_damaged_names=plan.choice.skip_damaged_names,
        )
        for message in result.issues:
            outcome.note(message)
        if result.draft is None:
            outcome.skipped_unshapeable += 1
            continue

        outcome.promoted += int(result.promoted)
        outcome.damaged_names += int(bool(result.damaged_names))
        outcome.omitted_names += int(bool(result.omitted_names))

        batch.append(result.draft.to_geojson(request.fields))
        if len(batch) >= batch_size:
            _flush(request, batch, outcome, feedback)

    if not report.cancelled:
        _flush(request, batch, outcome, feedback)
    # A cancelled run drops whatever is still in the batch rather than sending it. Those
    # features were drafted but never left the machine, and counting them as failures
    # would misreport a deliberate stop as a server problem.
    return outcome


def publish(request: PublishRequest, feedback: QgsFeedback | None = None) -> PublishReport:
    """Publish every prepared layer. **Worker thread.**

    Nothing here raises for a per-feature problem. A bootstrap that aborts on row 900 has
    published 899 features, told the user nothing about which, and left a backend that
    cannot be cleanly re-run into -- so every failure is counted, attributed and carried to
    the end. Only a caller error (no URL, no collection) raises, and it raises before
    anything is sent.
    """
    if not request.base_url:
        raise ConfigurationError("No backend URL configured.")
    if not request.collection_id:
        raise ConfigurationError("No collection chosen to publish into.")

    report = PublishReport()
    progress = _Progress(total=request.total_features(), feedback=feedback)
    for prepared in request.layers:
        outcome = _publish_layer(request, prepared, report, progress, feedback)
        if prepared.plan.choice.declare_extent and not report.cancelled:
            outcome.extent_declared, outcome.extent_problem = _declare_extent(
                request, prepared, feedback
            )
        elif prepared.extent_problem:
            outcome.extent_problem = prepared.extent_problem
        if report.cancelled:
            break

    report.classes_without_extent = tuple(
        sorted(
            {o.class_id for o in report.outcomes if o.published and o.class_id}
            - {o.class_id for o in report.outcomes if o.extent_declared}
        )
    )
    log(f"Publish finished: {report.summary()}")
    return report
