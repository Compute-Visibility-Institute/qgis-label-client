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
