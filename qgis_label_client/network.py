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
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from qgis.core import QgsBlockingNetworkRequest, QgsFeedback
from qgis.PyQt.QtCore import QByteArray, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from .core import oauth
from .core.errors import BackendError
from .core.tracks import TRACK_HEADER

#: Sent on every request so the auth edge can attribute traffic, and so a support
#: conversation can start from a server log line.
USER_AGENT = "qgis-label-client"

#: Media type of an OGC API - Features Part 4 create request body.
GEOJSON_MEDIA_TYPE = "application/geo+json"

#: The only body Google's OAuth endpoints accept.
FORM_MEDIA_TYPE = "application/x-www-form-urlencoded"

#: Header carrying why a write is being made. The edge sanitises it and binds it as
#: ``app.reason``, which the audit trigger writes onto every history row the request
#: produces.
#:
#: Set here and nowhere else, because only this plugin's own requests carry one -- QGIS's
#: native provider makes its edits without going through this module. The *string* is
#: built in :mod:`.core.bulk`, where the property that matters lives: an ambiguous batch
#: is resolved by asking the history collection for that exact reason, which only works
#: while no two chunks share one.
EDIT_REASON_HEADER = "X-Edit-Reason"

# TRACK_HEADER is imported rather than defined: it is set here on the plugin's own
# requests, and QGIS's OAPIF provider makes its own -- which this module never sees --
# carrying the same header from the layer URI and the credential instead (see
# :mod:`.layers`). Both routes end at the same header on the same edge, so there is one
# definition, in :mod:`.core.tracks`.


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


def _body_detail(body: bytes) -> str:
    """The part of a response body worth putting in front of a person.

    An HTML error page is not a message, it is a wall. The API's own errors are JSON and
    say something useful; a framework's default 500 page is markup wrapped around a status
    line that the status code already gave us. Rendering 300 characters of ``<!doctype
    html>`` where the useful line should be is how a report stops being read, so an HTML
    body is reduced to its ``<title>`` and labelled as what it is.
    """
    text = body[:2000].decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    lowered = text[:200].lower()
    if lowered.startswith("<!doctype html") or lowered.startswith("<html"):
        match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        title = " ".join(match.group(1).split()) if match else ""
        return (
            f"the server returned an HTML error page ({title}) rather than a JSON error, "
            "so it carries no detail about which rule refused the write"
            if title
            else "the server returned an HTML error page rather than a JSON error, so it "
            "carries no detail about which rule refused the write"
        )
    return text[:300]


