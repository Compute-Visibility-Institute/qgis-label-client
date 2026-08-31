"""Staying signed in for longer than an hour, which is the whole problem.

A Google ID token lives about an hour. An analyst's editing session does not. Between
those two facts sits the failure this module is about: a layer that starts returning 401
in the middle of an afternoon, made by QGIS's own OAPIF provider, which no plugin code is
in the path of and therefore cannot retry.

Three mechanisms answer it, and each is tested here for the case the other two miss:

* the **timer**, which renews five minutes early and is the one nobody notices;
* the **pre-flight check**, which runs on the way into anything that will put a credential
  on the wire, and covers the laptop that slept through the timer;
* the **401 handler**, which is a net rather than a fix -- it repairs the credential and
  then says, in words, that the layer must be reloaded. A message that stops at "signed in
  again" leaves the analyst looking at an empty layer that will stay empty.

The two failure modes worth naming: a renewal that mints new config ids breaks every open
layer once an hour, and a renewal loop that defers itself forever means the button does
nothing.
"""

from __future__ import annotations

import time

import pytest
from qgis.core import Qgis
from qgis_stubs import auth_manager

from qgis_label_client import auth
from qgis_label_client.core import oauth
from qgis_label_client.core.tracks import Track
from qgis_label_client.plugin import LabelClientPlugin

TRACK = Track(name="alpha", track_id="a-uuid", sort_order=10, is_default=True)

HOUR = 3600


def _credential(expires_at: int, refresh_token: str = "r3fr3sh") -> oauth.Credential:
    return oauth.Credential(
        id_token=f"id-token-{expires_at}",
        refresh_token=refresh_token,
        email="analyst@example.org",
        expires_at=expires_at,
    )


@pytest.fixture
def plugin(fake_iface):
    started = LabelClientPlugin(fake_iface)
    started.initGui()
    yield started
    started.unload()


def _texts(fake_iface) -> str:
    return " | ".join(text for _title, text, _level in fake_iface.messages)


def _levels(fake_iface) -> list:
    return [level for _title, _text, level in fake_iface.messages]


def _task_names(plugin) -> list[str]:
    # The stub task manager records submissions without running them, and the runner holds
    # a reference to each until it finishes -- which under the stubs is never.
    return [task.description() for task in plugin.tasks._tasks]


# --- what a completed sign-in does -------------------------------------------


def test_a_completed_sign_in_stores_the_token_the_track_and_the_expiry(plugin, fake_iface):
    """One act writes three different things to three different places.

    The ID token goes into the config map (which is the wire), the refresh token into an
    auth setting (which is never sent anywhere), and the address and expiry into settings
    (which are not secrets and have to be readable while the auth database is still
    locked, because the panel renders on startup).
    """
    plugin.tracks = [TRACK]
    credential = _credential(int(time.time()) + HOUR)

    plugin._store_credential(credential)

    stored = plugin.settings.authcfg_by_track
    assert set(stored) == {"", TRACK.name}
    assert plugin.settings.oauth_email == "analyst@example.org"
    assert plugin.settings.oauth_expires_at == credential.expires_at
    assert auth.read_refresh_token() == "r3fr3sh"
    assert "Signed in as analyst@example.org" in _texts(fake_iface)


def test_a_sign_in_that_could_not_store_a_renewal_token_says_so_immediately(
    plugin, fake_iface, monkeypatch
):
    """Otherwise the session simply dies mid-afternoon with no obvious cause.

    Silence here costs an hour before anybody finds out, and the symptom -- every layer
    401ing at once -- looks exactly like the backend going down.
    """
    monkeypatch.setattr(auth, "store_refresh_token", lambda token: False)
    plugin._store_credential(_credential(int(time.time()) + HOUR))
    assert "expire in about an hour" in _texts(fake_iface)


def test_a_cancelled_sign_in_is_not_reported_as_a_failure(plugin, fake_iface):
    # Closing the consent tab is a decision. Red for a decision trains people to ignore
    # red, and the next real failure is the one they ignore.
    plugin._on_sign_in_failed(f"{oauth.SignInCancelledError.__name__}: cancelled in the browser")
    assert "cancelled" in _texts(fake_iface).lower()
    assert Qgis.MessageLevel.Critical not in _levels(fake_iface)


# --- the pre-flight check -----------------------------------------------------


