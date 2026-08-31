"""Google sign-in, reduced to the parts that are arithmetic and string handling.

WHY THE PLUGIN RUNS THE FLOW INSTEAD OF QGIS'S OWN OAuth2 AUTH METHOD

QGIS 3.44 can carry an OpenID ``id_token`` into a request header, through the OAuth2
method's ``extraTokens`` map. It was rejected here for two reasons, both silent failures:

1. **It would evict ``X-Track``.** One auth config has one method, and ``extraTokens``
   maps *token-endpoint response fields* onto headers -- it cannot carry a constant. The
   track would stop reaching QGIS's own OAPIF requests, and
   :mod:`qgis_label_client.core.recorded` records the packet capture proving the auth
   config is the only channel that reaches them: the layer URI's ``http-header:``
   parameters send nothing. Every read and every Part 4 write would silently resolve to
   the deployment's default track, which is the populated-and-wrong failure
   :mod:`qgis_label_client.core.tracks` exists to prevent.
2. **The ID token is captured once and never refreshed.** QGIS sets ``extraTokens`` only
   on the initial code exchange; neither the refresh reply nor the synchronous refresh
   updates it. After roughly an hour QGIS goes on sending the original, expired JWT
   forever, and an editing session is longer than an hour.

So the plugin holds the refresh token itself and rewrites the ``APIHeader`` credential --
which keeps ``X-Track`` exactly where it already is and buys a genuinely fresh ID token
every hour with no browser round-trip.

WHY THIS MODULE IS PURE

Everything here is a function of strings, dictionaries and a clock. The Qt half -- opening
a browser, listening on a loopback socket -- is in :mod:`qgis_label_client.oauth_flow`,
and the credential writing is in :mod:`qgis_label_client.auth`. That split is the same one
the rest of this package makes, and it is what lets the parts where a mistake is *silent*
-- the PKCE challenge, the ``state`` comparison, the expiry arithmetic -- be tested at all.

WHAT THIS MODULE DELIBERATELY DOES NOT DO

It never verifies the ID token's signature. That is the server's job, and it has Google's
keys and the audience it will accept; a second, weaker verifier here would be a security
check that looks like one and is not. :func:`claims` decodes the payload without checking
anything, for exactly two values -- the address to put on screen and the expiry to
schedule a refresh from -- and both of those are only ever used to be *more* careful.

NO CLIENT SECRET. The client below is a Google "Desktop app" client shipped inside a
public GPL plugin, so a secret in it would not be one. PKCE (S256) is what actually binds
the code to this exchange, and Google documents the secret as optional for this client
type. Sending a secret that everyone can read would suggest a protection that does not
exist.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from .errors import LabelClientError

#: The desktop OAuth client this plugin signs in with. Public by construction: it
#: identifies the *application*, never the bearer, and is not an access-control decision
#: anywhere. Who may actually use the platform is decided by the server against the
#: verified address in the token.
CLIENT_ID = "513319405696-uenhllp36pheu0997ebb3tq6v4cjj513.apps.googleusercontent.com"

#: Where the browser is sent, where the code is exchanged, and where a refresh token is
#: destroyed. Constants rather than settings: they are Google's, not a deployment's, and a
#: user-editable authorization endpoint is a phishing target with a text field.
AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOCATION_ENDPOINT = "https://oauth2.googleapis.com/revoke"

#: ``openid`` is what makes Google return an ``id_token`` at all; ``email`` is what puts a
#: verified address in it, which is the only claim the server authorises on. ``profile``
#: buys nothing but a display name and is requested so the consent screen is honest about
#: what the plugin will show.
SCOPE = "openid email profile"

#: Only the loopback interface. A redirect to any other host would send the authorization
#: code somewhere off this machine.
LOOPBACK_HOST = "127.0.0.1"

#: Renew this long before the ID token dies. Five minutes is chosen against the clock skew
#: the *server* tolerates, not against how long a refresh takes: a token that expires
#: while a QGIS OAPIF request is in flight produces a 401 the plugin cannot retry, because
#: the request was made by the native provider and no plugin code is in its path.
REFRESH_SKEW_SECONDS = 300

#: How long the loopback listener waits for the browser before giving up. Long enough to
#: pick an account and click through consent, short enough that an abandoned sign-in does
#: not leave a socket open for the rest of the session.
CALLBACK_TIMEOUT_SECONDS = 120

#: Google's own name for "this refresh token is dead" -- revoked, expired, or invalidated
#: by a password change. It is the one token error that means "ask the person to sign in
#: again" rather than "something went wrong, retry".
INVALID_GRANT = "invalid_grant"


class SignInError(LabelClientError):
    """Sign-in did not produce a usable credential."""


class SignInCancelledError(SignInError):
    """The person closed the browser tab, or refused consent. Not a fault."""


class SignInExpiredError(SignInError):
    """The stored refresh token is dead. Only a new browser sign-in fixes this.

    Distinguished from every other failure because the *repair* is different and the
    message has to say so. Reported as "the server is down" it produces a support thread;
    reported as "sign in again" it produces a click.
    """


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    """Base64url with the padding stripped, which is what RFC 7636 specifies."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def new_verifier() -> str:
    """A fresh PKCE code verifier.

    64 random bytes render as 86 base64url characters, inside RFC 7636's 43-128 range and
    well above its 256-bit entropy floor. ``secrets`` rather than ``random``: this value
    is the only thing preventing another process on the machine from redeeming an
    intercepted authorization code, and ``random`` is seeded predictably enough to matter.
    """
    return _b64url(secrets.token_bytes(64))


