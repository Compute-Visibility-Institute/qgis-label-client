"""The "Sign out and clear network cache" button: what it clears, what it leaves alone.

The properties worth pinning down are the ones a quick reading gets wrong:

* only the hosts this plugin talks to lose their saved certificate exceptions, because
  every other exception in QGIS is somebody else's decision;
* connections are retired on each worker thread's own next request, once, rather than
  cleared from the main thread, where the manager belongs to QGIS and is in use;
* plain Sign out does none of this.
"""

from __future__ import annotations

import threading

import pytest
from qgis_stubs import auth_manager

from qgis_label_client import netreset
from qgis_label_client.plugin import LabelClientPlugin


class _Manager:
    """One thread's QgsNetworkAccessManager, and the cache they all share."""

    def __init__(self) -> None:
        self.connection_clears = 0
        self.cache_clears = 0

    def clearConnectionCache(self) -> None:  # noqa: N802 - Qt naming
        self.connection_clears += 1

    def cache(self):
        return self

    def clear(self) -> None:
        self.cache_clears += 1


class _Config:
    def __init__(self, hostport: str) -> None:
        self._hostport = hostport

    def sslHostPort(self) -> str:  # noqa: N802 - QGIS naming
        return self._hostport

    def sslCertificate(self) -> str:  # noqa: N802 - QGIS naming
        return f"cert-of-{self._hostport}"


class _CertUtils:
    @staticmethod
    def shaHexForCert(cert) -> str:  # noqa: N802 - QGIS naming
        return f"sha-{cert}"


@pytest.fixture
def managers(monkeypatch) -> dict[int, _Manager]:
    by_thread: dict[int, _Manager] = {}

    class _Nam:
        @staticmethod
        def instance() -> _Manager:
            return by_thread.setdefault(threading.get_ident(), _Manager())

    monkeypatch.setattr(netreset, "QgsNetworkAccessManager", _Nam)
    return by_thread


@pytest.fixture
def saved_exceptions(monkeypatch) -> list[tuple[str, str]]:
    """Three saved exceptions; returns the (digest, host:port) of each one removed."""
    configs = [
        _Config("api.example.org:443"),
        _Config("oauth2.googleapis.com:443"),
        _Config("tiles.example.net:443"),
    ]
    removed: list[tuple[str, str]] = []

    def remove(digest, hostport):
        removed.append((digest, hostport))
        return True

    manager = auth_manager()
    monkeypatch.setattr(manager, "sslCertCustomConfigs", lambda: configs, raising=False)
    monkeypatch.setattr(manager, "removeSslCertCustomConfig", remove, raising=False)
    monkeypatch.setattr(netreset, "QgsAuthCertUtils", _CertUtils)
    return removed


def test_hosts_are_the_api_and_googles_token_endpoint():
    assert netreset.hosts_for("https://api.example.org/v1/") == {
        "api.example.org",
        "oauth2.googleapis.com",
    }
    assert netreset.hosts_for("") == {"oauth2.googleapis.com"}


def test_only_this_plugins_hosts_lose_their_saved_exceptions(managers, saved_exceptions):
    result = netreset.clear(netreset.hosts_for("https://api.example.org"))

    assert result.exceptions_removed == ("api.example.org:443", "oauth2.googleapis.com:443")
    # Removed by the digest QGIS keys them on, not by host alone.
    assert saved_exceptions == [
        ("sha-cert-of-api.example.org:443", "api.example.org:443"),
        ("sha-cert-of-oauth2.googleapis.com:443", "oauth2.googleapis.com:443"),
    ]


def test_cache_clears_now_and_each_thread_drops_its_connections_once(managers):
    here = threading.get_ident()
    result = netreset.clear([])

    assert result.download_cache
    assert managers[here].cache_clears == 1
    # Nothing is closed on the calling thread's manager by clear() itself.
    assert managers[here].connection_clears == 0

    netreset.drop_stale_connections()
    netreset.drop_stale_connections()
    assert managers[here].connection_clears == 1

    def worker() -> None:
        netreset.drop_stale_connections()
        netreset.drop_stale_connections()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    others = [m.connection_clears for ident, m in managers.items() if ident != here]
    assert others == [1]


@pytest.fixture
def plugin(fake_iface):
    started = LabelClientPlugin(fake_iface)
    started.initGui()
    started.settings.set("api_base_url", "https://api.example.org")
    yield started
    started.unload()


def test_the_panel_button_signs_out_clears_and_says_so(
    plugin, fake_iface, managers, saved_exceptions
):
    plugin.dock.signOutClearNetworkRequested.emit()

    message = fake_iface.messages[-1][1]
    assert message.startswith("Signed out.")
    assert "Cleared QGIS's download cache" in message
    assert "api.example.org:443" in message
    assert "restart QGIS" in message
    assert managers[threading.get_ident()].cache_clears == 1


def test_plain_sign_out_leaves_the_network_alone(plugin, fake_iface, managers, saved_exceptions):
    plugin.sign_out()

    assert saved_exceptions == []
    assert managers == {}
    assert "download cache" not in fake_iface.messages[-1][1]
