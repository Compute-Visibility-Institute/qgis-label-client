"""Credentials, held by ``QgsAuthManager`` and referenced by id.

WHY THE TOKEN GOES IN qgis-auth.db AND NOWHERE ELSE

The `api` service authenticates desktop clients with a bearer token -- a Google ID token,
obtained by :mod:`..oauth_flow` and renewed by the plugin roughly hourly (IAP is
cookie-based and awkward outside a browser). A token in a ``.qgz`` travels with the
project the first time someone emails it; a token in ``QGIS3.ini`` ends up in a support
bundle; a token in this repository would be in a public GitHub repo forever.
``QgsAuthManager`` encrypts it in ``qgis-auth.db`` and hands out a seven-character id, and
that id is the only thing that appears in project files, settings and layer URIs.

WHERE THE REFRESH TOKEN GOES, AND WHY NOT HERE

Not in the config map. The ``APIHeader`` method emits **every** key in that map as an
outgoing HTTP header, so a ``refresh_token`` entry would be sent to the backend -- and to
anything else the credential is ever attached to -- on every single request. It is stored
as an *auth setting* instead (:func:`store_refresh_token`), encrypted in the same database
but never attached to a request. That distinction is the whole reason there are two
storage calls here rather than one bigger config map.

WHY THE APIHeader METHOD

QGIS ships several auth methods; ``APIHeader`` sets arbitrary HTTP headers on outgoing
requests. That is exactly a bearer token, and -- the part that matters -- it applies to
requests made by *QGIS core providers*, not only to requests this plugin makes. Putting
``authcfg=`` in the OAPIF layer URI means the native provider authenticates its own reads
and its Part 4 writes with no plugin code in the path at all.

ONE CONFIG PER HISTORY TRACK

A track is an isolated dataset sharing one deployment, and it reaches the backend as an
``X-Track`` header. Storing one config per track -- same token, one extra header -- makes
the credential itself say which dataset a request is for, so a layer holding an
``authcfg`` cannot be talking to the wrong track.

That is belt; the brace is :func:`qgis_label_client.core.uri.header_params`, which puts
``X-Track`` in the layer URI as well. Both exist because of an ordering problem with no
tidy answer: **signing in happens before Connect**, since you need a credential to
discover what tracks exist, so the first sign-in cannot know the track names. It writes
one config under the ``""`` key -- a credential naming no track -- and a later sign-in,
once tracks are known, fans out. Nothing here ever reads a stored token back out to clone
it into a new config: the token is passed in, used once and dropped, and that property is
worth more than the convenience of an automatic fan-out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from qgis.core import QgsApplication, QgsAuthMethodConfig

from .core.errors import ConfigurationError
from .core.tracks import TRACK_HEADER
from .log import log_warning

#: QGIS auth method that injects arbitrary request headers.
AUTH_METHOD = "APIHeader"

#: Header the auth edge reads.
AUTH_HEADER = "Authorization"

#: Name shown in QGIS's Authentication settings so the entry is recognisable there.
CONFIG_NAME = "CVI labeling API"

#: Key in the ``{track: authcfg}`` map for a credential that names no track. Requests made
#: with it reach whatever the deployment's default track is, which is what the API does
#: with any read that does not say.
DEFAULT_TRACK_KEY = ""

#: Where the OAuth refresh token lives: an *auth setting*, not a config map entry.
#:
#: The distinction is the security property, not a filing preference. Auth settings are
#: encrypted in qgis-auth.db and are never attached to a request by any auth method; the
#: APIHeader config map, by contrast, is emitted verbatim as request headers. A refresh
#: token in the config map would be sent to the backend on every request -- and a refresh
#: token is a far worse thing to leak than the hour-long ID token it mints, because it
#: does not expire.
REFRESH_TOKEN_SETTING = "cvi/refresh_token"


@dataclass(frozen=True)
class AuthConfigSummary:
    """What is safe to display about a stored credential. Never the secret."""

    authcfg: str
    name: str
    method: str

    def describe(self) -> str:
        return f"{self.name} [{self.authcfg}] via {self.method}"


def auth_manager():
    """The QGIS authentication manager, or raise with an actionable message."""
    manager = QgsApplication.authManager()
    if manager is None or manager.isDisabled():
        raise ConfigurationError(
            "QGIS's authentication system is disabled, so credentials cannot be stored "
            "securely. It is usually disabled because qgis-auth.db is missing or "
            "unwritable - see Settings > Options > Authentication."
        )
    return manager


def master_password_ready(prompt: bool = True) -> bool:
    """Ensure the auth database is unlocked.

    First use forces the user to set a master password, which is unrecoverable if
    forgotten. That is a real support burden for a new annotator, so it is triggered
    explicitly by a button press with an explanation next to it, never as a side effect
    of loading a layer.
    """
    manager = auth_manager()
    if manager.masterPasswordIsSet():
        return True
    if not prompt:
        return False
    # verify=True makes QGIS ask twice when setting a new password rather than once.
    return bool(manager.setMasterPassword(True))


def store_id_token(token: str, existing_authcfg: str = "", track: str = "") -> str:
    """Store or replace the API bearer token for one track; return its ``authcfg`` id.

    The token is passed in, used once and not returned, logged or stored anywhere else.
    Reusing `existing_authcfg` where possible matters: the id is written into layer URIs
    and possibly into a saved project, so rotating a token must not invalidate them.

    `track` adds an ``X-Track`` header alongside the bearer token, so every request this
    credential authenticates -- including the ones QGIS's own OAPIF provider makes, which
    no plugin code can reach -- names the dataset it is for. Empty means "name no track",
    which lands on the deployment default.
    """
    token = (token or "").strip()
    if not token:
        raise ConfigurationError("No token supplied.")
    manager = auth_manager()
    if not master_password_ready():
        raise ConfigurationError("The QGIS authentication database was not unlocked.")

    config = QgsAuthMethodConfig()
    if existing_authcfg:
        loaded, config = manager.loadAuthenticationConfig(existing_authcfg, config, True)
        if not loaded:
            config = QgsAuthMethodConfig()

    config.setName(config_name(track))
    config.setMethod(AUTH_METHOD)
    config.setVersion(1)
    # setConfigMap replaces the whole map, so a rotated token cannot leave the old header
    # value behind alongside the new one -- and, now, so a credential reused for a
    # different track cannot keep the old track's header.
    headers = {AUTH_HEADER: f"Bearer {token}"}
    if track:
        headers[TRACK_HEADER] = track
    config.setConfigMap(headers)

    if existing_authcfg and config.id():
        stored, config = manager.storeAuthenticationConfig(config, True)
    else:
        stored, config = manager.storeAuthenticationConfig(config, False)
    if not stored or not config.id():
        raise ConfigurationError(
            "QGIS refused to store the credential. Check the Authentication tab in "
            "Settings > Options."
        )
    # The renewal path rewrites this config every hour under the SAME id, which is what
    # keeps saved projects and loaded layers working. QGIS caches a decrypted config per
    # id, so without this the method would keep serving the previous hour's header from
    # cache -- a refresh that writes a fresh token nothing ever sends, which looks exactly
    # like the token not refreshing at all.
    clear_cached_config(config.id())
    return config.id()


def clear_cached_config(authcfg: str) -> None:
    """Drop QGIS's decrypted copy of one config, so the next request re-reads it.

    Best-effort on purpose. ``clearCachedConfig`` is not guaranteed to be exposed through
    SIP on every build this plugin supports, and a missing binding must degrade to "the
    header may be stale until QGIS is restarted" rather than to a failed sign-in.
    """
    if not authcfg:
        return
    manager = QgsApplication.authManager()
    clear = getattr(manager, "clearCachedConfig", None) if manager is not None else None
    if clear is None:
        return
    try:
        clear(authcfg)
    except Exception as exc:  # noqa: BLE001 - a cache hint must never fail a rotation
        log_warning(f"Could not clear the cached credential for {authcfg}: {exc}")


def store_refresh_token(token: str) -> bool:
    """Keep the refresh token in qgis-auth.db, out of every outgoing request.

    Returns False rather than raising when this QGIS build does not expose the auth-setting
    API. That degrades honestly: sign-in still works, the ID token is still stored, and the
    session simply ends in an hour instead of renewing itself -- which the caller says out
    loud. Raising instead would refuse a sign-in that would otherwise have worked.
    """
    if not token:
        return False
    manager = auth_manager()
    store = getattr(manager, "storeAuthSetting", None)
    if store is None:
        return False
    try:
        return bool(store(REFRESH_TOKEN_SETTING, token, True))
    except Exception as exc:  # noqa: BLE001 - see the docstring: degrade, never refuse
        log_warning(f"Could not store the sign-in renewal token: {exc}")
        return False


def read_refresh_token() -> str:
    """The stored refresh token, or ``""``.

    Needs the master password entered this session, because the value is encrypted. An
    empty answer therefore means either "never signed in with Google" or "the database is
    locked", and the caller has to check :func:`master_password_ready` to tell them apart
    -- reporting a locked database as a lost sign-in would send the analyst through a
    browser round-trip that fixes nothing.
    """
    manager = auth_manager()
    read = getattr(manager, "authSetting", None)
    if read is None:
        return ""
    try:
        value = read(REFRESH_TOKEN_SETTING, "", True)
    except Exception as exc:  # noqa: BLE001 - a missing binding is not a sign-in failure
        log_warning(f"Could not read the sign-in renewal token: {exc}")
        return ""
    return str(value or "").strip()


def clear_refresh_token() -> bool:
    """Destroy the local copy of the refresh token. Half of signing out.

    The other half is revoking it at Google, which the plugin does, because a refresh token
    that still works is a live grant on the analyst's account -- and this module promises
    that signing out actually removes the credential.
    """
    manager = auth_manager()
    remove = getattr(manager, "removeAuthSetting", None)
    if remove is None:
        return False
    try:
        return bool(remove(REFRESH_TOKEN_SETTING))
    except Exception as exc:  # noqa: BLE001 - sign-out must complete even if this fails
        log_warning(f"Could not remove the stored sign-in renewal token: {exc}")
        return False


def config_name(track: str = "") -> str:
    """The name shown in QGIS's Authentication settings.

    Names the track, because there is now more than one entry and a list of identical
    names is a list nobody can rotate or clean up by hand.
    """
    return f"{CONFIG_NAME} [{track}]" if track else CONFIG_NAME


def store_id_token_for_tracks(
    token: str,
    tracks: Sequence[str] = (),
    existing: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Store the token once per track, plus once un-tracked; return ``{track: authcfg}``.

    ROTATION IS THE REASON THIS TAKES THE WHOLE MAP

    A stale credential is a layer that 401s, and with one config per track a rotation that
    updated only the current track would leave every other track's layers broken -- in a
    way that looks like an outage rather than like a rotation. Passing `existing` means
    every id is *reused* rather than replaced, so saved projects and loaded layers keep
    working; passing it in full means none of them is missed.

    The ``""`` entry is always written. It is what a sign-in performed before Connect
    produces, it is the fallback :meth:`PluginSettings.authcfg_for` reaches for, and
    keeping it means a track added on the server after the last sign-in still has a
    working credential -- the track then travels in the layer URI's ``X-Track`` header
    instead, which is why that second mechanism exists.
    """
    existing = dict(existing or {})
    wanted = [DEFAULT_TRACK_KEY, *(name for name in tracks if name)]
    stored: dict[str, str] = {}
    for name in dict.fromkeys(wanted):  # ordered, deduplicated
        stored[name] = store_id_token(token, existing.get(name, ""), name)
    # Entries for tracks that no longer exist are carried over rather than dropped: a
    # track missing from `tracks` may simply mean the panel has not connected yet, and
    # deleting a credential over that guess is not recoverable.
    for name, authcfg in existing.items():
        stored.setdefault(name, authcfg)
    return stored


