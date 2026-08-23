"""The backend calls, as plain functions meant to run inside a ``QgsTask``.

Everything in here blocks. Nothing in here touches Qt widgets, ``iface`` or
``QgsProject``. That is the contract for code reachable from ``QgsTask.run()``, and
keeping it in one module makes the boundary easy to check.

Four calls, and only one of them is not standard OGC API - Features:

* ``/collections`` and ``/collections/{id}/items`` -- Part 1, also what QGIS's native
  provider speaks;
* the class registry, which has no OGC equivalent because "here is the JSON Schema for
  each class" is not a features question;
* signed imagery URLs, likewise.

The two custom endpoints are configurable paths rather than constants, so a deployment
can mount them wherever it likes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from qgis.core import QgsFeedback

from .core import urls
from .core.assets import SignedAsset, parse_signed_assets
from .core.collections import Collection, parse_collections
from .core.history import HistoryEntry, parse_history
from .core.registry import ClassRegistry, parse_registry
from .network import request_json


def fetch_collections(
    base_url: str, authcfg: str, feedback: QgsFeedback | None = None
) -> list[Collection]:
    """List the collections the backend serves."""
    url = urls.collections_url(base_url)
    return parse_collections(request_json(url, authcfg=authcfg, feedback=feedback))


def fetch_registry(
    base_url: str,
    registry_path: str,
    authcfg: str,
    feedback: QgsFeedback | None = None,
) -> ClassRegistry:
    """Fetch and parse the class registry."""
    url = urls.join_path(base_url, registry_path)
    return parse_registry(request_json(url, authcfg=authcfg, feedback=feedback), source_url=url)


def fetch_signed_assets(
    base_url: str,
    signed_urls_path: str,
    authcfg: str,
    feedback: QgsFeedback | None = None,
    capture_ids: list[str] | None = None,
) -> tuple[list[SignedAsset], Any]:
    """Mint fresh signed imagery URLs.

    Returns ``(assets, earliest_expiry)``. Optionally scoped to specific captures, so a
    project with one campus open does not mint URLs for the whole archive.
    """
    url = urls.join_path(base_url, signed_urls_path)
    if capture_ids:
        url = urls.with_query(url, {"capture_id": ",".join(capture_ids)})
    return parse_signed_assets(request_json(url, authcfg=authcfg, feedback=feedback))


def fetch_history(
    base_url: str,
    history_collection: str,
    label_id: str,
    authcfg: str,
    fields_label_id: str,
    limit: int = 200,
    feedback: QgsFeedback | None = None,
) -> list[HistoryEntry]:
    """Fetch the audit trail for one label.

    Keyed on ``label_id``, the immutable server-assigned UUID -- never on the OAPIF
    feature id, which is a surrogate that a new valid-time state deliberately replaces.
    Querying by the surrogate would return the history of one *state*, which is the
    question nobody asks.
    """
    url = urls.with_query(
        urls.items_url(base_url, history_collection),
        {fields_label_id: label_id, "limit": limit},
    )
    return parse_history(request_json(url, authcfg=authcfg, feedback=feedback))


def fetch_features(
    base_url: str,
    collection_id: str,
    authcfg: str,
    query: Mapping[str, Any] | None = None,
    feedback: QgsFeedback | None = None,
) -> Any:
    """Fetch a raw FeatureCollection. Used by the coverage check for survey extents."""
    url = urls.with_query(urls.items_url(base_url, collection_id), query or {})
    return request_json(url, authcfg=authcfg, feedback=feedback)