def challenge(verifier: str) -> str:
    """The S256 challenge for `verifier`.

    S256 rather than ``plain``: the challenge travels in a URL that reaches the browser,
    the system's URL handler and Google's logs, and ``plain`` would put the verifier in
    all three -- which is the whole attack PKCE exists to stop.
    """
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def new_state() -> str:
    """An unguessable ``state`` value.

    Compared on the way back in :func:`parse_callback`. Without it, anything on the
    machine that can reach the loopback port could hand this plugin an authorization code
    minted for a different account, and the analyst would silently sign in as somebody
    else.
    """
    return _b64url(secrets.token_bytes(32))


# ---------------------------------------------------------------------------
# The browser round trip
# ---------------------------------------------------------------------------


def redirect_uri(port: int) -> str:
    """The loopback address Google should send the browser back to.

    The port is whatever the operating system handed the listener, because Google matches
    a loopback redirect on host and path and ignores the port for desktop clients. A fixed
    port would be one more thing to collide with, and one more thing to register.
    """
    return f"http://{LOOPBACK_HOST}:{int(port)}/"


def authorization_url(
    *,
    redirect: str,
    state: str,
    verifier: str,
    client_id: str = CLIENT_ID,
    login_hint: str = "",
) -> str:
    """The URL to open in the system browser.

    ``access_type=offline`` with ``prompt=consent`` is what guarantees a refresh token
    comes back. Google returns one only on a *consenting* authorization, so an analyst who
    has already approved this plugin would otherwise get an ID token, no refresh token,
    and a session that dies silently in an hour with nothing to renew it from. The cost is
    one extra consent screen per sign-in, which is a button; the alternative is a bug
    report an hour later.

    ``login_hint`` pre-selects an account. Empty for a first sign-in, and the previously
    signed-in address when renewing, so somebody with a personal and a work Google account
    in the same browser is not silently offered the wrong one.
    """
    params = [
        ("client_id", client_id),
        ("redirect_uri", redirect),
        ("response_type", "code"),
        ("scope", SCOPE),
        ("state", state),
        ("code_challenge", challenge(verifier)),
        ("code_challenge_method", "S256"),
        ("access_type", "offline"),
        ("prompt", "consent"),
    ]
    if login_hint:
        params.append(("login_hint", login_hint))
    return f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}"


def request_target(request_line: str) -> str:
    """The path-and-query out of an HTTP request line, or ``""``.

    The listener speaks just enough HTTP to read one line. Anything that is not a ``GET``
    is not the browser coming back -- port scanners and other software on the machine do
    connect to loopback ports -- and answering it as though it were would mean comparing
    ``state`` against nothing.
    """
    parts = request_line.strip().split()
    if len(parts) < 2 or parts[0].upper() != "GET":
        return ""
    return parts[1]


