"""Settings defaults, coercion, and the two things that must never be stored."""

from __future__ import annotations

from datetime import date

import pytest

from qgis_label_client.core.asof import AsOfMechanism
from qgis_label_client.settings import DEFAULTS, PLACEHOLDER_API_URL, PluginSettings


def test_the_backend_url_default_is_empty_not_a_hostname():
    # This repository is public. A real default would be a deployment hostname in it.
    assert DEFAULTS["api_base_url"] == ""


def test_the_placeholder_uses_a_reserved_example_domain():
    # RFC 2606 reserves example.org, so the hint can never resolve to something real.
    assert "example.org" in PLACEHOLDER_API_URL


def test_no_setting_holds_a_credential():
    # Only the seven-character reference into qgis-auth.db, never the token.
    assert DEFAULTS["authcfg"] == ""
    assert not any(key for key in DEFAULTS if key in ("token", "api_key", "password", "secret"))


def test_the_custom_endpoint_paths_live_under_the_backend_namespace():
    """The two non-OAPIF endpoints sit under the backend's own `v1/` prefix.

    Everything outside that prefix is proxied straight to the feature service, so a
    path without it returns an OAPIF error about an unknown collection -- an error
    that points at the wrong component and reads like a backend outage.
    """
    assert DEFAULTS["class_registry_path"] == "v1/classes"
    assert DEFAULTS["signed_urls_path"] == "v1/imagery/signed-urls"


def test_defaults_are_returned_when_nothing_is_stored():
    settings = PluginSettings()
    assert settings.get("page_size") == 1000
    assert settings.get("restrict_to_canvas") is True


def test_values_round_trip():
    settings = PluginSettings()
    settings.set("api_base_url", "https://host/oapif")
    assert settings.api_base_url == "https://host/oapif"


@pytest.mark.parametrize(
    "stored,expected", [("true", True), ("false", False), ("1", True), (0, False)]
)
def test_booleans_survive_the_ini_round_trip(stored, expected):
    # QgsSettings goes through an INI file on some platforms and hands back strings.
    settings = PluginSettings()
    settings.set("restrict_to_canvas", stored)
    assert settings.get("restrict_to_canvas") is expected


def test_integers_survive_the_ini_round_trip():
    settings = PluginSettings()
    settings.set("page_size", "250")
    assert settings.get("page_size") == 250


def test_a_corrupt_integer_falls_back_to_the_default():
    settings = PluginSettings()
    settings.set("page_size", "not a number")
    assert settings.get("page_size") == DEFAULTS["page_size"]


def test_unknown_keys_are_rejected_at_both_ends():
    settings = PluginSettings()
    with pytest.raises(KeyError):
        settings.get("nope")
    with pytest.raises(KeyError):
        settings.set("nope", 1)


def test_as_of_is_none_when_disabled_even_if_a_date_is_stored():
    # None and "today" mean different things to the server: no datetime parameter asks
    # for the current state, datetime=today hides anything created since midnight.
    settings = PluginSettings()
    settings.set("as_of_date", "2026-04-21")
    settings.set("as_of_enabled", False)
    assert settings.as_of is None


def test_as_of_round_trips():
    settings = PluginSettings()
    settings.set_as_of(date(2026, 4, 21))
    assert settings.as_of == date(2026, 4, 21)
    settings.set_as_of(None)
    assert settings.as_of is None


def test_a_corrupt_as_of_date_is_treated_as_off():
    settings = PluginSettings()
    settings.set("as_of_enabled", True)
    settings.set("as_of_date", "not-a-date")
    assert settings.as_of is None


def test_mechanism_defaults_to_the_ogc_standard():
    assert PluginSettings().as_of_mechanism is AsOfMechanism.DATETIME


def test_is_configured_tracks_the_url_only():
    settings = PluginSettings()
    assert settings.is_configured() is False
    settings.set("api_base_url", "https://host")
    assert settings.is_configured() is True


# --- credentials, one per history track -------------------------------------


def test_a_profile_written_before_tracks_keeps_working():
    """The upgrade path, and the reason the legacy key still exists.

    Reading an old `authcfg` as "no credentials" would sign the analyst out on upgrade --
    in a plugin whose sign-in flow sets an unrecoverable master password. The old value
    means exactly "a credential that names no track", so that is where it is promoted.
    """
    settings = PluginSettings()
    settings.set("authcfg", "abc1234")
    assert settings.authcfg_by_track == {"": "abc1234"}
    assert settings.authcfg_for("anything") == "abc1234"


def test_the_promotion_is_a_read_and_does_not_rewrite_the_old_setting():
    # So downgrading the plugin leaves the profile working.
    settings = PluginSettings()
    settings.set("authcfg", "abc1234")
    settings.authcfg_by_track  # noqa: B018 - the read is the thing under test
    assert settings.get("authcfg") == "abc1234"


def test_a_track_with_its_own_credential_uses_it():
    settings = PluginSettings()
    settings.set_authcfg_by_track({"": "default1", "alpha": "alpha12"})
    assert settings.authcfg_for("alpha") == "alpha12"
    assert settings.authcfg_for("beta") == "default1"


def test_storing_a_map_clears_the_legacy_single_value():
    # Otherwise the promotion could resurrect a credential that was signed out of.
    settings = PluginSettings()
    settings.set("authcfg", "old1234")
    settings.set_authcfg_by_track({"": "new1234"})
    assert settings.get("authcfg") == ""
    assert settings.authcfg_by_track == {"": "new1234"}


def test_signing_out_leaves_nothing_behind():
    settings = PluginSettings()
    settings.set_authcfg_by_track({"": "a", "alpha": "b"})
    settings.set_authcfg_by_track({})
    assert settings.authcfg_by_track == {}
    assert settings.authcfg == ""


def test_a_corrupt_credential_map_is_treated_as_none_rather_than_raising():
    # A settings file somebody edited must not make the plugin unloadable.
    settings = PluginSettings()
    settings.set("authcfg_by_track", "{not json")
    assert settings.authcfg_by_track == {}


def test_no_track_name_is_a_default_anywhere_in_settings():
    # Tracks are data, exactly like classes. A default here would be the beginning of a
    # second copy of the deployment's vocabulary.
    assert DEFAULTS["track"] == ""
    assert PluginSettings().track == ""


def test_the_tracks_endpoint_sits_under_the_backend_namespace():
    assert DEFAULTS["tracks_path"] == "v1/tracks"


def test_the_credential_map_still_holds_no_secret():
    assert DEFAULTS["authcfg_by_track"] == ""
