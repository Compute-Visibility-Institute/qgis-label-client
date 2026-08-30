"""Signed imagery URLs: parsing them, and matching them to raster layers.

THE PROBLEM THIS SOLVES, WHICH IS THE PLUGIN'S CLEAREST REASON TO EXIST

The imagery is Maxar "Limited Rights Data" in a private bucket with uniform bucket-level
access. Nothing about it is public, and nothing about it goes through the API either --
a 1.1 GB analysis COG is streamed straight from GCS by range request. The two constraints
meet at short-lived signed URLs: the backend verifies who you are and mints one, and the
client range-requests the bucket directly.

Signed URLs expire. A ``.qgz`` therefore cannot hold one; saved on Monday it is a broken
layer on Tuesday, and there is no way to express "a URL, but fetch it fresh" in a project
file. So the plugin fetches fresh URLs at session start and rewrites the raster layer
sources in place. QGIS has no mechanism for that, which is precisely why this code exists
while the vector half of the plugin does not.

TWO RULES ENFORCED HERE

1. **A signed URL is a credential.** It grants read access to licensed imagery to anyone
   holding it, for as long as it lives. It is never logged, never written to settings and
   never put in an error message. :func:`redact` exists so that logging a failure cannot
   accidentally leak one.
2. **Matching is explicit first.** A layer says which asset it is via a custom property
   the project file sets. Matching by URL is a fallback for projects saved before that
   property existed, and it compares only the bucket-and-object path -- never the query
   string, which is the signature and differs on every mint.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import BackendError

#: Layer custom property naming which capture/asset a raster layer shows.
#: Set by the project file in the private repo; read here.
ASSET_KEY_PROPERTY = "cvi/asset_key"

#: Layer custom property holding the scene's acquisition instant, ISO 8601.
#:
#: Stamped during the signed-URL refresh because that is the only moment the plugin
#: holds both the layer and the capture metadata at once. Everything downstream reads
#: it off the layer instead of re-querying: a label's default ``valid_from`` is the
#: acquisition time of whatever imagery is on top, and resolving that must not require
#: a network round trip every time the analyst starts a polygon.
CAPTURED_AT_PROPERTY = "cvi/captured_at"

#: GDAL's HTTP range-request virtual filesystem. QGIS streams COGs through it with no
#: tile server in the path at all.
VSICURL_PREFIX = "/vsicurl/"

_GCS_HOSTS = ("storage.googleapis.com", "storage.cloud.google.com")


@dataclass(frozen=True)
class SignedAsset:
    """One signed URL for one imagery derivative of one capture."""

    capture_id: str | None
    stac_id: str
    #: Asset role as ``capture.assets`` names it -- analysis, visual, nir, raw.
    #: Never validated against a fixed list here: the ingest pipeline decides what
    #: derivatives exist, and a new one must not need a plugin release.
    role: str
    url: str
    gs_uri: str | None = None
    expires_at: datetime | None = None
    #: When the sensor recorded this scene -- STAC ``datetime``, from the IMD
    #: ``earliestAcqTime``, and NOT when it was delivered or ingested.
    #:
    #: This is the honest default for a label's ``valid_from``: a polygon traced
    #: over this imagery is a claim about the ground AT THIS INSTANT. See
    #: :mod:`..core.validtime` for what is done with it, and for the defect that
    #: exists when it is missing -- a compound digitised from the April scene
    #: recorded as a claim about an afternoon in August.
    #:
    #: ``None`` for a backend too old to send it. The plugin then has no default
    #: and says so, rather than substituting ``now()``.
    captured_at: datetime | None = None

    @property
    def key(self) -> str | None:
        """Stable identifier for the (capture, derivative) pair.

        ``None`` when the backend did not name the scene. Without ``stac_id`` the key
        would be ``":visual"`` for *every* capture, which is worse than having no key:
        two captures would collide in the lookup below and a layer stamped with it would
        bind to an arbitrary scene on the next refresh -- the wrong imagery under the
        right layer name, which is exactly the kind of silent error a signed-URL refresh
        must not introduce. Fall back to matching by object path instead.
        """
        return f"{self.stac_id}:{self.role}" if self.stac_id else None

    @property
    def object_key(self) -> str | None:
        """``bucket/path`` for this asset, from the gs:// URI or the signed URL."""
        return gcs_object_key(self.gs_uri) or gcs_object_key(self.url)

    def source(self) -> str:
        """The QGIS/GDAL raster source string for this asset."""
        return vsicurl_source(self.url)