def parse_callback(target: str, expected_state: str) -> str:
    """The authorization code out of the redirect, or raise saying what went wrong.

    THE ``state`` COMPARISON IS THE SECURITY CHECK IN THIS FUNCTION. Anything running on
    the machine can connect to the loopback listener and present a code; without the
    comparison the plugin would exchange it, store the resulting credential, and the
    analyst would be signed in as whoever minted it -- with every label they then drew
    attributed to that account, in an append-only table.
    """
    query = dict(parse_qsl(urlsplit(target).query, keep_blank_values=True))

    error = query.get("error", "")
    if error:
        if error == "access_denied":
            raise SignInCancelledError(
                "Sign-in was cancelled in the browser. Nothing was changed; click "
                "Sign in with Google to try again."
            )
        detail = query.get("error_description", "")
        raise SignInError(f"Google refused the sign-in ({error}){f': {detail}' if detail else ''}.")

    state = query.get("state", "")
    if not expected_state or not secrets.compare_digest(state, expected_state):
        # compare_digest rather than ``==``: the comparison is against a secret this
        # process generated, and a timing-distinguishable comparison is a needless gift.
        raise SignInError(
            "The sign-in reply did not match the request this plugin made, so it was "
            "discarded. Nothing was stored. Try signing in again; if it keeps happening, "
            "something else on this machine is answering the loopback port."
        )

    code = query.get("code", "")
    if not code:
        raise SignInError("The sign-in reply carried no authorization code.")
    return code


def callback_page(message: str) -> bytes:
    """The whole HTTP response the browser tab is left showing.

    A complete response rather than a redirect: sending the browser anywhere else would
    put the authorization code in a second server's logs, and this page exists only so the
    person knows the tab has done its job and can be closed.
    """
    body = (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<title>CVI Label Client</title>"
        "<body style='font:16px system-ui,sans-serif;margin:4rem auto;max-width:32rem'>"
        f"<p>{html.escape(message)}</p>"
        "<p style='color:#666'>You can close this tab and return to QGIS.</p>"
    ).encode()
    head = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    return head + body


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


def encode_form(fields: Mapping[str, str]) -> bytes:
    """``application/x-www-form-urlencoded``, which is the only body Google's token
    endpoint accepts. Here rather than in the network layer so it can be tested."""
    return urlencode(sorted(fields.items())).encode("ascii")


def token_request(code: str, verifier: str, redirect: str, client_id: str = CLIENT_ID) -> dict:
    """Form fields exchanging an authorization code for tokens.

    NO ``client_secret``. See the module docstring: this is a desktop client shipped in a
    public repository, the verifier is what binds the exchange, and shipping a secret
    would imply a protection nobody has.
    """
    return {
        "client_id": client_id,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect,
    }


def refresh_request(refresh_token: str, client_id: str = CLIENT_ID) -> dict:
    """Form fields for the silent renewal. Same client, same absence of a secret."""
    return {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }


def revocation_request(token: str) -> dict:
    """Form fields destroying a refresh token at Google.

    Signing out has to be true on Google's side too. Deleting the local copy alone would
    leave a live, long-lived grant in the analyst's account while the panel said the
    credential was gone -- a promise :mod:`qgis_label_client.auth` makes explicitly.
    """
    return {"token": token}


def raise_for_token_error(payload: Any) -> None:
    """Turn Google's error object into the exception whose repair is right.

    ``invalid_grant`` is the one that must not be reported as an outage: the refresh token
    is dead and no amount of retrying will revive it. Everything else is transient or a
    bug, and says so.
    """
    if not isinstance(payload, Mapping):
        return
    error = str(payload.get("error") or "")
    if not error:
        return
    detail = str(payload.get("error_description") or "").strip()
    if error == INVALID_GRANT:
        raise SignInExpiredError(
            "Your Google sign-in is no longer valid, so it could not be renewed "
            f"({detail or error}). Click Sign in with Google to sign in again. Any layers "
            "already open will keep failing until you reload them."
        )
    raise SignInError(
        f"Google refused the token request ({error}){f': {detail}' if detail else ''}."
    )


# ---------------------------------------------------------------------------
# The ID token, read but never trusted
# ---------------------------------------------------------------------------


