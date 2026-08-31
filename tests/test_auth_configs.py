"""What ends up in a credential, and what must never end up in one.

This is the first test module to touch :mod:`qgis_label_client.auth`, and it exists
because Google sign-in added a second secret with a very different lifetime. Under the
``APIHeader`` method **every key in a config map is emitted as an outgoing HTTP request
header**, so the map is not storage -- it is the wire. Two consequences, and both are
asserted below rather than argued in a comment:

* ``X-Track`` has to be in there. :mod:`qgis_label_client.core.recorded` records a packet
  capture showing the auth config is the only channel that reaches QGIS's own OAPIF
  provider; a layer URI's ``http-header:`` parameters send nothing. If the track stops
  riding on the credential, every provider read and every Part 4 write silently resolves
  to the deployment's default track, which is the populated-and-wrong failure
  :mod:`qgis_label_client.core.tracks` exists to prevent.
* the **refresh token** must not be. It is long-lived where the ID token lives an hour,
  and a key by that name in the map would transmit it to the backend on every single
  request.

The remaining property is the hourly rotation reusing config ids. The id is written into
every layer's data source and into any saved ``.qgz``, so a renewal that minted new ids
would break every open layer once an hour, all by itself.
"""

from __future__ import annotations

import pytest
from qgis_stubs import auth_manager

from qgis_label_client import auth
from qgis_label_client.core.errors import ConfigurationError
from qgis_label_client.core.tracks import TRACK_HEADER

# No real track name: tracks are data on the server, exactly like class ids.
TRACK = "alpha"
OTHER_TRACK = "beta"


@pytest.fixture
def manager():
    """The stub credential store, reset by the autouse fixture in conftest."""
    return auth_manager()


def _config_maps(manager) -> list[dict]:
    return [config.configMap() for config in manager.configs.values()]


# --- what the credential carries ---------------------------------------------


def test_the_credential_carries_the_bearer_token_and_the_track_header(manager):
    """The track rides on the credential, which is the only route to the provider's wire.

    Losing this does not fail: it produces a full layer of somebody else's dataset.
    """
    authcfg = auth.store_id_token("id-token-value", track=TRACK)

    stored = manager.configs[authcfg].configMap()
    assert stored[auth.AUTH_HEADER] == "Bearer id-token-value"
    assert stored[TRACK_HEADER] == TRACK
    assert manager.configs[authcfg].method() == "APIHeader"


def test_a_credential_that_names_no_track_sends_no_track_header(manager):
    """Empty is not the same as a blank header.

    An ``X-Track: `` the edge has to interpret is a decision, and a decision is where a
    wrong answer comes from. Sending nothing lands on the deployment default, which is
    exactly what the API does with any read that does not say.
    """
    authcfg = auth.store_id_token("id-token-value")
    assert TRACK_HEADER not in manager.configs[authcfg].configMap()


def test_the_refresh_token_never_enters_a_config_map(manager):
    """The security property this module exists for.

    Every key in the map becomes a request header. A refresh token there would be sent to
    the backend on every request, and unlike the ID token beside it, it does not expire.
    """
    auth.store_id_token_for_tracks("id-token-value", [TRACK, OTHER_TRACK])
    auth.store_refresh_token("the-refresh-token")

    assert manager.settings[auth.REFRESH_TOKEN_SETTING] == ("the-refresh-token", True)
    for headers in _config_maps(manager):
        assert "the-refresh-token" not in headers.values()
        assert not [key for key in headers if "refresh" in key.lower()]


def test_the_refresh_token_is_stored_encrypted(manager):
    # Unencrypted it would sit in qgis-auth.db in the clear, which is the one thing that
    # database exists to prevent.
    auth.store_refresh_token("the-refresh-token")
    _value, encrypted = manager.settings[auth.REFRESH_TOKEN_SETTING]
    assert encrypted is True


def test_reading_back_the_refresh_token_round_trips(manager):
    auth.store_refresh_token("the-refresh-token")
    assert auth.read_refresh_token() == "the-refresh-token"
    assert auth.clear_refresh_token() is True
    assert auth.read_refresh_token() == ""


def test_an_empty_refresh_token_is_not_stored(manager):
    # Google returns no refresh token on a renewal. Writing the empty string would destroy
    # the working one and end the session at its first renewal.
    assert auth.store_refresh_token("") is False
    assert auth.REFRESH_TOKEN_SETTING not in manager.settings


# --- the hourly rotation ------------------------------------------------------


