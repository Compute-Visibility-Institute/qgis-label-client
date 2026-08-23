"""Plugin settings, held in ``QgsSettings``.

WHAT IS AND IS NOT ALLOWED IN HERE

This repository is public. There are no deployment hostnames in it, so every backend URL
is a user setting whose default is empty and whose *placeholder* -- the greyed-out hint in
the field -- is an obviously fake example. An empty default is not laziness: a real
default would be a hostname committed to a public repo.

No credential is stored here either. The only auth-related value is ``authcfg``, the
seven-character id of an entry in ``qgis-auth.db``. The token itself never leaves
``QgsAuthManager``, which means it is never in a ``.qgz``, never in ``QGIS3.ini``, and
never in a support bundle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from qgis.core import QgsSettings

from .core.asof import AsOfMechanism

#: QgsSettings group. One level, so everything is visible together in QGIS3.ini.
SETTINGS_GROUP = "cvi-label-client"

#: Shown as placeholder text, never used as a value. RFC 2606 reserves example.org, so
#: it can never accidentally resolve to something real.
PLACEHOLDER_API_URL = "https://api.example.org/oapif"

DEFAULTS: dict[str, Any] = {
    # Landing page of the `api` Cloud Run service's OGC API - Features endpoint.
    "api_base_url": "",
    # Reference into qgis-auth.db. NOT a token.
    "authcfg": "",
    # Paths, relative to api_base_url, of the two endpoints that are not OGC API -
    # Features. Paths rather than full URLs so a deployment that moves the API does not
    # have to update two settings, and settings rather than constants so a deployment
    # may mount them elsewhere.
    #
    # The `v1/` prefix is the backend's own namespace: everything outside it is proxied
    # verbatim to the feature service, so the two cannot collide. A path without it
    # reaches the feature service instead and comes back as an OAPIF error about an
    # unknown collection, which points at the wrong component entirely.
    "class_registry_path": "v1/classes",
    # Mints signed URLs for many captures at once -- one round trip per session, not one
    # per scene. Session start is exactly when a project's raster layers are all dead at
    # the same time.
    "signed_urls_path": "v1/imagery/signed-urls",
    # OAPIF page size. 1000 is pygeoapi's usual maximum; larger is silently clamped.
    "page_size": 1000,
    # Fetch only features overlapping the canvas. On by default because the compound
    # class spans 3428 x 2652 km and drawing one campus should not download the country.
    "restrict_to_canvas": True,
    # Valid-time as-of control.
    "as_of_enabled": False,
    "as_of_date": "",
    "as_of_mechanism": AsOfMechanism.DATETIME.value,
    # Collection ids. Discovered from /collections at runtime; these are only the
    # pre-selected defaults in the panel and are overwritten by what the user picks.
    "label_collection": "",
    "extent_collection": "",
    "history_collection": "",
    # Warn this many minutes before the signed URLs expire.
    "expiry_warning_minutes": 30,
    # Features per POST when publishing local layers. A FeatureCollection body turns 1,246
    # round trips into a couple of dozen; set it to 1 against a server that only accepts a
    # single Feature per create. Failures fall back to one at a time either way, so this is
    # a speed setting and never a correctness one.
    "publish_batch_size": 50,
}


def _coerce(key: str, value: Any) -> Any:
    """Coerce a value read back from QgsSettings to the type of its default.

    QgsSettings round-trips through the INI file on some platforms, so a bool written as
    ``True`` can come back as the string ``"true"`` and an int as ``"1000"``. Coercing
    against the default's type here means no call site has to remember that.
    """
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        return "" if value is None else str(value)
    return value


@dataclass
class PluginSettings:
    """Typed accessor over ``QgsSettings``.

    Deliberately not cached. QGIS settings can be changed from another panel or another
    profile mid-session, and a stale cached backend URL is a confusing bug to chase.
    """

    group: str = SETTINGS_GROUP

    def _store(self) -> QgsSettings:
        return QgsSettings()

    def _full_key(self, key: str) -> str:
        return f"{self.group}/{key}"

    def get(self, key: str) -> Any:
        if key not in DEFAULTS:
            raise KeyError(f"unknown setting {key!r}")
        raw = self._store().value(self._full_key(key), DEFAULTS[key])
        return _coerce(key, raw)

    def set(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            raise KeyError(f"unknown setting {key!r}")
        self._store().setValue(self._full_key(key), value)

    # --- convenience accessors for the values used in more than one place -------

    @property
    def api_base_url(self) -> str:
        return str(self.get("api_base_url")).strip()

    @property
    def authcfg(self) -> str:
        return str(self.get("authcfg")).strip()

    @property
    def as_of_mechanism(self) -> AsOfMechanism:
        return AsOfMechanism.parse(self.get("as_of_mechanism"))

    @property
    def as_of(self) -> date | None:
        """The as-of instant, or ``None`` when the control is off.

        Returns ``None`` rather than "today" when disabled, because the two mean
        different things to the backend: no ``datetime`` parameter asks for the current
        state, while ``datetime=<today>`` asks for the state valid at midnight this
        morning and would quietly hide anything created since.
        """
        if not self.get("as_of_enabled"):
            return None
        raw = str(self.get("as_of_date")).strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw).date()
        except ValueError:
            return None

    def set_as_of(self, value: date | None) -> None:
        self.set("as_of_enabled", value is not None)
        self.set("as_of_date", value.isoformat() if value else "")

    def is_configured(self) -> bool:
        """True when there is at least a backend URL to try."""
        return bool(self.api_base_url)