def claims(id_token: str) -> dict:
    """The JWT payload, decoded and **not verified**.

    Two values are wanted: ``email``, to say on screen who is signed in, and ``exp``, to
    know when to renew. Neither is an authorization decision -- the server verifies the
    signature, the issuer and the audience, and decides who may do what. A returned
    ``{}`` therefore degrades to "no address shown, fall back to ``expires_in``" rather
    than to a failure: a token this cannot parse may still be one the server accepts.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        return {}
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


@dataclass(frozen=True)
class Credential:
    """One signed-in session: what to send, what to renew with, and when.

    ``refresh_token`` may be empty, and that is a state worth naming rather than an error:
    Google returns one only on a consenting authorization, and never on a refresh. An
    empty one after a *renewal* means "keep the one you already have"; an empty one after
    a *sign-in* means this session ends in an hour and the analyst has to be told.
    """

    id_token: str
    refresh_token: str
    email: str
    expires_at: int

    def needs_refresh(self, now: float, skew: int = REFRESH_SKEW_SECONDS) -> bool:
        return needs_refresh(self.expires_at, now, skew)


def credential_from_token_response(
    payload: Any,
    now: float,
    refresh_token: str = "",
) -> Credential:
    """Read a token-endpoint reply, or raise saying what is wrong with it.

    EXPIRY COMES FROM THE ID TOKEN'S ``exp``, NOT FROM ``expires_in``. They are usually
    the same hour, but ``expires_in`` describes the *access token* -- a different
    credential, which this plugin does not send anywhere. Scheduling the renewal off the
    wrong one is how a token expires five minutes before the timer that was meant to
    replace it.
    """
    raise_for_token_error(payload)
    if not isinstance(payload, Mapping):
        raise SignInError("Google's token endpoint did not return a JSON object.")

    id_token = str(payload.get("id_token") or "")
    if not id_token:
        raise SignInError(
            "Google returned no ID token. The sign-in did not request the 'openid' scope, "
            "or this OAuth client is not configured for it."
        )

    decoded = claims(id_token)
    expires_at = decoded.get("exp")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        # A token whose payload could not be read is still one the server may accept, so
        # fall back to the reply's own lifetime rather than refusing the sign-in. The
        # 3600 is Google's documented default and only ever makes the renewal earlier.
        lifetime = payload.get("expires_in")
        lifetime = lifetime if isinstance(lifetime, (int, float)) else 3600
        expires_at = now + float(lifetime)

    return Credential(
        id_token=id_token,
        refresh_token=str(payload.get("refresh_token") or refresh_token or ""),
        email=str(decoded.get("email") or ""),
        expires_at=int(expires_at),
    )


# ---------------------------------------------------------------------------
# Expiry arithmetic -- the sharp edge
# ---------------------------------------------------------------------------


def needs_refresh(expires_at: int, now: float, skew: int = REFRESH_SKEW_SECONDS) -> bool:
    """True when the token should be renewed before anything else is attempted.

    ``expires_at`` of 0 means "no Google session" -- a profile that has never signed in,
    or one still holding a hand-pasted token -- and answers False, because there is
    nothing to renew and nagging about it would be noise.

    An *already expired* token answers True, which is the case the timer cannot cover: a
    laptop that slept through the renewal wakes with a dead credential and a timer that
    fired late or not at all. Every path that is about to issue a request asks this first,
    so the repair happens before the 401 rather than after it.
    """
    if expires_at <= 0:
        return False
    return now >= expires_at - max(skew, 0)


def seconds_until_refresh(expires_at: int, now: float, skew: int = REFRESH_SKEW_SECONDS) -> float:
    """How long a renewal timer should wait. Never negative, so an armed timer fires."""
    if expires_at <= 0:
        return 0.0
    return max(0.0, (expires_at - max(skew, 0)) - now)


def describe_session(email: str, expires_at: int, now: float) -> str:
    """One line for the panel: who is signed in, and how long that stays true.

    The remaining time is shown rather than the expiry instant because the number people
    act on is "do I have time to finish this before something happens", and because an
    absolute timestamp in an unstated zone is the classic way to be read off by an hour.
    """
    who = f"Signed in as {email}" if email else "Signed in with Google"
    if expires_at <= 0:
        return who
    remaining = expires_at - now
    if remaining <= 0:
        return f"{who} - sign-in expired, renewing"
    minutes = int(remaining // 60)
    if minutes < 1:
        return f"{who} - renewing now"
    return f"{who} - renews in about {minutes} min"
