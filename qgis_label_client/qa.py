"""QA affordances: coverage against ``labeled_extent``.

The check answers one question -- "have I drawn labels on ground I never declared as
swept for this class?" -- and answers it while the analyst still remembers, because the
knowledge cannot be reconstructed later. See :mod:`.core.coverage` for why that matters
more than it sounds.

A note on CRS, because the rule is absolute and this looks like it breaks it: nothing
here measures anything. ``intersects`` is a topological predicate and is CRS-safe
provided both geometries are in the *same* CRS, which is what the transform below
guarantees. Area and length are never computed in EPSG:4326 anywhere in this plugin --
that work belongs in a projected CRS (EPSG:32649 for Ulanqab, an equal-area conic for
anything spanning the seven UTM zones between 84E and 125E) and it belongs server-side,
not in a QA button.
"""

from __future__ import annotations

from collections.abc import Iterable

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsSpatialIndex,
    QgsVectorLayer,
)

from .core.coverage import (
    CoverageReport,
    ExtentRef,
    LabelRef,
    build_report,
    extent_from_properties,
    label_from_properties,
)
from .core.errors import ConfigurationError
from .core.registry import ClassRegistry


def _feature_properties(feature: QgsFeature) -> dict[str, object]:
    """A feature's attributes as a plain mapping, so the pure core can read them."""
    return {field.name(): feature[field.name()] for field in feature.fields()}


def _load_extents(
    extent_layer: QgsVectorLayer,
    registry: ClassRegistry,
    target_crs,
) -> tuple[dict[str, ExtentRef], QgsSpatialIndex, dict[str, QgsGeometry]]:
    """Index the survey extents, reprojected into the label layer's CRS if needed."""
    transform = None
    if extent_layer.crs() != target_crs:
        transform = QgsCoordinateTransform(
            extent_layer.crs(), target_crs, QgsProject.instance().transformContext()
        )

    extents: dict[str, ExtentRef] = {}
    geometries: dict[str, QgsGeometry] = {}
    index = QgsSpatialIndex()

    for feature in extent_layer.getFeatures():
        ref = extent_from_properties(
            _feature_properties(feature), registry.fields, fallback_id=feature.id()
        )
        if ref is None:
            continue
        geometry = QgsGeometry(feature.geometry())
        if geometry.isEmpty():
            continue
        if transform is not None:
            geometry.transform(transform)
        # Key on the feature id: extent_id may be absent, and the spatial index works in
        # feature ids anyway.
        key = str(feature.id())
        extents[key] = ref
        geometries[key] = geometry
        indexed = QgsFeature(feature.id())
        indexed.setGeometry(geometry)
        index.addFeature(indexed)

    return extents, index, geometries


def check_coverage(
    label_layer: QgsVectorLayer,
    extent_layer: QgsVectorLayer,
    registry: ClassRegistry,
    selected_only: bool = False,
) -> tuple[CoverageReport, dict[int, str]]:
    """Classify labels against declared survey extents.

    Returns the report and a map from QGIS feature id to coverage value, so the caller
    can select the uncovered features on the canvas -- which is what makes the finding
    actionable rather than a number in a dialog.
    """
    if label_layer is None or extent_layer is None:
        raise ConfigurationError(
            "The coverage check needs both the label layer and the labeled_extent layer "
            "loaded. Load them from the Collections list."
        )

    extents_by_key, index, geometries = _load_extents(extent_layer, registry, label_layer.crs())
    if not extents_by_key:
        raise ConfigurationError(
            "The labeled_extent layer has no usable features in view. Nothing has been "
            "declared as surveyed, so every label is on unknown ground."
        )

    features: Iterable[QgsFeature] = (
        label_layer.getSelectedFeatures() if selected_only else label_layer.getFeatures()
    )

    labels: list[LabelRef] = []
    hits: dict[object, set[str]] = {}
    coverage_by_fid: dict[int, str] = {}

    for feature in features:
        ref = label_from_properties(
            _feature_properties(feature), registry.fields, feature_id=feature.id()
        )
        if ref is None:
            continue
        geometry = feature.geometry()
        if geometry.isEmpty():
            continue
        candidate_keys = {str(fid) for fid in index.intersects(geometry.boundingBox())}
        # The index gives bounding-box candidates; confirm with a real intersection, or a
        # label just outside a rectangular-ish extent would be counted as covered.
        hits[ref.feature_id] = {
            key
            for key in candidate_keys
            if key in geometries and geometry.intersects(geometries[key])
        }
        labels.append(ref)

    # Two survey extents can be value-equal (same class, same completeness, blank id) and
    # still be different polygons, so the predicate resolves them by object identity
    # rather than equality.
    extent_list = list(extents_by_key.values())
    key_by_extent = {id(ref): key for key, ref in extents_by_key.items()}

    def intersects(label: LabelRef, extent: ExtentRef) -> bool:
        key = key_by_extent.get(id(extent))
        return key is not None and key in hits.get(label.feature_id, ())

    report = build_report(labels, extent_list, intersects)
    for finding in report.findings:
        feature_id = finding.label.feature_id
        if isinstance(feature_id, int):
            coverage_by_fid[feature_id] = finding.coverage.value
    return report, coverage_by_fid
