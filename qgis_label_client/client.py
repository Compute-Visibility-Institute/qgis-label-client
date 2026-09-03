"""The backend calls, as plain functions meant to run inside a ``QgsTask``.

Everything in here blocks. Nothing in here touches Qt widgets, ``iface`` or
``QgsProject``. That is the contract for code reachable from ``QgsTask.run()``, and
keeping it in one module makes the boundary easy to check.

Only two of these are not standard OGC API - Features:

* ``/collections`` and ``/collections/{id}/items`` -- Part 1 for reads and Part 4 for
  creates, both also spoken by QGIS's native provider;
* the class registry, which has no OGC equivalent because "here is the JSON Schema for
  each class" is not a features question;
* the history-track list, likewise: "which isolated datasets does this deployment hold?"
  is not one either;
* signed imagery URLs, likewise;
* the capability document, and the atomic bulk create it advertises. OGC API - Features
  Part 4 creates one resource per request and says nothing about creating many in one
  transaction, which is precisely the property that makes a batch safe here.

The custom endpoints are configurable paths rather than constants, so a deployment can
mount them wherever it likes -- the bulk path being the one the deployment states in its
own capability document rather than a setting, because a client that has to be configured
to match the server it just interrogated has asked and then ignored the answer.

EVERY CALL TAKES A TRACK, AND SENDS IT

``track`` is threaded through to :func:`~.network.request_json` and
:func:`~.network.post_json`, which set ``X-Track``. Empty means "name no track", which the
edge answers from the deployment default -- correct for a read, and refused for a write,
which is the edge's decision rather than this module's. The two calls that are shared
between tracks by design (the class registry, signed imagery URLs) take it anyway, so that
the audit line on the edge can say who asked for what while working on which dataset.

The creates at the bottom exist for the bootstrap publish only. Ordinary editing goes
through the native provider's Part 4 support, where QGIS already owns the edit buffer,
the undo stack and the conflict handling; duplicating that here would be a second, worse
editor. What the provider cannot do is take a *local* layer -- a shapefile that has never
been part of the collection -- and turn it into features, which is the one case below.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from qgis.core import QgsFeedback

from .core import bulk, urls
from .core.assets import SignedAsset, parse_signed_assets
from .core.collections import Collection, parse_collections
from .core.history import HistoryEntry, parse_history
from .core.registry import ClassRegistry, parse_registry
from .core.tracks import Track, parse_tracks
from .network import post_json, request_json


def fetch_collections(
    base_url: str, authcfg: str, feedback: QgsFeedback | None = None, track: str = ""
) -> list[Collection]:
    """List the collections the backend serves."""
    url = urls.collections_url(base_url)
    return parse_collections(request_json(url, authcfg=authcfg, feedback=feedback, track=track))


def fetch_tracks(
    base_url: str,
    tracks_path: str,
    authcfg: str,
    feedback: QgsFeedback | None = None,
) -> list[Track]:
    """List the history tracks this deployment holds.

    Deliberately sends no ``X-Track``: this is the call that asks *which tracks exist*, and
    scoping it to one would be circular. The edge answers it from the principal's own
    permissions.
    """
    url = urls.tracks_url(base_url, tracks_path)
    return parse_tracks(request_json(url, authcfg=authcfg, feedback=feedback))


def fetch_registry(
    base_url: str,
    registry_path: str,
    authcfg: str,
    feedback: QgsFeedback | None = None,
    track: str = "",
) -> ClassRegistry:
    """Fetch and parse the class registry.

    The registry is SHARED between tracks and must stay that way: the same vocabulary
    describes both datasets, and two registries would drift -- which is the exact defect
    this platform exists to fix. The track is sent for attribution, not for scoping.
    """
    url = urls.join_path(base_url, registry_path)
    return parse_registry(
        request_json(url, authcfg=authcfg, feedback=feedback, track=track), source_url=url
    )


def fetch_signed_assets(
    base_url: str,
    signed_urls_path: str,
    authcfg: str,
    feedback: QgsFeedback | None = None,
    capture_ids: list[str] | None = None,
    track: str = "",
) -> tuple[list[SignedAsset], Any]:
    """Mint fresh signed imagery URLs.

    Returns ``(assets, earliest_expiry)``. Optionally scoped to specific captures, so a
    project with one campus open does not mint URLs for the whole archive.
    """
    url = urls.join_path(base_url, signed_urls_path)
    if capture_ids:
        url = urls.with_query(url, {"capture_id": ",".join(capture_ids)})
    # Imagery is shared between tracks -- the same 1.1 GB GeoTIFF serves both, because a
    # capture is a fact about the world and a track is a body of belief about it. Sent for
    # the edge's audit line only.
    return parse_signed_assets(request_json(url, authcfg=authcfg, feedback=feedback, track=track))


def fetch_history(
    base_url: str,
    history_collection: str,
    label_id: str,
    authcfg: str,
    fields_label_id: str,
    limit: int = 200,
    feedback: QgsFeedback | None = None,
    track: str = "",
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
    return parse_history(request_json(url, authcfg=authcfg, feedback=feedback, track=track))


def fetch_features(
    base_url: str,
    collection_id: str,
    authcfg: str,
    query: Mapping[str, Any] | None = None,
    feedback: QgsFeedback | None = None,
    track: str = "",
) -> Any:
    """Fetch a raw FeatureCollection. Used by the coverage check for survey extents."""
    url = urls.with_query(urls.items_url(base_url, collection_id), query or {})
    return request_json(url, authcfg=authcfg, feedback=feedback, track=track)


def create_feature(
    base_url: str,
    collection_id: str,
    feature: Mapping[str, Any],
    authcfg: str,
    feedback: QgsFeedback | None = None,
    track: str = "",
) -> Any:
    """Create one feature. OGC API - Features Part 4: POST a Feature to ``/items``.

    No feature id is sent. ``label.label_id`` is ``uuid DEFAULT gen_random_uuid()`` and
    the surrogate OAPIF id is ``GENERATED ALWAYS AS IDENTITY``; both are the server's to
    assign, and the source data's own ``id`` column is 0% populated across all 1,246
    features -- it is the defect being fixed, not a fallback.

    No ``track_id`` is sent either, for the same reason and with the same force: the
    column's server-side default is ``app.writable_track_id()``, resolved from the
    ``X-Track`` header this call sends. A client-supplied track_id would be a client
    asserting which dataset a row belongs to, which is precisely what row-level security
    exists to take away from it -- and the write policy would refuse it anyway.
    """
    return post_json(
        urls.items_url(base_url, collection_id),
        dict(feature),
        authcfg=authcfg,
        feedback=feedback,
        track=track,
    )


def create_features(
    base_url: str,
    collection_id: str,
    features: Sequence[Mapping[str, Any]],
    authcfg: str,
    feedback: QgsFeedback | None = None,
    track: str = "",
    reason: str = "",
    path: str = urls.BULK_PATH,
) -> Any:
    """Create many features in ONE database transaction. Not OGC API - Features.

    THIS IS NOT THE BATCH THAT WAS REMOVED, AND THE DIFFERENCE IS THE TRANSACTION

    A FeatureCollection posted to ``/items`` was here once and was taken out: a save is
    not atomic, so the first refusal aborted the rest *after* earlier rows had committed;
    there is no ETag and no If-Match and identity is the server's, so nothing here could
    ask whether an ambiguous failure had already been applied; and the Part 4 create
    handler takes a single Feature. A partly-applied batch got re-sent whole, and the
    founding dataset gained duplicates nothing could tell apart.

    This endpoint is a different thing wearing a similar shape. Every feature is inserted
    inside one transaction, so it lands whole or not at all; the response states how many
    rows it created and every refusal states ``created: 0``; and it does not reach the
    single-Feature handler at all. Those three are the answers to the three reasons, one
    for one, and they are the only reason this function exists.

    Still no identity is sent, for the reasons in :func:`create_feature`. What comes back
    is the answer to "did that land?" -- the question the old batch path could not ask --
    and :mod:`.core.bulk` reads it. `reason` must be unique to this chunk; it is what
    makes an ambiguous *response* recoverable with one read.
    """
    return post_json(
        urls.bulk_url(base_url, path, collection_id),
        bulk.feature_collection(features),
        authcfg=authcfg,
        feedback=feedback,
        track=track,
        reason=reason,
    )


def fetch_capabilities(
    base_url: str,
    capabilities_path: str,
    authcfg: str,
    feedback: QgsFeedback | None = None,
    track: str = "",
) -> Any:
    """Ask what this deployment can do, before deciding how to publish into it.

    Returns the raw document; :func:`~.core.bulk.parse_capabilities` decides what it
    means. A backend that predates the endpoint answers 404, which is not an error and
    not a warning -- one feature per request is correct, only slower, and telling an
    analyst mid-publish that their backend is out of date is noise about something they
    cannot fix.
    """
    return request_json(
        urls.capabilities_url(base_url, capabilities_path),
        authcfg=authcfg,
        feedback=feedback,
        track=track,
    )
