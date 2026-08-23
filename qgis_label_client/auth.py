"""Credentials, held by ``QgsAuthManager`` and referenced by id.

WHY THE TOKEN GOES IN qgis-auth.db AND NOWHERE ELSE

The `api` service authenticates desktop clients with bearer tokens (IAP is cookie-based
and awkward outside a browser). A token in a ``.qgz`` travels with the project the first
time someone emails it; a token in ``QGIS3.ini`` ends up in a support bundle; a token in
this repository would be in a public GitHub repo forever. ``QgsAuthManager`` encrypts it
in ``qgis-auth.db`` and hands out a seven-character id, and that id is the only thing
that appears in project files, settings and layer URIs.

WHY THE APIHeader METHOD

QGIS ships several auth methods; ``APIHeader`` sets arbitrary HTTP headers on outgoing
requests. That is exactly a bearer token, and -- the part that matters -- it applies to
requests made by *QGIS core providers*, not only to requests this plugin makes. Putting
``authcfg=`` in the OAPIF layer URI means the native provider authenticates its own reads
and its Part 4 writes with no plugin code in the path at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from qgis.core import QgsApplication, QgsAuthMethodConfig

from .core.errors import ConfigurationError

#: QGIS auth method that injects arbitrary request headers.
AUTH_METHOD = "APIHeader"

#: Header the auth edge reads.
AUTH_HEADER = "Authorization"

#: Name shown in QGIS's Authentication settings so the entry is recognisable there.
CONFIG_NAME = "CVI labeling API"


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


def store_bearer_token(token: str, existing_authcfg: str = "") -> str:
    """Store or replace the API bearer token; return its ``authcfg`` id.

    The token is passed in, used once and not returned, logged or stored anywhere else.
    Reusing `existing_authcfg` where possible matters: the id is written into layer URIs
    and possibly into a saved project, so rotating a token must not invalidate them.
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

    config.setName(CONFIG_NAME)
    config.setMethod(AUTH_METHOD)
    config.setVersion(1)
    # setConfigMap replaces the whole map, so a rotated token cannot leave the old header
    # value behind alongside the new one.
    config.setConfigMap({AUTH_HEADER: f"Bearer {token}"})

    if existing_authcfg and config.id():
        stored, config = manager.storeAuthenticationConfig(config, True)
    else:
        stored, config = manager.storeAuthenticationConfig(config, False)
    if not stored or not config.id():
        raise ConfigurationError(
            "QGIS refused to store the credential. Check the Authentication tab in "
            "Settings > Options."
        )
    return config.id()


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
