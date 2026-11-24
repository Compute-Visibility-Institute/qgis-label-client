"""Forget what QGIS remembers about the network: "Sign out and clear network cache".

WHY THIS IS NOT PART OF PLAIN SIGN OUT

Sign out removes this plugin's credentials and nothing else. This reaches into state
that belongs to all of QGIS: the download cache holds every layer's and every plugin's
responses (basemap tiles download again afterwards), and a saved certificate exception
is a decision somebody made on purpose. Neither belongs in a routine sign-out, so it is
a separately named action for "it will not connect, start me over".

WHAT IT CLEARS

* QGIS's HTTP download cache, all of it -- what Options > Network > Cache > Clear does.
  The api marks its track and class registries ``private, max-age``, so a copy fetched
  before the sign-out can be served after it.
* Open connections. Qt keeps TLS connections alive and reuses them, and a reused
  connection never checks the certificate again, so one opened past a certificate
  warning would carry on as though nothing had been decided. A network manager belongs
  to the thread that created it and can only be cleared from there, so each worker
  thread drops its own connections before its next request from this plugin
  (:func:`drop_stale_connections`, called by ``network._prepare``). The main thread's
  manager is left alone: the plugin makes no requests on it, and clearing it would cut
  off whatever else QGIS has in flight there.
* Saved certificate exceptions ("Save SSL Server Exception") for the hosts this plugin
  talks to. A saved exception makes QGIS accept that certificate silently from then on.
  Saved behind an intercepting proxy, it sends the Google token through the proxy on
  every request without a word; removing it brings the warning back, and the warning is
  how a person sees who issued the certificate.

WHAT IT CANNOT CLEAR

Plain "Ignore" (not "Save & Ignore") on QGIS's certificate warning is remembered in
memory until QGIS restarts. QGIS keeps that list inside ``QgsAuthManager`` and does not
expose it to Python, so only a restart forgets it. The message after clearing says so.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from qgis.core import QgsApplication, QgsAuthCertUtils, QgsNetworkAccessManager

from .core import oauth
from .log import log_warning

# Bumped by every clear. A thread whose last-seen value differs drops its connections.
_generation = 0
_thread_state = threading.local()


@dataclass(frozen=True)
class Cleared:
    """What :func:`clear` did, for the message the person reads."""

    download_cache: bool
    #: ``host:port`` of each saved certificate exception removed.
    exceptions_removed: tuple[str, ...]


def hosts_for(api_base_url: str) -> set[str]:
    """The hosts this plugin itself connects to through QGIS: the api and Google's token
    endpoints. The browser sign-in page is not one of them; QGIS never opens it."""
    urls = (api_base_url, oauth.TOKEN_ENDPOINT, oauth.REVOCATION_ENDPOINT)
    return {host for host in (urlsplit(url).hostname for url in urls if url) if host}


def clear(hosts: Iterable[str]) -> Cleared:
    """Clear the download cache and saved exceptions for ``hosts``; retire connections.

    Main thread only. Connections are not closed here but on each worker thread's next
    request. Each step is independent, and the caller has already signed out, so a step
    that fails is logged and the rest still run.
    """
    global _generation
    _generation += 1

    cache_cleared = False
    try:
        manager = QgsNetworkAccessManager.instance()
        cache = manager.cache() if manager is not None else None
        if cache is not None:
            # One store behind every thread's manager (QgsNetworkDiskCache shares it under
            # a mutex), so this clears the cache for all of QGIS, as the Options button does.
            cache.clear()
        cache_cleared = True  # no cache at all means nothing stale to serve either
    except Exception as exc:  # noqa: BLE001 - the saved exceptions must still go
        log_warning(f"Could not clear QGIS's network cache: {exc}")

    try:
        removed = _remove_saved_exceptions(hosts)
    except Exception as exc:  # noqa: BLE001 - reported through the result instead
        log_warning(f"Could not remove saved certificate exceptions: {exc}")
        removed = []
    return Cleared(download_cache=cache_cleared, exceptions_removed=tuple(removed))


def drop_stale_connections() -> None:
    """Before a request: drop this thread's kept-alive connections if a clear happened.

    One attribute read when nothing has changed. Never raises: a request must not fail
    because tidying up before it did.
    """
    if getattr(_thread_state, "generation", 0) == _generation:
        return
    _thread_state.generation = _generation
    try:
        manager = QgsNetworkAccessManager.instance()
        if manager is not None:
            manager.clearConnectionCache()
    except Exception as exc:  # noqa: BLE001 - see docstring
        log_warning(f"Could not drop this thread's open connections: {exc}")


def _remove_saved_exceptions(hosts: Iterable[str]) -> list[str]:
    """Remove saved certificate exceptions for ``hosts``; return their ``host:port``.

    Removing an exception also drops its entry from QGIS's in-memory ignore list, which
    is keyed the same way.
    """
    wanted = {host.lower() for host in hosts if host}
    manager = QgsApplication.authManager()
    if not wanted or manager is None or manager.isDisabled():
        return []
    removed = []
    for config in manager.sslCertCustomConfigs():
        hostport = str(config.sslHostPort()).strip()
        if hostport.rsplit(":", 1)[0].lower() not in wanted:
            continue
        digest = QgsAuthCertUtils.shaHexForCert(config.sslCertificate())
        if manager.removeSslCertCustomConfig(digest, hostport):
            removed.append(hostport)
        else:
            log_warning(f"Could not remove the saved certificate exception for {hostport}.")
    return removed