def redact(url: str) -> str:
    """Strip the signature from a URL so it can appear in a log line.

    Everything before the query string is kept, because the object path is what a human
    debugging a mismatch needs to see. Everything after it is the credential.
    """
    if not url:
        return ""
    parts = urlsplit(url)
    if not parts.query:
        return url
    return f"{parts.scheme}://{parts.netloc}{parts.path}?<signature redacted>"


def vsicurl_source(url: str) -> str:
    """Wrap a signed HTTPS URL as a GDAL ``/vsicurl/`` source.

    Idempotent, because the fallback matcher reads sources that already carry the prefix
    and re-wrapping would produce ``/vsicurl//vsicurl/...``.
    """
    if url.startswith(VSICURL_PREFIX):
        return url
    return VSICURL_PREFIX + url


def gcs_object_key(reference: str | None) -> str | None:
    """Reduce any reference to one GCS object to ``bucket/object/path``.

    Accepts every form the same object turns up in across a project:

    * ``gs://bucket/a/b.tif`` -- as ``capture.assets`` records it;
    * ``https://storage.googleapis.com/bucket/a/b.tif?X-Goog-Signature=...``;
    * ``https://bucket.storage.googleapis.com/a/b.tif``;
    * either of those behind ``/vsicurl/``, or behind ``/vsicurl?url=<encoded>``.

    Returns ``None`` for anything that is not recognisably a GCS object, so a local file
    or an unrelated WMS layer is never mistaken for imagery and rewritten.
    """
    if not reference:
        return None
    candidate = reference.strip()

    if candidate.startswith("/vsicurl?"):
        # GDAL's option-bearing form: /vsicurl?url=<percent-encoded>&option=...
        query = parse_qs(urlsplit(candidate).query)
        inner = query.get("url", [""])[0]
        return gcs_object_key(unquote(inner)) if inner else None
    if candidate.startswith(VSICURL_PREFIX):
        return gcs_object_key(candidate[len(VSICURL_PREFIX) :])

    parts = urlsplit(candidate)
    if parts.scheme == "gs":
        bucket, path = parts.netloc, parts.path.lstrip("/")
        return f"{bucket}/{unquote(path)}" if bucket and path else None
    if parts.scheme not in ("http", "https"):
        return None

    host = parts.netloc.split(":")[0].lower()
    path = unquote(parts.path.lstrip("/"))
    if not path:
        return None
    if host in _GCS_HOSTS:
        # Path-style: /bucket/object...
        return path if "/" in path else None
    for gcs_host in _GCS_HOSTS:
        if host.endswith("." + gcs_host):
            # Virtual-hosted style: bucket.storage.googleapis.com/object...
            bucket = host[: -len("." + gcs_host)]
            return f"{bucket}/{path}" if bucket else None
    return None


