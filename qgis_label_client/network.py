"""HTTP through the QGIS network stack.

WHY NOT ``requests``

``QgsBlockingNetworkRequest`` goes through ``QgsNetworkAccessManager``, which means it
inherits the user's proxy configuration, their SSL exceptions, and -- the reason that
matters most here -- the authentication database. Setting ``authcfg`` on the request is
what applies the stored bearer token without this module ever seeing it. ``requests``
would need the token in plaintext, a proxy setting duplicated from QGIS, and a CA bundle
that does not match the one QGIS trusts.

It blocks, so **every function here must be called from inside** ``QgsTask.run()``.
Calling one on the main thread freezes the QGIS window for the duration of the request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from qgis.core import QgsBlockingNetworkRequest, QgsFeedback
from qgis.PyQt.QtCore import QByteArray, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from .core.errors import BackendError

#: Sent on every request so the auth edge can attribute traffic, and so a support
#: conversation can start from a server log line.
USER_AGENT = "qgis-label-client"

#: Media type of an OGC API - Features Part 4 create request body.
GEOJSON_MEDIA_TYPE = "application/geo+json"


@dataclass(frozen=True)
class Response:
    """A completed HTTP response."""

    status: int
    body: bytes
    content_type: str

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            preview = self.body[:200].decode("utf-8", errors="replace")
            raise BackendError(
                f"Expected JSON but could not parse the response ({exc}). "
                f"Content-Type was {self.content_type or 'unset'}; body began: {preview!r}"
            ) from exc


def _describe_status(status: int, url: str, body: bytes) -> str:
    """Turn an HTTP status into something an annotator can act on."""
    hints = {
        401: "The API rejected the credential. Sign in again from the panel; the token "
        "may have expired or been rotated.",
        403: "The credential is valid but not authorised for this resource.",
        404: "Not found. Check that the backend URL is the API landing page, not a "
        "collection or a UI page.",
        429: "Rate limited by the auth edge. Wait a moment and retry.",
    }
    hint = hints.get(status)
    if hint is None and 500 <= status < 600:
        hint = (
            "The backend returned a server error. It may still be starting up: a "
            "scale-to-zero deployment is slow to answer the first request after an "
            "idle period."
        )
    detail = body[:300].decode("utf-8", errors="replace").strip()
    parts = [f"HTTP {status} from {url}"]
    if hint:
        parts.append(hint)
    if detail:
        parts.append(f"Server said: {detail}")
    return " ".join(parts)


def _prepare(url: str, accept: str, authcfg: str) -> tuple[QNetworkRequest, Any]:
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"Accept", accept.encode("ascii"))
    request.setRawHeader(b"User-Agent", USER_AGENT.encode("ascii"))

    fetcher = QgsBlockingNetworkRequest()
    if authcfg:
        fetcher.setAuthCfg(authcfg)
    return request, fetcher


def _read(fetcher: Any, error: Any, url: str) -> Response:
    """Turn a completed blocking request into a :class:`Response`, or raise.

    The status is checked before the error code on purpose: a 4xx sets both, and the
    server's own message is far more useful than "protocol error".
    """
    reply = fetcher.reply()
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    status = int(status) if status is not None else 0
    body = bytes(reply.content())

    if status and not (200 <= status < 300):
        raise BackendError(_describe_status(status, url, body))

    if error != QgsBlockingNetworkRequest.ErrorCode.NoError:
        message = fetcher.errorMessage() or reply.errorString() or "unknown network error"
        raise BackendError(f"Request to {url} failed: {message}")

    content_type = bytes(reply.rawHeader(b"Content-Type")).decode("ascii", errors="replace")
    return Response(status=status or 200, body=body, content_type=content_type)


def request_json(
    url: str,
    *,
    authcfg: str = "",
    feedback: QgsFeedback | None = None,
    accept: str = "application/json",
) -> Any:
    """GET `url` and parse the response as JSON. Call only from a worker thread.

    `feedback` is threaded through so a cancelled task actually aborts the socket rather
    than finishing the download and discarding it.
    """
    request, fetcher = _prepare(url, accept, authcfg)
    # forceRefresh=True: signed URLs and as-of views must never come from the HTTP cache.
    # A cached class registry is merely stale; a cached signed URL is expired.
    error = fetcher.get(request, True, feedback)
    return _read(fetcher, error, url).json()


def post_json(
    url: str,
    payload: Any,
    *,
    authcfg: str = "",
    feedback: QgsFeedback | None = None,
    content_type: str = GEOJSON_MEDIA_TYPE,
    accept: str = "application/json",
) -> Any:
    """POST `payload` as JSON and parse whatever comes back. Worker thread only.

    Through ``QgsBlockingNetworkRequest`` for the same reasons GET is: the proxy
    configuration, the SSL exceptions and -- the one that matters -- the authentication
    database, which is the only place the API token exists.

    A create returns ``201`` with a ``Location`` header and frequently an empty body, so an
    empty response is ``None`` rather than a parse error. The body is not needed: the
    server assigns ``label_id`` and this client deliberately never learns it, because
    holding a client-side copy of an identity it did not issue is the defect this whole
    schema exists to remove.
    """
    request, fetcher = _prepare(url, accept, authcfg)
    request.setRawHeader(b"Content-Type", content_type.encode("ascii"))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    error = fetcher.post(request, QByteArray(body), False, feedback)
    response = _read(fetcher, error, url)
    return response.json() if response.body.strip() else None
