"""Building the QGIS data-source URI for an OGC API - Features layer.

WHY THE PLUGIN BUILDS THIS AT ALL

QGIS's native OAPIF provider does the reading *and* the writing (Part 4), so the plugin
contributes no data-access code. What it does contribute is the URI: the collection to
open, the ``authcfg`` id so the provider authenticates with a token it never sees in
plaintext, and the as-of filter. Getting that string wrong is the difference between a
layer and a silent empty layer, which is why it is a pure function with tests rather
than an f-string in a slot.

Note ``authcfg``: the URI carries a seven-character *reference* into ``qgis-auth.db``,
never the token. That is what makes a ``.qgz`` safe to email -- the project file names a
credential, it does not contain one.
"""

from __future__ import annotations

from collections.abc import Mapping

_QUOTE = "'"


def encode_uri_value(value: str) -> str:
    """Quote a value for ``QgsDataSourceUri``.

    QGIS parses ``key='value'`` with backslash escaping inside the quotes. Values are
    always quoted rather than quoted-when-necessary: an OAPIF ``url`` routinely contains
    ``=`` and ``&`` from its query string, and an unquoted ``&`` terminates the value
    and turns the rest of the URL into garbage parameters.
    """
    escaped = value.replace("\\", "\\\\").replace(_QUOTE, "\\" + _QUOTE)
    return f"{_QUOTE}{escaped}{_QUOTE}"


def encode_datasource_uri(params: Mapping[str, object]) -> str:
    """Render an ordered mapping as a ``key='value'`` data-source URI.

    ``None`` values are omitted, so callers can pass optional parameters unconditionally.
    Booleans become ``1``/``0``, which is what the WFS/OAPIF provider parses.
    """
    parts: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        rendered = ("1" if value else "0") if isinstance(value, bool) else str(value)
        parts.append(f"{key}={encode_uri_value(rendered)}")
    return " ".join(parts)


#: URI parameter prefix the WFS/OAPIF provider reads extra request headers from. One key
#: per header: ``http-header:X-Track='production'``.
HTTP_HEADER_PREFIX = "http-header:"


def header_params(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Render a header mapping as ``http-header:`` URI parameters.

    WHY A HEADER GOES IN THE URI AT ALL

    Because the native OAPIF provider makes the requests, not this plugin -- including the
    Part 4 writes -- so anything that has to ride on every one of them must be somewhere
    the *provider* will look. There are exactly two such places: the ``authcfg``
    credential, and these.

    That matters for history tracks specifically. The track has to reach the auth edge on
    every read and every write, and putting it here means it is part of the layer's own
    data source: a layer cannot be pointed at the wrong track by a stale setting, and a
    saved ``.qgz`` reopens on the track it was saved on rather than on whatever the person
    who opens it happens to have selected.

    Empty values are dropped rather than sent blank -- a blank ``X-Track`` is not "no
    track", it is a header the edge has to decide what to do with.
    """
    return {
        f"{HTTP_HEADER_PREFIX}{name}": value
        for name, value in (headers or {}).items()
        if name and value
    }


def build_oapif_uri(
    *,
    landing_url: str,
    collection_id: str,
    authcfg: str | None = None,
    page_size: int | None = None,
    restrict_to_request_bbox: bool = True,
    cql_filter: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> str:
    """Build the data-source URI for one OGC API - Features collection.

    Parameters map onto the WFS/OAPIF provider's URI vocabulary:

    ``url``
        the API landing page. The provider follows its links to reach
        ``/collections/{id}/items``; it is not the items URL itself.
    ``typename``
        the collection id.
    ``restrictToRequestBBOX``
        request only features overlapping the canvas. On at national extent -- the
        ``compound`` class spans 3428 x 2652 km and fetching all of it to draw one
        campus is a minute of waiting for nothing.
    ``filter``
        a **QGIS expression**, which the provider compiles to CQL2 itself and sends as
        ``filter=...&filter-lang=cql2-text``. Not literal CQL2: an expression QGIS
        cannot parse makes the layer invalid rather than unfiltered. See
        :mod:`.asof` for the specific trap.
    ``authcfg``
        reference into ``qgis-auth.db``. Never a token.
    ``http-header:*``
        extra request headers, one parameter each -- see :func:`header_params`. This is
        how the history track reaches the auth edge on the provider's own requests.
    """
    if not collection_id:
        raise ValueError("collection_id is required")
    return encode_datasource_uri(
        {
            "url": landing_url,
            "typename": collection_id,
            "restrictToRequestBBOX": restrict_to_request_bbox,
            "pageSize": page_size if page_size and page_size > 0 else None,
            "filter": cql_filter or None,
            # Before authcfg, so the credential stays the last thing on the line and is
            # easy to find by eye in the layer properties dialog.
            **header_params(headers),
            "authcfg": authcfg or None,
        }
    )