def test_an_action_is_parked_behind_a_renewal_when_the_token_is_about_to_die(plugin):
    """The half of the expiry story the timer cannot cover.

    A laptop suspended over lunch wakes with a dead token and a timer that fired late or
    not at all, so the check on the way in is the only thing between waking up and a
    canvas full of 401s.
    """
    auth.store_refresh_token("r3fr3sh")
    plugin.settings.set_oauth_session("analyst@example.org", int(time.time()) - 10)

    ran: list[str] = []
    assert plugin._defer_until_fresh(lambda: ran.append("resumed")) is True
    assert ran == []

    # The renewal landing is what releases it.
    plugin._on_refreshed(_credential(int(time.time()) + HOUR))
    assert ran == ["resumed"]


def test_a_healthy_token_parks_nothing(plugin):
    auth.store_refresh_token("r3fr3sh")
    plugin.settings.set_oauth_session("analyst@example.org", int(time.time()) + HOUR)
    assert plugin._defer_until_fresh(lambda: None) is False


def test_a_profile_with_no_google_session_is_never_held_up(plugin):
    """A hand-pasted token, or none at all, must not be nagged about.

    The plugin did not issue that credential and cannot renew it, so deferring on it would
    block every action behind a renewal that can never happen.
    """
    assert plugin.settings.oauth_expires_at == 0
    assert plugin._defer_until_fresh(lambda: None) is False


def test_a_resumed_action_cannot_defer_itself_a_second_time(plugin):
    """The loop guard, and the reason it exists.

    Deferred actions re-run themselves, and every one of them re-checks freshness on the
    way in. Without the guard a renewal that somehow did not move the expiry would park
    the action again, forever -- which presents as "the button does nothing".
    """
    auth.store_refresh_token("r3fr3sh")
    plugin.settings.set_oauth_session("analyst@example.org", int(time.time()) - 10)

    attempts: list[bool] = []

    def action() -> None:
        attempts.append(plugin._defer_until_fresh(action))

    plugin._defer_until_fresh(action)
    # A renewal that lands with an already-expired token: pathological, and exactly what
    # the guard is for.
    plugin._on_refreshed(_credential(int(time.time()) - 5))
    assert attempts == [False]


def test_a_renewal_with_no_stored_refresh_token_says_to_sign_in_again(plugin, fake_iface):
    # Distinct from a backend failure: no amount of retrying produces a refresh token that
    # was never stored, and "the server is down" sends the analyst to the wrong place.
    plugin.settings.set_oauth_session("analyst@example.org", int(time.time()) - 10)
    assert plugin._defer_until_fresh(lambda: None) is True
    assert "Sign in with Google" in _texts(fake_iface)


def test_a_locked_auth_database_is_reported_as_something_to_unlock(plugin, fake_iface):
    """The master password is a hard dependency of the renewal path.

    Rewriting an auth config needs qgis-auth.db unlocked this session. An analyst who
    dismissed the prompt gets a session that cannot renew itself, and the generic failure
    that produces reads as a backend outage rather than as a dialog they closed.
    """
    manager = auth_manager()
    manager.master_password_set = False
    manager.setMasterPassword = lambda verify=False: False
    plugin.settings.set_oauth_session("analyst@example.org", int(time.time()) - 10)

    assert plugin._defer_until_fresh(lambda: None) is True
    assert "Unlock the QGIS authentication database" in _texts(fake_iface)


# --- the renewal itself -------------------------------------------------------


def test_a_renewal_rewrites_every_config_under_its_existing_id(plugin):
    """The property every open layer and every saved project depends on.

    The authcfg id is written into the layer's data source. A renewal that minted new ids
    would break every loaded layer once an hour -- a self-inflicted outage, on a schedule.
    """
    plugin.tracks = [TRACK]
    plugin._store_credential(_credential(int(time.time()) + HOUR))
    before = plugin.settings.authcfg_by_track

    plugin._on_refreshed(_credential(int(time.time()) + 2 * HOUR))

    assert plugin.settings.authcfg_by_track == before
    manager = auth_manager()
    for authcfg in before.values():
        headers = manager.configs[authcfg].configMap()
        assert headers[auth.AUTH_HEADER].endswith(str(int(time.time()) + 2 * HOUR))
        # And QGIS's cached copy was dropped, or the provider keeps sending the old one.
        assert authcfg in manager.cleared


def test_a_renewal_after_a_401_tells_the_analyst_to_reload_the_layer(plugin, fake_iface):
    """The honest gap in this design, stated where the analyst will read it.

    QGIS's OAPIF provider made the request that failed and this plugin is not in its path,
    so a fresh credential cannot un-fail it. Stopping at "signed in again" leaves somebody
    staring at an empty layer, concluding the backend is down.
    """
    auth.store_refresh_token("r3fr3sh")
    plugin.settings.set_oauth_session("analyst@example.org", int(time.time()) + HOUR)

    plugin._fail("HTTP 401 from https://api.example.org/oapif/collections/x/items")
    plugin._on_refreshed(_credential(int(time.time()) + 2 * HOUR))

    assert "Reload the layer" in _texts(fake_iface)


