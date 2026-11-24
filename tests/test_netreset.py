"""The "Sign out and clear network cache" button: what it clears, what it leaves alone.

The properties worth pinning down are the ones a quick reading gets wrong:

* only this plugin's servers lose their saved certificate exceptions, matched on
  ``host:port``, because every other exception in QGIS is somebody else's decision;
* connections are retired on each worker thread's own next request, once, rather than
  cleared from the main thread, where the manager belongs to QGIS and is in use. "Once"
  is remembered by the thread's manager, because QGIS's task threads lose Python
  thread-local state between tasks;
* a step that fails is reported as a failure, never as "nothing to remove";
* the Google revocation goes out before the exception for Google's endpoint is removed;
* plain Sign out does none of this.
"""

from __future__ import annotations

import threading
import time

import pytest
from qgis.core import Qgis
from qgis_stubs import auth_manager

from qgis_label_client import netreset
from qgis_label_client import plugin as plugin_module
from qgis_label_client.core import oauth
from qgis_label_client.plugin import LabelClientPlugin


class _Manager:
    """One thread's QgsNetworkAccessManager, and the cache they all share."""

    def __init__(self) -> None:
        self.connection_clears = 0
        self.cache_clears = 0
        self.properties: dict[str, object] = {}
        #: netreset._generation at the moment the cache was cleared.
        self.generation_during_clear: list[int] = []

    def clearConnectionCache(self) -> None:  # noqa: N802 - Qt naming
        self.connection_clears += 1

    def property(self, name: str):
        return self.properties.get(name)

    def setProperty(self, name: str, value) -> None:  # noqa: N802 - Qt naming
        self.properties[name] = value

    def cache(self):
        return self

    def clear(self) -> None:
        self.generation_during_clear.append(netreset._generation)
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
    monkeypatch.setattr(netreset, "_generation", 0)
    return by_thread


class _SavedExceptions:
    """Saved exceptions in the stub auth manager, and what happened to each."""

    def __init__(self, monkeypatch) -> None:
        self.configs = [
            _Config("api.example.org:443"),
            _Config("api.example.org:8443"),
            _Config("api-dev.example.org:443"),
            _Config("oauth2.googleapis.com:443"),
            _Config("tiles.example.net:443"),
        ]
        #: (digest, host:port) of each one removed.
        self.removed: list[tuple[str, str]] = []
        #: host:port QGIS refuses to remove, and host:port whose removal raises.
        self.refused: set[str] = set()
        self.raising: set[str] = set()
        #: netreset._generation at the moment each removal ran.
        self.generation_during_removal: list[int] = []
        manager = auth_manager()
        monkeypatch.setattr(manager, "sslCertCustomConfigs", lambda: self.configs, raising=False)
        monkeypatch.setattr(manager, "removeSslCertCustomConfig", self._remove, raising=False)
        monkeypatch.setattr(netreset, "QgsAuthCertUtils", _CertUtils)

    def _remove(self, digest, hostport) -> bool:
        self.generation_during_removal.append(netreset._generation)
        if hostport in self.raising:
            raise RuntimeError("database is locked")
        if hostport in self.refused:
            return False
        self.removed.append((digest, hostport))
        return True

    @property
    def removed_hostports(self) -> list[str]:
        return [hostport for _digest, hostport in self.removed]


@pytest.fixture
def saved_exceptions(monkeypatch) -> _SavedExceptions:
    return _SavedExceptions(monkeypatch)


def test_servers_are_the_api_and_googles_token_endpoint_with_their_ports():
    assert netreset.hosts_for("https://api.example.org/v1/") == {
        "api.example.org:443",
        "oauth2.googleapis.com:443",
    }
    assert netreset.hosts_for("") == {"oauth2.googleapis.com:443"}
    assert netreset.hosts_for("https://API.example.org:8443", "https://api-dev.example.org") == {
        "api.example.org:8443",
        "api-dev.example.org:443",
        "oauth2.googleapis.com:443",
    }


