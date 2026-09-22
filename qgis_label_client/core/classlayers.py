"""Additive class-layer protocol; legacy collection discovery remains available.

Collection identifiers and field aliases come from the manifest, never from a local
class list. The protocol root is deliberately relative to the authenticated API: a
manifest cannot redirect a provider carrying credentials to another origin.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .collections import Collection
from .errors import BackendError
from .routing import stem_of
from .urls import join_path, normalise_base_url

MANIFEST_PATH = "v1/class-layers"
ROOT_PATH = "class-layers"
METADATA_PROPERTY = "cvi/class_layer"
_ID = re.compile(r"cl_(.+)__(point|line|polygon)\Z")
_GEOMETRY = {"point": "Point", "line": "LineString", "polygon": "Polygon"}


def is_class_collection(collection_id: str) -> bool:
    return _ID.fullmatch(collection_id) is not None


def collection_root(base_url: str, collection_id: str) -> str:
    base = normalise_base_url(base_url)
    return join_path(base, ROOT_PATH) if is_class_collection(collection_id) else base


def parse_manifest(document: object) -> list[Collection]:
    """Fail visibly on malformed enabled metadata; never silently lose classes."""
    if not isinstance(document, Mapping):
        raise BackendError("The class-layer manifest is not an object.")
    if document.get("enabled") is False:
        return []
    if document.get("enabled") is not True or document.get("version") != 1:
        raise BackendError("The server advertised an unsupported class-layer protocol.")
    raw_collections = document.get("collections")
    if not isinstance(raw_collections, list):
        raise BackendError("The class-layer manifest has no collections array.")
    collections = []
    seen = set()
    for raw in raw_collections:
        if not isinstance(raw, Mapping):
            raise BackendError("Invalid class-layer collection metadata.")
        identifier = raw.get("id", "")
        match = _ID.fullmatch(identifier) if isinstance(identifier, str) else None
        if (
            not match
            or identifier in seen
            or raw.get("class_id") != match[1]
            or raw.get("geometry_type") != _GEOMETRY[match[2]]
        ):
            raise BackendError("Invalid or duplicate class-layer identity.")
        field_names = set()
        fields = raw.get("fields")
        if not isinstance(fields, list):
            raise BackendError("The class-layer manifest has no field definitions.")
        for field in fields:
            if (
                not isinstance(field, Mapping)
                or not isinstance(field.get("name"), str)
                or not field["name"]
                or field["name"] in field_names
            ):
                raise BackendError("Invalid or duplicate class-layer field definition.")
            field_names.add(field["name"])
        seen.add(identifier)
        collections.append(
            Collection(
                collection_id=identifier,
                title=str(raw.get("title") or raw.get("class_name") or raw["class_id"]),
                transactional=True,
                class_layer=dict(raw),
            )
        )
    return sorted(collections, key=lambda item: (item.display_name.casefold(), item.collection_id))


def collection_role(collection: Collection, bulk_capability=None, roles=None) -> str:
    """UI placement only; never a source of write permission or routing authority.

    New servers can advertise roles explicitly. The established legacy IDs are a
    presentation fallback for older servers; unknown collections stay in Advanced.
    """
    if collection.class_layer is not None:
        return "editable"
    advertised = (roles or {}).get(collection.collection_id)
    if advertised in ("editable", "current", "historical", "audit", "extent", "metadata"):
        return advertised
    if bulk_capability is not None and bulk_capability.serves(collection.collection_id):
        return "editable"
    stem = stem_of(collection.collection_id)
    legacy = {
        "label": "editable",
        "label_current": "current",
        "label_asof": "historical",
        "label_history": "audit",
        "labeled_extent": "extent",
        "label_class": "metadata",
        "capture": "metadata",
    }
    return legacy.get(stem, "reference")


def visible_collections(
    collections: Sequence[Collection], bulk_capability=None, roles=None
) -> list[Collection]:
    """The main chooser contains current editing layers, never metadata or history."""
    discovered = [collection for collection in collections if collection.class_layer is not None]
    return discovered or [
        collection
        for collection in collections
        if collection_role(collection, bulk_capability, roles) == "editable"
    ]


def reference_collections(
    collections: Sequence[Collection], bulk_capability=None, roles=None
) -> list[Collection]:
    return [
        collection
        for collection in collections
        if collection_role(collection, bulk_capability, roles) == "reference"
    ]


def current_collections(
    collections: Sequence[Collection], bulk_capability=None, roles=None
) -> list[Collection]:
    return [
        collection
        for collection in collections
        if collection_role(collection, bulk_capability, roles) == "current"
    ]


def readonly_collections(
    collections: Sequence[Collection], bulk_capability=None, roles=None
) -> list[Collection]:
    """Use class columns for both modes; older servers retain their current views."""
    discovered = [collection for collection in collections if collection.class_layer is not None]
    return discovered or current_collections(collections, bulk_capability, roles)
