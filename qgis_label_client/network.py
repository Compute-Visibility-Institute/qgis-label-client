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
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from .core.errors import BackendError

#: Sent on every request so the auth edge can attribute traffic, and so a support
#: conversation can start from a server log line.
USER_AGENT = "qgis-label-client"


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
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"Accept", accept.encode("ascii"))
    request.setRawHeader(b"User-Agent", USER_AGENT.encode("ascii"))

    fetcher = QgsBlockingNetworkRequest()
    if authcfg:
        fetcher.setAuthCfg(authcfg)

    # forceRefresh=True: signed URLs and as-of views must never come from the HTTP cache.
    # A cached class registry is merely stale; a cached signed URL is expired.
    error = fetcher.get(request, True, feedback)

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
    return Response(status=status or 200, body=body, content_type=content_type).json()
