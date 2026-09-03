"""URL assembly for the backend, kept away from Qt so it can be tested.

The backend base URL is a *user setting with a placeholder default*: this repository is
public and must contain no deployment hostnames. Everything here therefore takes the
base URL as an argument and never reaches for a constant.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .errors import ConfigurationError

_ALLOWED_SCHEMES = ("https", "http")

#: Where a collection id goes in the bulk endpoint's advertised path template.
COLLECTION_PLACEHOLDER = "{collectionId}"

#: Path of the atomic bulk create, relative to the backend base URL.
#:
#: Under the backend's own ``v1/`` namespace, and that is load-bearing rather than tidy:
#: everything outside the prefix is proxied verbatim to the feature service, while an
#: unserved path *inside* it is answered with a plain 404 by the edge itself. That 404 is
#: what lets a backend predating this endpoint be recognised as one -- exactly as
#: ``v1/tracks`` already is -- instead of producing an OAPIF error about an unknown path,
#: which a client can misread as "the endpoint exists and failed".
#:
#: A constant rather than a setting because it is only the fallback: the deployment
#: advertises its own path in ``v1/capabilities``, and that is what is used when it can be
#: substituted into.
BULK_PATH = f"v1/collections/{COLLECTION_PLACEHOLDER}/bulk"


def normalise_base_url(base_url: str) -> str:
    """Validate and canonicalise the API base URL.

    Trailing slashes are stripped so that :func:`join_path` produces one and only one
    separator, and a bare host is rejected rather than silently turned into a relative
    URL later.
    """
    candidate = (base_url or "").strip()
    if not candidate:
        raise ConfigurationError("No backend URL configured. Set it in the CVI Label Client panel.")
    parts = urlsplit(candidate)
    if parts.scheme not in _ALLOWED_SCHEMES or not parts.netloc:
        raise ConfigurationError(f"Backend URL must be an absolute http(s) URL, got {candidate!r}.")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def join_segments(base_url: str, *segments: str) -> str:
    """Append already-separated path segments, percent-encoding each one.

    Encoding per segment rather than per path is what keeps a collection id containing a
    slash from silently becoming two path segments and 404ing.
    """
    base = normalise_base_url(base_url)
    encoded = [quote(segment, safe="") for segment in segments if segment]
    if not encoded:
        return base
    return base + "/" + "/".join(encoded)


def join_path(base_url: str, path: str) -> str:
    """Append a slash-separated `path` to an already-normalised base URL.

    Deliberately not :func:`urllib.parse.urljoin`, whose behaviour with a leading slash
    is to discard the base path -- that would quietly drop a deployment's ``/oapif``
    prefix and produce 404s that look like a backend outage.

    Use this for configured paths, where the slashes are separators. Use
    :func:`join_segments` when a segment is a value that may itself contain a slash.
    """
    return join_segments(base_url, *(part for part in path.strip("/").split("/") if part))


def with_query(url: str, params: Mapping[str, object]) -> str:
    """Return `url` with `params` merged into its query string.

    Existing parameters are preserved; ``None`` values are dropped so callers can pass
    optional filters without branching. Booleans are rendered as ``true``/``false``
    because that is what OGC API - Features expects, not Python's ``True``.
    """
    parts = urlsplit(url)
    query: list[tuple[str, str]] = parse_qsl(parts.query, keep_blank_values=True)
    for key, value in params.items():
        if value is None:
            continue
        rendered = ("true" if value else "false") if isinstance(value, bool) else str(value)
        query.append((key, rendered))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def collections_url(base_url: str) -> str:
    """OGC API - Features collection list."""
    return join_segments(base_url, "collections")


def collection_url(base_url: str, collection_id: str) -> str:
    """Metadata document for one collection."""
    return join_segments(base_url, "collections", collection_id)


def items_url(base_url: str, collection_id: str) -> str:
    """Items endpoint for one collection."""
    return join_segments(base_url, "collections", collection_id, "items")


def bulk_url(base_url: str, path: str, collection_id: str) -> str:
    """The atomic bulk create for one collection.

    The collection id is substituted as a whole *segment* rather than by string
    replacement, so it is percent-encoded exactly like every other value this module puts
    in a path -- an id containing a slash otherwise silently becomes two segments, and the
    404 that follows points at a collection nobody named.
    """
    segments = [part for part in (path or BULK_PATH).strip("/").split("/") if part]
    return join_segments(
        base_url,
        *(collection_id if part == COLLECTION_PLACEHOLDER else part for part in segments),
    )


def capabilities_url(base_url: str, capabilities_path: str) -> str:
    """The service's own capability document.

    Same argument as :func:`tracks_url` for the path being a setting: "what can this
    deployment do?" is not a features question, so it is not an OGC endpoint.
    """
    return join_path(base_url, capabilities_path)


def tracks_url(base_url: str, tracks_path: str) -> str:
    """The backend's history-track list.

    Not an OGC endpoint -- "which isolated datasets does this deployment hold?" is not a
    features question -- so the path is a setting, exactly like the class registry's, and
    lives under the backend's own ``v1/`` namespace. Everything outside that prefix is
    proxied verbatim to the feature service, so a path without it comes back as an OAPIF
    error about an unknown collection and points at the wrong component entirely.
    """
    return join_path(base_url, tracks_path)
