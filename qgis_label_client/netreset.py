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
* Saved certificate exceptions ("Save SSL Server Exception") for the servers this
  plugin talks to, matched on ``host:port`` exactly as QGIS keys them, so an exception for
  another service on the same host stays. A saved exception makes QGIS accept that
  certificate silently from then on. Saved behind an intercepting proxy, it sends the
  Google token through the proxy on every request without a word; removing it brings the
  warning back, and the warning is how a person sees who issued the certificate.

WHAT IT CANNOT CLEAR

Plain "Ignore" (not "Save & Ignore") on QGIS's certificate warning is remembered in
memory until QGIS restarts. QGIS keeps that list inside ``QgsAuthManager`` and does not
expose it to Python, so only a restart forgets it. The message after clearing says so.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from qgis.core import QgsApplication, QgsAuthCertUtils, QgsNetworkAccessManager

from .core import oauth
from .log import log_warning

# Bumped by every clear. A thread whose manager has seen a different value drops its
# connections before its next request.
_generation = 0
#: The dynamic property on each thread's network manager that records the generation that
#: thread has caught up with. See drop_stale_connections for why not threading.local.
_SEEN_PROPERTY = "cviLabelClientNetresetGeneration"
#: QGIS keys saved exceptions on ``host:port`` and uses 443 when a URL names no port.
_DEFAULT_PORT = 443


@dataclass(frozen=True)
class Cleared:
    """What :func:`clear` did, for the message the person reads."""

    download_cache: bool
    #: ``host:port`` of each saved certificate exception removed.
    exceptions_removed: tuple[str, ...]
    #: ``host:port`` of each saved exception for these servers that could not be removed.
    exceptions_failed: tuple[str, ...] = ()
    #: Why the saved exceptions could not all be checked, or "" if they could.
    exceptions_error: str = ""

    @property
    def complete(self) -> bool:
        """Every step did what it says. Anything else is a warning, never a quiet "done"."""
        return self.download_cache and not self.exceptions_failed and not self.exceptions_error


def hosts_for(*api_base_urls: str) -> set[str]:
    """``host:port`` of each server this plugin itself connects to through QGIS: the api
    and Google's token endpoints. The browser sign-in page is not one of them; QGIS never
    opens it.

    The caller passes both the saved api URL and the one in the panel field: the field is
    where the next Connect goes, the saved one is where the last one went. A URL that does
    not parse names no server QGIS could have connected to, and is skipped rather than
    allowed to stop the sign-out it is part of.
    """
    found = set()
    for url in (*api_base_urls, oauth.TOKEN_ENDPOINT, oauth.REVOCATION_ENDPOINT):
        if not url:
            continue
        try:
            parts = urlsplit(url.strip())
            host, port = parts.hostname, parts.port
        except ValueError:  # "https://[bad", "https://host:port", ...
            continue
        if host:
            found.add(f"{host}:{port or _DEFAULT_PORT}")
    return found


def clear(hostports: Iterable[str]) -> Cleared:
    """Clear the download cache and saved exceptions for ``hostports``; retire connections.

    Main thread only. Connections are not closed here but on each worker thread's next
    request. Each step is independent, and the caller has already signed out, so a step
    that fails is logged, reported in the result, and the rest still run.

    The generation moves LAST. A worker request that starts while the cache is being
    deleted is set up with the saved exception still in place; if the generation had
    already moved, that request would count as fresh and its connection would be kept.
    """
    global _generation

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

    removed, failed, error = _remove_saved_exceptions(hostports)
    _generation += 1
    return Cleared(
        download_cache=cache_cleared,
        exceptions_removed=tuple(removed),
        exceptions_failed=tuple(failed),
        exceptions_error=error,
    )


def drop_stale_connections() -> None:
    """Before a request: drop this thread's kept-alive connections if a clear happened.

    The generation a thread has caught up with is kept on that thread's own network
    manager, not in ``threading.local``. QgsTask work runs on QThreadPool threads that
    Python did not start, and the Python thread state there -- with everything
    ``threading.local`` holds -- is created and destroyed around every task. The value
    would be forgotten between tasks, and every task after a clear would drop its
    connections again until QGIS restarted. The manager lives as long as its thread.

    Free until the first clear, one property read after it. Never raises: a request must
    not fail because tidying up before it did.
    """
    generation = _generation
    if not generation:
        return
    try:
        manager = QgsNetworkAccessManager.instance()
        if manager is None or manager.property(_SEEN_PROPERTY) == generation:
            return
        # Recorded first, so a manager that cannot clear warns once rather than per request.
        manager.setProperty(_SEEN_PROPERTY, generation)
        manager.clearConnectionCache()
    except Exception as exc:  # noqa: BLE001 - see docstring
        log_warning(f"Could not drop this thread's open connections: {exc}")


def _remove_saved_exceptions(hostports: Iterable[str]) -> tuple[list[str], list[str], str]:
    """Remove saved certificate exceptions for ``hostports``.

    Returns the ``host:port`` of each one removed, of each one that could not be, and why
    the list could not be fully checked ("" if it could). A failure is never folded into
    "none found": the person would read that as the exception being gone.

    Removing an exception also drops its entry from QGIS's in-memory ignore list, which
    is keyed the same way.
    """
    wanted = {hostport.lower() for hostport in hostports if hostport}
    if not wanted:
        return [], [], ""
    try:
        manager = QgsApplication.authManager()
        if manager is None or manager.isDisabled():
            return [], [], "QGIS's authentication system is not available"
        configs = list(manager.sslCertCustomConfigs())
    except Exception as exc:  # noqa: BLE001 - reported through the result
        log_warning(f"Could not read QGIS's saved certificate exceptions: {exc}")
        return [], [], f"could not read them: {exc}"

    removed: list[str] = []
    failed: list[str] = []
    unreadable = 0
    for config in configs:
        try:
            hostport = str(config.sslHostPort()).strip()
        except Exception as exc:  # noqa: BLE001 - might be one of ours; say so
            log_warning(f"Could not read a saved certificate exception: {exc}")
            unreadable += 1
            continue
        if hostport.lower() not in wanted:
            continue
        try:
            digest = QgsAuthCertUtils.shaHexForCert(config.sslCertificate())
            done = bool(manager.removeSslCertCustomConfig(digest, hostport))
            reason = "QGIS did not remove it"
        except Exception as exc:  # noqa: BLE001 - the next exception must still go
            done, reason = False, str(exc)
        if done:
            removed.append(hostport)
        else:
            log_warning(f"Could not remove the saved exception for {hostport}: {reason}")
            failed.append(hostport)
    error = f"{unreadable} saved exception(s) could not be read" if unreadable else ""
    return removed, failed, error