def test_renewing_reuses_the_config_id_so_open_layers_keep_working(manager):
    """The id is in every layer URI and in any saved project.

    A renewal that minted a new id would break every open layer once an hour -- an outage
    the plugin would be inflicting on itself, hourly, in the name of staying signed in.
    """
    first = auth.store_id_token("hour-one", track=TRACK)
    second = auth.store_id_token("hour-two", existing_authcfg=first, track=TRACK)

    assert second == first
    assert manager.configs[first].configMap()[auth.AUTH_HEADER] == "Bearer hour-two"
    # And the track survived the rotation, which is the half that fails silently.
    assert manager.configs[first].configMap()[TRACK_HEADER] == TRACK


def test_renewing_clears_the_cached_copy_of_the_config(manager):
    """QGIS caches a decrypted config per id.

    Without the invalidation the method serves the previous hour's header from cache: a
    renewal that writes a fresh token nothing ever sends, which is indistinguishable from
    a renewal that never happened.
    """
    authcfg = auth.store_id_token("hour-one", track=TRACK)
    manager.cleared.clear()
    auth.store_id_token("hour-two", existing_authcfg=authcfg, track=TRACK)
    assert authcfg in manager.cleared


def test_the_whole_map_is_rotated_at_once(manager):
    """A rotation reaching only the current track leaves the rest 401ing.

    And a 401 on a layer belonging to another track reads as an outage, not as a rotation
    that missed -- which is why the fan-out takes the entire existing map.
    """
    first = auth.store_id_token_for_tracks("hour-one", [TRACK, OTHER_TRACK])
    assert set(first) == {"", TRACK, OTHER_TRACK}

    second = auth.store_id_token_for_tracks("hour-two", [TRACK, OTHER_TRACK], first)
    assert second == first
    for headers in _config_maps(manager):
        assert headers[auth.AUTH_HEADER] == "Bearer hour-two"


def test_a_first_sign_in_before_connect_writes_only_the_untracked_entry(manager):
    """Signing in happens before Connect: you need a credential to discover the tracks.

    So the first sign-in cannot know any track names, and the entry it writes is the one
    ``PluginSettings.authcfg_for`` falls back to.
    """
    stored = auth.store_id_token_for_tracks("id-token-value", [])
    assert list(stored) == [""]


def test_a_track_missing_from_the_list_keeps_its_credential(manager):
    """Absent from the list usually means the panel has not connected yet.

    Deleting a credential over that guess is not recoverable, and it would sign the
    analyst out of a track that still exists.
    """
    stored = auth.store_id_token_for_tracks("hour-one", [TRACK, OTHER_TRACK])
    later = auth.store_id_token_for_tracks("hour-two", [TRACK], stored)
    assert later[OTHER_TRACK] == stored[OTHER_TRACK]


# --- signing out --------------------------------------------------------------


def test_signing_out_removes_the_refresh_token_as_well_as_the_configs(manager):
    """Otherwise the strongest credential of the three survives being signed out.

    The ID token expires in an hour; the refresh token does not. Leaving it behind while
    the panel says the sign-in is gone breaks the promise this module makes in its own
    docstring, in the one direction nobody would notice.
    """
    stored = auth.store_id_token_for_tracks("id-token-value", [TRACK])
    auth.store_refresh_token("the-refresh-token")

    removed = auth.remove_all(stored)

    assert removed == len(stored)
    assert manager.configs == {}
    assert auth.REFRESH_TOKEN_SETTING not in manager.settings


# --- refusals -----------------------------------------------------------------


def test_a_locked_authentication_database_refuses_rather_than_storing_nothing(manager):
    """A silent failure here is a sign-in that reports success and stored no credential.

    The analyst then gets 401s from a panel that says they are signed in.
    """
    manager.master_password_set = False
    manager.setMasterPassword = lambda verify=False: False  # dismissed the prompt
    with pytest.raises(ConfigurationError):
        auth.store_id_token("id-token-value")


def test_an_empty_token_is_refused(manager):
    with pytest.raises(ConfigurationError):
        auth.store_id_token("   ")


def test_a_disabled_auth_system_explains_itself(manager):
    manager.disabled = True
    with pytest.raises(ConfigurationError) as caught:
        auth.store_id_token("id-token-value")
    assert "qgis-auth.db" in str(caught.value)


# --- what is safe to show -----------------------------------------------------


def test_the_panel_summary_never_touches_the_secret(manager):
    # The label is rendered on screen and pasted into support threads.
    authcfg = auth.store_id_token("id-token-value", track=TRACK)
    summary = auth.summarise(authcfg)
    assert summary is not None
    assert "id-token-value" not in summary.describe()
    assert authcfg in summary.describe()


def test_summarising_an_unknown_credential_is_none_rather_than_an_error(manager):
    # A profile naming a credential somebody deleted by hand is a normal state, and the
    # panel has to render it as "not signed in" rather than as a failure.
    assert auth.summarise("nosuchid") is None
    assert auth.summarise("") is None