def _parse_expiry(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        # RFC 3339 with a literal Z, which ``fromisoformat`` rejects before 3.11.
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_signed_assets(document: Any) -> tuple[list[SignedAsset], datetime | None]:
    """Parse the backend's signed-URL response.

    Expected shape::

        {"expires_at": "...", "assets": [
            {"capture_id": ..., "stac_id": ..., "asset": "visual",
             "url": "https://storage.googleapis.com/...", "gs_uri": "gs://..."}]}

    A bare array of asset objects is also accepted. Returns the assets and the earliest
    expiry across them, which is what the panel reports and warns on -- the session is
    only good until the first URL dies.
    """
    if isinstance(document, Mapping):
        raw_assets = document.get("assets")
        overall_expiry = _parse_expiry(document.get("expires_at"))
    elif isinstance(document, list):
        raw_assets, overall_expiry = document, None
    else:
        raise BackendError("Signed-URL response is not a JSON object or array.")

    if not isinstance(raw_assets, Sequence):
        raise BackendError("Signed-URL response has no 'assets' array.")

    assets: list[SignedAsset] = []
    for raw in raw_assets:
        if not isinstance(raw, Mapping):
            continue
        # Two vocabularies, both accepted. The backend's capture rows are STAC-shaped,
        # where an asset is a map key plus an ``href``; this client reads a flat list
        # where each entry names its own ``asset`` and ``url``. Reading only one
        # spelling does not degrade gracefully -- an entry missing it is skipped, and
        # skipping them all raises "no usable assets", which names neither field and
        # sends whoever reads it looking at the network rather than at the payload.
        url = raw.get("url") or raw.get("href")
        stac_id = raw.get("stac_id") or raw.get("scene_id")
        role = raw.get("asset") or raw.get("role") or raw.get("name") or raw.get("key")
        if not isinstance(url, str) or not url or not isinstance(role, str) or not role:
            continue
        expiry = _parse_expiry(raw.get("expires_at")) or overall_expiry
        assets.append(
            SignedAsset(
                capture_id=str(raw["capture_id"]) if raw.get("capture_id") else None,
                stac_id=str(stac_id) if stac_id else "",
                role=role,
                url=url,
                gs_uri=str(raw["gs_uri"]) if raw.get("gs_uri") else None,
                expires_at=expiry,
                # Same RFC 3339 parse as the expiry: one instant format on this
                # wire, and a value that fails to parse becomes None rather than
                # a wrong date.
                captured_at=_parse_expiry(raw.get("captured_at")),
            )
        )
    if not assets:
        raise BackendError("Signed-URL response contained no usable assets.")

    expiries = [asset.expires_at for asset in assets if asset.expires_at]
    earliest = min(expiries) if expiries else overall_expiry
    return assets, earliest


@dataclass(frozen=True)
class RasterLayerRef:
    """The subset of a ``QgsRasterLayer`` this module needs, so it stays testable."""

    layer_id: str
    name: str
    source: str
    asset_key: str | None = None


@dataclass(frozen=True)
class Rewrite:
    """An instruction to repoint one raster layer at a freshly signed URL."""

    layer_id: str
    layer_name: str
    new_source: str
    asset: SignedAsset
    #: "asset_key" (the layer declared what it is) or "object_key" (inferred from the
    #: stale URL). Reported so a project missing its custom properties is visible rather
    #: than merely working.
    matched_by: str


def plan_rewrites(
    layers: Iterable[RasterLayerRef],
    assets: Iterable[SignedAsset],
) -> tuple[list[Rewrite], list[RasterLayerRef]]:
    """Decide which layers get which signed URL.

    Returns ``(rewrites, unmatched)``. A layer that matches nothing is returned rather
    than skipped silently: "the imagery did not refresh" must be a visible outcome, not
    a layer that renders yesterday's cached tiles and looks fine until it doesn't.
    """
    asset_list = list(assets)
    by_key = {asset.key: asset for asset in asset_list if asset.key}
    by_object: dict[str, SignedAsset] = {}
    for asset in asset_list:
        object_key = asset.object_key
        if object_key:
            by_object.setdefault(object_key, asset)

    rewrites: list[Rewrite] = []
    unmatched: list[RasterLayerRef] = []
    for layer in layers:
        asset = by_key.get(layer.asset_key) if layer.asset_key else None
        matched_by = "asset_key"
        if asset is None:
            object_key = gcs_object_key(layer.source)
            asset = by_object.get(object_key) if object_key else None
            matched_by = "object_key"
        if asset is None:
            unmatched.append(layer)
            continue
        rewrites.append(
            Rewrite(
                layer_id=layer.layer_id,
                layer_name=layer.name,
                new_source=asset.source(),
                asset=asset,
                matched_by=matched_by,
            )
        )
    return rewrites, unmatched
