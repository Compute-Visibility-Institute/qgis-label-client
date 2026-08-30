"""Repointing raster layers at freshly signed imagery URLs.

This is the reason the plugin exists at all. Vectors need no plugin code -- QGIS's native
OAPIF provider reads and writes them. Imagery does, because the access grant expires.

The imagery is Maxar Limited Rights Data in a private bucket, streamed by GDAL over HTTP
range requests from a short-lived signed URL. There is no way to write "a URL, but fetch
it fresh" into a ``.qgz``, so a project saved on Monday has four dead raster layers on
Tuesday. At session start the plugin mints new URLs and swaps them in.

Everything here runs on the **main thread**: it touches ``QgsProject`` and layer objects.
The network call that produced the assets ran in a task; this is its ``finished()`` half.
"""

from __future__ import annotations

from qgis.core import Qgis, QgsProject, QgsRasterLayer
from qgis.PyQt.QtXml import QDomDocument

from .core.assets import (
    ASSET_KEY_PROPERTY,
    CAPTURED_AT_PROPERTY,
    RasterLayerRef,
    Rewrite,
    SignedAsset,
    gcs_object_key,
    plan_rewrites,
)
from .log import log, log_url, log_warning


def collect_raster_layers(project: QgsProject | None = None) -> list[RasterLayerRef]:
    """Snapshot the raster layers that could be imagery.

    Layers whose source is not recognisably a GCS object and which carry no asset key are
    excluded here rather than in the matcher, so a basemap or a local DEM is never a
    candidate for rewriting.
    """
    project = project or QgsProject.instance()
    refs: list[RasterLayerRef] = []
    for layer in project.mapLayers().values():
        if layer.type() != Qgis.LayerType.Raster:
            continue
        asset_key = layer.customProperty(ASSET_KEY_PROPERTY, "")
        asset_key = str(asset_key) if asset_key else None
        source = layer.source()
        if not asset_key and gcs_object_key(source) is None:
            continue
        refs.append(
            RasterLayerRef(
                layer_id=layer.id(),
                name=layer.name(),
                source=source,
                asset_key=asset_key,
            )
        )
    return refs


def repoint_raster(layer: QgsRasterLayer, new_source: str) -> bool:
    """Swap a raster layer's source, keeping its renderer.

    The renderer is the whole point of the analysis COG. QGIS reads the 4-band UInt16
    product and does band selection itself, so false-colour NIR is a renderer setting --
    bands 4-3-2 with a contrast stretch -- and nothing else. ``setDataSource`` rebuilds
    the provider and does not reliably preserve that, so a refresh would quietly reset a
    NIR layer to natural colour and the analyst would only notice by the picture looking
    wrong.

    Export the style, swap, re-import.
    """
    document = QDomDocument()
    layer.exportNamedStyle(document)

    layer.setDataSource(new_source, layer.name(), layer.providerType() or "gdal")

    if not layer.isValid():
        log_warning(f"Layer {layer.name()!r} is invalid after re-pointing.")
        return False

    restored, message = layer.importNamedStyle(document)
    if not restored:
        # Worth a warning rather than a failure: the pixels are back, the rendering may
        # need a manual fix.
        log_warning(f"Renderer not restored on {layer.name()!r}: {message}")
    layer.triggerRepaint()
    return True


def refresh_sources(
    assets: list[SignedAsset],
    project: QgsProject | None = None,
) -> tuple[list[Rewrite], list[RasterLayerRef], int]:
    """Repoint every matching raster layer. Returns (planned, unmatched, applied).

    ``unmatched`` is returned rather than swallowed. "The imagery did not refresh" has to
    be a visible outcome: a layer left on an expired URL renders from GDAL's cache for a
    while and looks fine right up until it doesn't.
    """
    project = project or QgsProject.instance()
    layers = collect_raster_layers(project)
    rewrites, unmatched = plan_rewrites(layers, assets)

    applied = 0
    for rewrite in rewrites:
        layer = project.mapLayer(rewrite.layer_id)
        if layer is None:
            continue
        # Log the object path, never the signed URL -- it is the access grant.
        log_url(f"Re-pointing {rewrite.layer_name!r} ({rewrite.matched_by})", rewrite.new_source)
        if repoint_raster(layer, rewrite.new_source):
            # Only stamp a key that identifies one scene. An asset the backend sent
            # without a stac_id has none, and writing a placeholder would bind this layer
            # to whichever unnamed asset happens to sort first on the next refresh.
            if rewrite.asset.key:
                layer.setCustomProperty(ASSET_KEY_PROPERTY, rewrite.asset.key)
            # The acquisition instant, stamped for core.validtime to read back off
            # the layer tree. Only written when the backend actually sent one --
            # clearing it otherwise, rather than leaving a stale value, because a
            # date belonging to the scene this layer USED to show would produce a
            # confidently wrong default rather than an absent one.
            if rewrite.asset.captured_at is not None:
                layer.setCustomProperty(CAPTURED_AT_PROPERTY, rewrite.asset.captured_at.isoformat())
            else:
                layer.removeCustomProperty(CAPTURED_AT_PROPERTY)
            applied += 1

    for layer_ref in unmatched:
        log_warning(
            f"No signed URL matched raster layer {layer_ref.name!r}. It will keep using "
            f"its existing source, which may have expired. Set the "
            f"'{ASSET_KEY_PROPERTY}' layer property to bind it explicitly."
        )
    log(f"Imagery refresh: {applied} layer(s) re-pointed, {len(unmatched)} unmatched.")
    return rewrites, unmatched, applied