def test_a_malformed_url_names_no_server_instead_of_raising():
    # Typed into the field when Connect fails -- exactly when this button gets clicked.
    for bad in ("https://[api.example.org", "https://[bad]/", "https://api.example.org:port"):
        assert netreset.hosts_for(bad) == {"oauth2.googleapis.com:443"}


def test_only_this_plugins_servers_lose_their_saved_exceptions(managers, saved_exceptions):
    result = netreset.clear(netreset.hosts_for("https://api.example.org"))

    assert result.exceptions_removed == ("api.example.org:443", "oauth2.googleapis.com:443")
    assert result.complete
    # Removed by the digest QGIS keys them on, not by host alone; another service on the
    # api's host (port 8443) keeps its exception.
    assert saved_exceptions.removed == [
        ("sha-cert-of-api.example.org:443", "api.example.org:443"),
        ("sha-cert-of-oauth2.googleapis.com:443", "oauth2.googleapis.com:443"),
    ]


def test_an_exception_that_could_not_be_removed_is_reported_not_dropped(managers, saved_exceptions):
    saved_exceptions.refused.add("api.example.org:443")
    saved_exceptions.raising.add("api-dev.example.org:443")

    result = netreset.clear(
        netreset.hosts_for("https://api.example.org", "https://api-dev.example.org")
    )

    # A raise partway through the list does not stop the exceptions after it going.
    assert result.exceptions_removed == ("oauth2.googleapis.com:443",)
    assert result.exceptions_failed == ("api.example.org:443", "api-dev.example.org:443")
    assert not result.complete


@pytest.mark.parametrize("broken", ["disabled", "unreadable"])
def test_exceptions_that_could_not_be_checked_are_reported(
    managers, saved_exceptions, monkeypatch, broken
):
    manager = auth_manager()
    if broken == "disabled":
        monkeypatch.setattr(manager, "disabled", True)
    else:

        def unreadable():
            raise RuntimeError("database is locked")

        monkeypatch.setattr(manager, "sslCertCustomConfigs", unreadable, raising=False)

    result = netreset.clear(netreset.hosts_for("https://api.example.org"))

    assert result.exceptions_removed == ()
    assert result.exceptions_error
    assert not result.complete
    assert result.download_cache  # the other step still ran


def test_the_generation_moves_only_after_everything_is_cleared(managers, saved_exceptions):
    """A request that starts mid-clear must not count as fresh: it was set up with the
    exception still saved, and its connection has to go at that thread's next request."""
    netreset.clear(netreset.hosts_for("https://api.example.org"))

    here = managers[threading.get_ident()]
    assert here.generation_during_clear == [0]
    assert set(saved_exceptions.generation_during_removal) == {0}
    assert netreset._generation == 1


def test_cache_clears_now_and_each_thread_drops_its_connections_once(managers):
    here = threading.get_ident()
    netreset.drop_stale_connections()  # before any clear: nothing to do, no manager asked
    assert managers == {}

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


def test_once_per_thread_is_remembered_by_the_manager_not_python_thread_state(monkeypatch):
    """QgsTask work runs on QThreadPool threads Python did not start, and their Python
    thread state (and every threading.local) is created afresh for each task. Two tasks on
    the same pool thread are two Python thread states sharing one network manager."""
    shared = _Manager()

    class _Nam:
        @staticmethod
        def instance() -> _Manager:
            return shared

    monkeypatch.setattr(netreset, "QgsNetworkAccessManager", _Nam)
    monkeypatch.setattr(netreset, "_generation", 1)

    for _task in range(3):
        thread = threading.Thread(target=netreset.drop_stale_connections)
        thread.start()
        thread.join()

    assert shared.connection_clears == 1


@pytest.fixture
def plugin(fake_iface):
    started = LabelClientPlugin(fake_iface)
    started.initGui()
    started.settings.set("api_base_url", "https://api.example.org")
    started.dock.api_url = lambda: started.settings.api_base_url
    yield started
    started.unload()