def _json_object(body: bytes) -> dict | None:
    """The response body as a JSON object, or ``None``. Never raises."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _describe_status(status: int, url: str, body: bytes) -> str:
    """Turn an HTTP status into something an annotator can act on."""
    if status == 403:
        # The auth edge distinguishes "your credential is bad" (401) from "you
        # authenticated perfectly and are not on the access list" (403), and it writes the
        # second message for a person to read: which address it saw, and that signing in
        # again will not help. That sentence is the difference between an analyst emailing
        # an administrator and an analyst filing a bug, so it is passed through verbatim
        # rather than replaced with the word "Forbidden".
        payload = _json_object(body) or {}
        detail = str(payload.get("detail") or "").strip()
        if detail:
            return f"HTTP 403 from {url}. {detail}"

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
        # Deliberately two causes rather than one. The database enforces the class
        # registry's JSON Schema, the geometry type and ST_IsValid in a trigger, and the
        # feature service does not catch that exception -- so a write the schema refuses
        # arrives here as a generic 500 with no JSON body. Naming only the cold start
        # sends the reader to the deployment logs for a problem that is in their data.
        hint = (
            "The backend returned a server error. On a write this is most often the "
            "database refusing the row -- the class's schema, the geometry type or "
            "geometry validity -- which the feature service reports as a bare 500. It "
            "can also be a cold start: a scale-to-zero deployment is slow to answer the "
            "first request after an idle period."
        )
    detail = _body_detail(body)
    parts = [f"HTTP {status} from {url}"]
    if hint:
        parts.append(hint)
    if detail:
        parts.append(f"Server said: {detail}")
    return " ".join(parts)


def _retry_after(reply: Any) -> float | None:
    """Seconds the server asked us to wait, if it asked.

    The auth edge sends ``Retry-After`` with every 429 and it is the only number anywhere
    that knows how long the token bucket needs. Guessing instead is how a client either
    hammers a throttled server or sleeps far longer than it had to.
    """
    try:
        raw = bytes(reply.rawHeader(b"Retry-After")).decode("ascii", errors="replace").strip()
    except (AttributeError, TypeError, UnicodeDecodeError):
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        # The HTTP-date form is legal and neither the edge nor pygeoapi sends it.
        return None
    return seconds if seconds >= 0 else None


def _prepare(
    url: str,
    accept: str,
    authcfg: str,
    track: str = "",
    reason: str = "",
) -> tuple[QNetworkRequest, Any]:
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"Accept", accept.encode("ascii"))
    request.setRawHeader(b"User-Agent", USER_AGENT.encode("ascii"))
    if reason:
        request.setRawHeader(EDIT_REASON_HEADER.encode("ascii"), reason.encode("utf-8"))
    if track:
        # Empty means "name no track", which the edge answers from the deployment
        # default. A blank header would not: it is a header the edge has to decide what
        # to do with, and "decide" is where a wrong answer comes from.
        request.setRawHeader(TRACK_HEADER.encode("ascii"), track.encode("utf-8"))

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
        raise BackendError(
            _describe_status(status, url, body),
            status=status,
            retry_after=_retry_after(reply),
            # The same body twice, deliberately: once as prose for the report and once as
            # the object it was. The bulk publish decides what to re-send from `at` and
            # `created`, and recovering those from the truncated sentence would mean
            # parsing English.
            payload=_json_object(body),
        )

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
    track: str = "",
) -> Any:
    """GET `url` and parse the response as JSON. Call only from a worker thread.

    `feedback` is threaded through so a cancelled task actually aborts the socket rather
    than finishing the download and discarding it.
    """
    request, fetcher = _prepare(url, accept, authcfg, track)
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
    track: str = "",
    reason: str = "",
) -> Any:
    """POST `payload` as JSON and parse whatever comes back. Worker thread only.

    Through ``QgsBlockingNetworkRequest`` for the same reasons GET is: the proxy
    configuration, the SSL exceptions and -- the one that matters -- the authentication
    database, which is the only place the API token exists.

    A create returns ``201`` with a ``Location`` header and frequently an empty body, so an
    empty response is ``None`` rather than a parse error. The body is not needed at all:
    the server assigns ``label_id`` and this client deliberately never learns it, because
    holding a client-side copy of an identity it did not issue is the defect this whole
    schema exists to remove.

    Which is why an unparseable body on a *successful* status is also ``None`` rather than
    an error. The status already said the row was created; raising over the shape of a
    body nobody reads would report a feature that landed as one the server refused, and
    the only repair for a refusal that did not happen is to publish it a second time.
    A non-2xx status still raises, in :func:`_read`, before any of this.

    That last paragraph is why the *bulk* publish reads the returned document rather than
    the status: an atomic batch answers with the count it created, and a caller that
    treated a 201 as "all of them" would be crediting features on faith again.
    """
    request, fetcher = _prepare(url, accept, authcfg, track, reason)
    request.setRawHeader(b"Content-Type", content_type.encode("ascii"))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    error = fetcher.post(request, QByteArray(body), False, feedback)
    response = _read(fetcher, error, url)
    if not response.body.strip():
        return None
    try:
        return response.json()
    except BackendError:
        return None


def post_form(
    url: str,
    fields: Mapping[str, str],
    *,
    feedback: QgsFeedback | None = None,
) -> Any:
    """POST a form-encoded body with **no** credential attached. Worker thread only.

    For Google's token and revocation endpoints, which authenticate the request *body* --
    the authorization code plus its PKCE verifier, or the refresh token. Attaching an
    ``authcfg`` here would send the labeling API's bearer token to Google, which is both
    useless and a credential sent somewhere it was not issued for.

    Through ``QgsBlockingNetworkRequest`` anyway, rather than reaching for the standard
    library, for the reasons in the module docstring minus the last one: the user's proxy
    configuration and their SSL exceptions. A sign-in that works everywhere except behind
    a corporate proxy is a sign-in that does not work.
    """
    request, fetcher = _prepare(url, "application/json", "", "")
    request.setRawHeader(b"Content-Type", FORM_MEDIA_TYPE.encode("ascii"))
    error = fetcher.post(request, QByteArray(oauth.encode_form(fields)), False, feedback)

    # The OAuth error object is read BEFORE _read turns the status into prose, because the
    # difference between `invalid_grant` and every other error is the difference between
    # "sign in again" and "retry in a minute" -- and _read would flatten both into one
    # BackendError whose only distinguishing feature is a substring. Anything that is not
    # an OAuth error object falls straight through, so there is still exactly one place
    # that handles HTTP status.
    reply = fetcher.reply()
    body = bytes(reply.content()) if reply is not None else b""
    oauth.raise_for_token_error(_json_object(body) or {})

    response = _read(fetcher, error, url)
    if not response.body.strip():
        # The revocation endpoint answers 200 with nothing, and that is a success.
        return None
    return response.json()