def summarise(authcfg: str) -> AuthConfigSummary | None:
    """Describe a stored credential without touching the secret.

    ``full=False`` asks QgsAuthManager for the metadata only, so this works -- and is
    safe -- even when the master password has not been entered this session.
    """
    if not authcfg:
        return None
    manager = auth_manager()
    if authcfg not in manager.configIds():
        return None
    config = QgsAuthMethodConfig()
    loaded, config = manager.loadAuthenticationConfig(authcfg, config, False)
    if not loaded:
        return None
    return AuthConfigSummary(
        authcfg=authcfg,
        name=config.name() or CONFIG_NAME,
        method=config.method() or AUTH_METHOD,
    )


def remove(authcfg: str) -> bool:
    """Delete a stored credential. Signing out should actually remove the token."""
    if not authcfg:
        return False
    return bool(auth_manager().removeAuthenticationConfig(authcfg))


def remove_all(authcfgs: Mapping[str, str] | None) -> int:
    """Delete every stored credential; return how many went. Signing out means all of them.

    One per track means signing out has to be one act, not one per track. A credential
    left behind is a token still on disk after the analyst was told it was gone.

    THE REFRESH TOKEN GOES TOO, and it goes here rather than at the call site so that no
    future caller can forget it. Removing the ID-token configs while leaving the refresh
    token behind would leave the strongest credential of the three on disk after the panel
    said the sign-in was gone -- which is the promise in this module's docstring, broken
    in the one direction nobody would notice.
    """
    removed = 0
    for authcfg in dict(authcfgs or {}).values():
        if remove(authcfg):
            removed += 1
    clear_refresh_token()
    return removed