@pytest.fixture
def timers(monkeypatch) -> list:
    """QTimer.singleShot callbacks, held until the test runs them."""
    pending: list = []
    monkeypatch.setattr(
        plugin_module.QTimer, "singleShot", lambda delay, callback: pending.append(callback)
    )
    return pending


def _sign_in(plugin) -> None:
    plugin._store_credential(
        oauth.Credential(
            id_token="id-token",
            refresh_token="r3fr3sh",
            email="analyst@example.org",
            expires_at=int(time.time()) + 3600,
        )
    )


def _revoke_task(plugin):
    (task,) = [t for t in plugin.tasks._tasks if t.description() == "Revoke Google sign-in"]
    return task


def test_the_panel_button_signs_out_then_clears_and_says_so(
    plugin, fake_iface, managers, saved_exceptions, timers
):
    plugin.dock.signOutClearNetworkRequested.emit()

    signed_out = fake_iface.messages[-1][1]
    assert signed_out.startswith("Signed out.")
    assert "Clearing QGIS's network cache next" in signed_out
    # Not yet: the cache is deleted once that message is on screen.
    assert managers == {}

    (clear,) = timers
    clear()

    _title, message, level = fake_iface.messages[-1]
    assert "Cleared QGIS's download cache" in message
    assert "api.example.org:443" in message
    assert "restart QGIS" in message
    assert level == Qgis.MessageLevel.Info
    assert managers[threading.get_ident()].cache_clears == 1


def test_googles_exception_goes_only_after_the_revocation_has(
    plugin, fake_iface, managers, saved_exceptions, timers
):
    """Removed first, it would put a certificate warning in front of the revoke request;
    rejecting it leaves the grant live while the panel says "Signed out"."""
    _sign_in(plugin)

    plugin.sign_out_and_clear_network()

    assert saved_exceptions.removed == []
    assert timers == []
    _revoke_task(plugin).finished(False)  # however it ends, the clear follows
    assert "oauth2.googleapis.com:443" in saved_exceptions.removed_hostports
    assert "Cleared QGIS's download cache" in fake_iface.messages[-1][1]


def test_the_url_in_the_field_counts_even_before_it_is_saved(
    plugin, fake_iface, managers, saved_exceptions, timers
):
    plugin.dock.api_url = lambda: "https://api-dev.example.org"

    plugin.sign_out_and_clear_network()
    timers[0]()

    assert saved_exceptions.removed_hostports == [
        "api.example.org:443",
        "api-dev.example.org:443",
        "oauth2.googleapis.com:443",
    ]


def test_a_malformed_url_still_signs_out_and_revokes(
    plugin, fake_iface, managers, saved_exceptions, timers
):
    _sign_in(plugin)
    plugin.dock.api_url = lambda: "https://[api.example.org"

    plugin.sign_out_and_clear_network()

    assert fake_iface.messages[-1][1].startswith("Signed out.")
    _revoke_task(plugin).finished(False)
    assert "Cleared QGIS's download cache" in fake_iface.messages[-1][1]


def test_a_step_that_failed_is_a_warning(plugin, fake_iface, managers, saved_exceptions, timers):
    saved_exceptions.refused.add("api.example.org:443")

    plugin.sign_out_and_clear_network()
    timers[0]()

    _title, message, level = fake_iface.messages[-1]
    assert level == Qgis.MessageLevel.Warning
    assert "Could not remove the saved certificate exceptions for api.example.org:443" in message


def test_plain_sign_out_leaves_the_network_alone(
    plugin, fake_iface, managers, saved_exceptions, timers
):
    _sign_in(plugin)

    plugin.sign_out()
    _revoke_task(plugin).finished(False)

    assert saved_exceptions.removed == []
    assert timers == []
    assert managers == {}
    assert "network cache" not in " ".join(text for _t, text, _l in fake_iface.messages)
