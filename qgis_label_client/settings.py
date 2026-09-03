"""Plugin settings, held in ``QgsSettings``.

WHAT IS AND IS NOT ALLOWED IN HERE

This repository is public. There are no deployment hostnames in it, so every backend URL
is a user setting whose default is empty and whose *placeholder* -- the greyed-out hint in
the field -- is an obviously fake example. An empty default is not laziness: a real
default would be a hostname committed to a public repo.

No credential is stored here either. The only auth-related values are ``authcfg`` ids --
seven-character references to entries in ``qgis-auth.db``, one per history track. The
token itself never leaves ``QgsAuthManager``, which means it is never in a ``.qgz``, never
in ``QGIS3.ini``, and never in a support bundle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from qgis.core import QgsSettings

from .core import recorded
from .core.asof import AsOfMechanism

#: QgsSettings group. One level, so everything is visible together in QGIS3.ini.
SETTINGS_GROUP = "cvi-label-client"

#: Shown as placeholder text, never used as a value. RFC 2606 reserves example.org, so
#: it can never accidentally resolve to something real.
PLACEHOLDER_API_URL = "https://api.example.org/oapif"

DEFAULTS: dict[str, Any] = {
    # Landing page of the `api` Cloud Run service's OGC API - Features endpoint.
    #
    # PROVISIONAL, and deliberately so. This is the generated run.app hostname, which is
    # stable for the life of the service but is not the address this deployment intends
    # to keep: a custom domain is already delegated in Terraform and waiting on NS records
    # at the registrar. When that lands, this default changes and every analyst who never
    # touched the field follows automatically -- which is the reason to have a default at
    # all rather than making each of them paste it.
    #
    # An analyst who HAS edited the field keeps their value: QgsSettings only falls back
    # to this when the key is absent. So changing it later cannot silently repoint
    # somebody who deliberately aimed at a different deployment.
    "api_base_url": "https://api-xzuhhhdboa-zf.a.run.app",
    # Google's client secret for the DESKTOP OAuth client in core.oauth.CLIENT_ID.
    #
    # NOT A SECRET, AND STILL NOT HARDCODED. Google documents an installed app's secret
    # as not confidential -- it is compiled into the binary and cannot be one -- while
    # still requiring it as a client identifier on the token endpoint. Without it the
    # exchange fails with `invalid_request: client_secret is missing` AFTER the analyst
    # has already consented in the browser, which is the worst possible moment to fail.
    #
    # It lives here rather than beside CLIENT_ID because this repository is public and
    # GPL: a value committed there is public permanently and could only be rotated by
    # cutting a release. The client id is hardcoded because it is on every authorization
    # URL anyway; the secret is deployment configuration, distributed with the API URL.
    "oauth_client_secret": "",
    # Reference into qgis-auth.db. NOT a token.
    #
    # Kept for one reason only: profiles written before history tracks existed have a
    # value here, and `authcfg_by_track` promotes it rather than silently signing the
    # user out. Nothing writes it any more. See PluginSettings.authcfg_by_track.
    "authcfg": "",
    # {track name: authcfg}, JSON. One credential per track, plus "" for the deployment
    # default -- the entry a sign-in made before any track was known.
    #
    # Still not a token: every value is a seven-character reference into qgis-auth.db.
    "authcfg_by_track": "",
    # Who is signed in, for the panel label. An email address, not a credential: it is
    # already visible in the QGIS window and on every server log line, and holding it here
    # is what lets the panel say "Signed in as ..." without decrypting anything -- which
    # matters because reading the credential needs the master password and the panel
    # refreshes on startup, before anybody has entered it.
    "oauth_email": "",
    # When the stored ID token dies, as epoch seconds. NOT a credential either: it is one
    # integer out of a token the server issued, and it is what the renewal timer and the
    # lazy pre-flight check both read.
    #
    # Held here rather than derived from the stored token on demand for the same reason as
    # the address: decoding the token means decrypting it, and the schedule has to be
    # readable while the auth database is still locked. 0 means "no Google session" --
    # a fresh profile, or one still holding a hand-pasted token -- and nothing is renewed.
    "oauth_expires_at": 0,
    # Which history track this profile works in. Empty means "whatever the deployment
    # defaults to", which is what a fresh profile has and what the API does with a read
    # that names no track.
    #
    # There is deliberately no track name in this file. Tracks are data, exactly like
    # classes: 'test' and 'production' are rows on the server, and a default here would be
    # the beginning of a second copy of the deployment's vocabulary.
    "track": "",
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
    # History tracks. Same argument as the class registry above: "which isolated datasets
    # does this deployment hold?" is not a features question, so it is not an OGC endpoint
    # and its path is a setting rather than a constant.
    "tracks_path": "v1/tracks",
    # Mints signed URLs for many captures at once -- one round trip per session, not one
    # per scene. Session start is exactly when a project's raster layers are all dead at
    # the same time.
    "signed_urls_path": "v1/imagery/signed-urls",
    # What this deployment can do. Read once before a publish run, to find out whether the
    # backend offers the atomic bulk create and, if it does, the two numbers needed to
    # chunk against it. Same namespace argument as the three paths above.
    #
    # A backend that predates the endpoint answers 404 from inside that namespace, which
    # is how "no bulk here" is recognised rather than guessed. See core.bulk.
    "capabilities_path": "v1/capabilities",
    # OAPIF page size. 1000 is pygeoapi's usual maximum; larger is silently clamped.
    "page_size": 1000,
    # Fetch only features overlapping the canvas. On by default because the compound
    # class spans 3428 x 2652 km and drawing one campus should not download the country.
    "restrict_to_canvas": True,
    # Valid-time as-of control.
    "as_of_enabled": False,
    "as_of_date": "",
    "as_of_mechanism": AsOfMechanism.DATETIME.value,
    # Transaction-time historical view: the instant the picker opens on.
    #
    # THE PICKER'S DEFAULT, AND NOTHING ELSE. The instant a *layer* is a view of lives in
    # that layer's own data source and in a custom property on it, for exactly the reason
    # the track does: a layer must not be re-aimed at another instant by a setting somebody
    # changed afterwards, and a .qgz has to reopen on what it was saved with. Nothing in
    # layers.py reads this key, and nothing should start to.
    "recorded_at": "",
    # Collection ids. Discovered from /collections at runtime; these are only the
    # pre-selected defaults in the panel and are overwritten by what the user picks.
    #
    # label_collection is now a HINT rather than a destination. Labels live in one
    # collection per geometry type -- a cooling unit is a Point, a powerline is a
    # LineString -- and which one a layer publishes to is resolved from its geometry
    # against what the backend lists (qgis_label_client.core.routing). What this key
    # supplies is which FAMILY of collections holds labels at all, matched by stem, so a
    # value stored before the split ("label") still selects the split collections
    # afterwards and nothing has to migrate it. It is still the destination outright on a
    # deployment that serves a single untyped collection.
    "label_collection": "",
    "extent_collection": "",
    "history_collection": "",
    # Which collection serves the transaction-time view. Asked for once and remembered,
    # exactly like history_collection: collection ids are a deployment's choice, and
    # guessing one produces a 404 that reads like an outage.
    "recorded_collection": "",
    # Warn this many minutes before the signed URLs expire.
    "expiry_warning_minutes": 30,
    # Features per request when the backend offers the atomic bulk create. A CEILING on
    # this side, always clamped down to whatever the deployment says it will take, and
    # ignored entirely by the one-feature-per-request path a backend without the endpoint
    # still gets.
    #
    # There was deliberately no batch size here until the server grew a bulk endpoint that
    # inserts in ONE transaction. That is what makes a size a safe thing to have: a chunk
    # lands whole or not at all, so nothing can be partly applied and nothing has to be
    # re-sent to find out.
    #
    # 200 rather than the 500 the deployment currently allows, for two reasons that both
    # get worse as the number grows. A refusal is all-or-nothing and takes its whole chunk
    # with it, so the chunk size is also the cost of one bad row. And progress is reported
    # per completed chunk, so 1,807 features at 500 is a bar that moves four times.
    # Against fifteen minutes of one-at-a-time publishing the difference between 4 and 9
    # requests is nothing; the difference between those two failure modes is not.
    "publish_chunk_size": 200,
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
    def oauth_client_secret(self) -> str:
        """Google's client secret for the desktop OAuth client. See DEFAULTS."""
        return str(self.get("oauth_client_secret")).strip()

    @property
    def track(self) -> str:
        """The history track this profile works in, or ``""`` for the deployment default."""
        return str(self.get("track")).strip()

    @property
    def authcfg_by_track(self) -> dict[str, str]:
        """``{track name: authcfg}``, with the pre-tracks setting promoted.

        WHY THIS IS A MAP AND NOT A STRING

        Because the ``authcfg`` is what a *layer URI* names, and a layer belongs to one
        track. Storing one credential per track means a rotation can be applied to all of
        them at once and a layer can never end up authenticating as one track while asking
        for another.

        THE MIGRATION, WHICH IS THE PART WORTH READING

        A profile written before this existed has a plain ``authcfg`` and no map. Reading
        that as "no credentials" would sign the analyst out on upgrade, in a plugin whose
        sign-in flow sets an unrecoverable master password -- a support call, for a change
        they did not ask for. So the old value is promoted onto the ``""`` key, which is
        exactly what it meant: a credential that names no track.

        The promotion is a read, not a write. Nothing here rewrites the stored setting, so
        downgrading the plugin leaves the old profile working.
        """
        raw = str(self.get("authcfg_by_track") or "").strip()
        mapping: dict[str, str] = {}
        if raw:
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, dict):
                mapping = {
                    str(key): str(value).strip()
                    for key, value in decoded.items()
                    if isinstance(key, str) and value
                }
        legacy = str(self.get("authcfg")).strip()
        if legacy and "" not in mapping:
            mapping[""] = legacy
        return mapping

    def set_authcfg_by_track(self, mapping: dict[str, str]) -> None:
        """Store the whole map. Values are references into qgis-auth.db, never tokens."""
        cleaned = {str(k): str(v).strip() for k, v in mapping.items() if v}
        self.set("authcfg_by_track", json.dumps(cleaned, sort_keys=True))
        # The legacy single-value setting is cleared once a map exists, so the promotion
        # above cannot resurrect a credential that has since been signed out of.
        self.set("authcfg", "")

    def authcfg_for(self, track: str = "") -> str:
        """The credential a request on `track` should use.

        Falls back to the unnamed default entry, and that fallback is load-bearing rather
        than tidy: sign-in happens *before* Connect -- you need a credential to discover
        what tracks exist -- so the common case is one credential stored under ``""`` and
        used by every track. The track itself travels in the ``X-Track`` header, which
        does not depend on this at all.
        """
        mapping = self.authcfg_by_track
        return mapping.get(track) or mapping.get("", "")

    @property
    def authcfg(self) -> str:
        """The credential for the currently selected track."""
        return self.authcfg_for(self.track)

    @property
    def oauth_email(self) -> str:
        """The signed-in address, or ``""``. For display, never for authorisation."""
        return str(self.get("oauth_email")).strip()

    @property
    def oauth_expires_at(self) -> int:
        """Epoch seconds at which the stored ID token dies, or 0 for "no Google session".

        Zero is the answer for a profile that never signed in with Google *and* for one
        holding a hand-pasted token, and the two are deliberately not distinguished: in
        both cases there is nothing this plugin can renew, so the renewal machinery stays
        silent rather than nagging about a credential it does not manage.
        """
        try:
            return max(0, int(self.get("oauth_expires_at")))
        except (TypeError, ValueError):
            return 0

    def set_oauth_session(self, email: str, expires_at: int) -> None:
        """Remember who signed in and when their token dies. Neither value is a secret."""
        self.set("oauth_email", email or "")
        self.set("oauth_expires_at", max(0, int(expires_at)))

    def clear_oauth_session(self) -> None:
        """Forget the session. Called on sign-out, so the panel cannot claim a stale one.

        Clearing ``oauth_expires_at`` also disarms the renewal timer, which otherwise would
        keep trying to renew a sign-in the analyst has just ended.
        """
        self.set_oauth_session("", 0)

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

    @property
    def recorded_at(self) -> str:
        """The instant the historical-view picker should open on, or ``""``.

        A remembered *default*, never a layer's state -- see the DEFAULTS entry. Validated
        on the way out rather than trusted: a hand-edited QGIS3.ini could otherwise put a
        string on the wire that the database's echo can never match, which shows up as an
        empty layer and reads as a backend outage.
        """
        stored = str(self.get("recorded_at")).strip()
        return stored if recorded.parse_instant(stored) is not None else ""

    def set_recorded_at(self, moment: str) -> None:
        self.set("recorded_at", moment or "")

    def is_configured(self) -> bool:
        """True when there is at least a backend URL to try."""
        return bool(self.api_base_url)
