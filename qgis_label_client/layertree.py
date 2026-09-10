"""Native, collapsible groups for geometry-specific collection layers.

Only tree nodes move. The registered layers, providers, renderers and edit buffers
stay intact, and a user's own groups and later tree rearrangements are respected.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from qgis.core import QgsDataSourceUri, QgsLayerTreeGroup, QgsLayerTreeLayer

from . import layers
from .core.collections import CollectionGroup

GROUP_PROPERTY = "cvi/collection_group"
CONTEXT_PROPERTY = "cvi/collection_group_context"
PLACED_PROPERTY = "cvi/collection_group_placed"


def _context(layer) -> str:
    # Keep the entire landing URL, including temporal queries. Two contexts must not
    # appear interchangeable merely because they advertise the same collection ids.
    url = QgsDataSourceUri(layer.source()).param("url")
    return json.dumps(
        [url, layers.track_of(layer), layers.recorded_at_of(layer)], separators=(",", ":")
    )


def _collection_group(layer, groups: Sequence[CollectionGroup]) -> CollectionGroup | None:
    collection_id = layers.collection_of(layer)
    return next(
        (
            group
            for group in groups
            if len(group.members) > 1 and collection_id in group.collection_ids
        ),
        None,
    )


def _find_group(root, metadata: CollectionGroup, context: str):
    for group in root.findGroups():
        if group.customProperty(GROUP_PROPERTY, "") != metadata.stem:
            continue
        nodes = group.findLayers()
        if nodes:
            # Track/time transitions repoint existing layers. Consult their current
            # context instead of a stale group property saved before the transition.
            if all(
                node.layer() is not None
                and layers.collection_of(node.layer()) in metadata.collection_ids
                and _context(node.layer()) == context
                for node in nodes
            ):
                return group
        elif group.customProperty(CONTEXT_PROPERTY, "") == context:
            return group
    return None


def _ensure_group(root, metadata: CollectionGroup, context: str, index: int = 0):
    group = _find_group(root, metadata, context)
    if group is None:
        group = root.insertGroup(index, metadata.display_name)
        group.setCustomProperty(GROUP_PROPERTY, metadata.stem)
        group.setCustomProperty(CONTEXT_PROPERTY, context)
        group.setExpanded(False)
    return group


def add_collection_layer(project, layer, groups: Sequence[CollectionGroup]) -> None:
    """Register a new layer under its mode group; ordinary collections stay flat."""
    metadata = _collection_group(layer, groups)
    if metadata is None:
        project.addMapLayer(layer)
        return
    group = _ensure_group(project.layerTreeRoot(), metadata, _context(layer))
    project.addMapLayer(layer, False)
    node = group.addLayer(layer)
    node.setCustomProperty(PLACED_PROPERTY, True)


def group_existing_layers(project, groups: Sequence[CollectionGroup]) -> None:
    """Adopt existing root siblings once, leaving user-managed arrangements alone.

    Clone the tree node before removing its old location, so the project bridge sees
    another reference and never removes the underlying layer. Its checked/expanded
    states and custom node properties travel with the clone.
    """
    root = project.layerTreeRoot()
    for node in list(root.children()):
        if not isinstance(node, QgsLayerTreeLayer) or node.customProperty(PLACED_PROPERTY, False):
            continue
        layer = node.layer()
        if layer is None:
            continue
        metadata = _collection_group(layer, groups)
        if metadata is None:
            continue
        index = root.children().index(node)
        group = _ensure_group(root, metadata, _context(layer), index)
        clone = node.clone()
        clone.setCustomProperty(PLACED_PROPERTY, True)
        group.addChildNode(clone)
        root.removeChildNode(node)

    # Mark recognized layers already inside a manual group too. If the user later
    # drags one out, Connect must not interpret that choice as unfinished migration.
    for group in root.findGroups():
        if not isinstance(group, QgsLayerTreeGroup):
            continue
        for node in group.findLayers():
            if node.layer() is not None and _collection_group(node.layer(), groups) is not None:
                node.setCustomProperty(PLACED_PROPERTY, True)