def test_only_a_401_provokes_a_repair(plugin):
    # Renewing on every failure would turn a 500 or a timeout into a token rotation, and
    # a rotation that races an in-flight request is a second failure on top of the first.
    auth.store_refresh_token("r3fr3sh")
    plugin.settings.set_oauth_session("analyst@example.org", int(time.time()) + HOUR)
    plugin._fail("HTTP 500 from https://api.example.org/oapif")
    assert plugin._refreshing is False


def test_a_401_without_a_google_session_is_not_dressed_up_as_a_renewal(plugin, fake_iface):
    # With a hand-pasted token there is nothing to renew, and "renewing your sign-in"
    # would be a lie printed on top of a failure.
    plugin._fail("HTTP 401 from https://api.example.org/oapif")
    assert "renew" not in _texts(fake_iface).lower()


def test_a_dead_refresh_token_ends_the_session_rather_than_looping(plugin, fake_iface):
    """``invalid_grant`` cannot be retried, and parked actions must not wait forever.

    Google expires refresh tokens after seven days while an OAuth consent screen is still
    in testing mode, so this is a state a real deployment reaches on a schedule.
    """
    plugin.settings.set_oauth_session("analyst@example.org", int(time.time()) - 10)
    plugin._deferred.append(lambda: pytest.fail("a dead session must not resume anything"))

    plugin._on_refresh_failed(f"{oauth.SignInExpiredError.__name__}: sign in again")

    assert plugin._deferred == []
    assert plugin._refreshing is False


# --- signing out --------------------------------------------------------------


def test_signing_out_clears_the_session_and_disarms_the_renewal(plugin, fake_iface):
    """A timer still trying to renew a sign-in the analyst just ended is a bug with a UI.

    It would prompt for the master password, fail, and report an expired sign-in to
    somebody who deliberately signed out.
    """
    plugin.tracks = [TRACK]
    plugin._store_credential(_credential(int(time.time()) + HOUR))

    plugin.sign_out()

    assert plugin.settings.authcfg_by_track == {}
    assert plugin.settings.oauth_expires_at == 0
    assert plugin.settings.oauth_email == ""
    assert auth_manager().configs == {}
    assert auth.REFRESH_TOKEN_SETTING not in auth_manager().settings
    assert "Signed out" in _texts(fake_iface)


def test_signing_out_asks_google_to_revoke_the_grant(plugin):
    """Local deletion alone leaves a live grant on the analyst's Google account.

    "Signed out" then describes this machine only, which is not what the word means and
    not what the panel says.
    """
    plugin._store_credential(_credential(int(time.time()) + HOUR))
    plugin.sign_out()
    assert "Revoke Google sign-in" in _task_names(plugin)


def test_revocation_is_skipped_when_there_is_nothing_to_revoke(plugin):
    # Signing out of a profile that never signed in with Google must not post an empty
    # token to Google and must not report a failure for doing so.
    plugin.sign_out()
    assert "Revoke Google sign-in" not in _task_names(plugin)


def test_the_panel_line_names_who_is_signed_in_and_when_it_renews(plugin):
    """The two questions actually asked of that label.

    The config id and the auth method follow it for a support conversation, but nobody
    opens the panel to find out which of seven characters their credential is.
    """
    shown: list[str] = []
    assert plugin.dock is not None
    plugin.dock.set_auth_status = shown.append

    plugin._store_credential(_credential(int(time.time()) + HOUR))

    assert shown
    assert "analyst@example.org" in shown[-1]
    assert "renews in about" in shown[-1]


def test_a_renewal_refused_with_401_does_not_provoke_another_renewal(plugin):
    """The loop this design is most exposed to, closed at the one place it could start.

    Every task failure routes through the same handler, and that handler renews on a 401.
    A renewal that itself failed with a 401 would therefore start another one, forever --
    a background task and a red message bar per turn, from one expired credential.
    """
    auth.store_refresh_token("r3fr3sh")
    plugin.settings.set_oauth_session("analyst@example.org", int(time.time()) + HOUR)

    plugin._on_refresh_failed("BackendError: HTTP 401 from https://oauth2.googleapis.com/token")

    assert plugin._refreshing is False
    assert "Renew Google sign-in" not in _task_names(plugin)
