"""The browser half of Google sign-in: open a tab, catch the redirect, hand back a code.

DELIBERATELY THIN, AND DELIBERATELY NOT UNIT TESTED

Everything with a decision in it -- the PKCE challenge, the ``state`` comparison, the
redirect parsing, the response page -- is in :mod:`.core.oauth`, where it is pure and
tested. What is left here is socket and window plumbing that a stub can only pretend to
do, and a test against a pretend ``QTcpServer`` would assert that the stub works. The same
treatment the rest of the QGIS-touching modules get.

WHY ``QTcpServer`` AND NOT ``http.server``

``http.server`` would need a thread, and its handler would then be delivering an
authorization code onto a background thread from which nothing may touch ``QgsAuthManager``
or a widget. ``QTcpServer`` is event-driven on the main thread, ships with the Qt this
plugin already imports, and adds no runtime dependency -- which the plugin has none of, on
purpose.

WHY LOOPBACK AT ALL

Google's desktop-client flow has no other option that keeps the authorization code off
somebody else's server. The listener binds 127.0.0.1 only, on an ephemeral port, for the
length of one sign-in; :func:`.core.oauth.parse_callback` compares ``state`` because
anything else on the machine can also connect to that port.
"""

from __future__ import annotations

from typing import Any

from qgis.PyQt.QtCore import QObject, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtNetwork import QHostAddress, QTcpServer

from .core import oauth
from .core.errors import ConfigurationError
from .log import log, log_warning


class GoogleSignIn(QObject):
    """One sign-in attempt: start it, then wait for exactly one of the two signals.

    The verifier and the ``state`` are generated here and never leave the object until
    :attr:`codeReceived` carries the verifier out to be exchanged. Nothing is stored, so an
    abandoned attempt leaves no trace beyond a closed socket.
    """

    #: ``(code, verifier, redirect_uri)``. All three are needed for the exchange, and the
    #: exchange is the plugin's job rather than this object's: it is a network request, it
    #: belongs on a worker thread, and this class must not grow a second responsibility.
    codeReceived = pyqtSignal(str, str, str)
    failed = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._server: Any = None
        self._timeout: Any = None
        self._verifier = ""
        self._state = ""
        self._redirect = ""
        self._buffer = b""
        self._done = False

    # --- start -----------------------------------------------------------------

    def start(self, login_hint: str = "") -> None:
        """Bind the loopback listener and open the browser. Main thread only."""
        self._verifier = oauth.new_verifier()
        self._state = oauth.new_state()

        self._server = QTcpServer(self)
        # Port 0 asks the OS for a free one. QHostAddress of the loopback literal rather
        # than QHostAddress.Any: binding every interface would put an authorization-code
        # endpoint on the network for two minutes.
        if not self._server.listen(QHostAddress(oauth.LOOPBACK_HOST), 0):
            self._server = None
            raise ConfigurationError(
                "Could not open a local port to receive the Google sign-in reply. A "
                "firewall or security product blocking loopback connections is the usual "
                "cause."
            )
        self._server.newConnection.connect(self._accept)
        self._redirect = oauth.redirect_uri(self._server.serverPort())

        # An abandoned sign-in must not leave the listener open for the rest of the
        # session: it is a port on this machine that accepts authorization codes.
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        self._timeout.start(oauth.CALLBACK_TIMEOUT_SECONDS * 1000)

        url = oauth.authorization_url(
            redirect=self._redirect,
            state=self._state,
            verifier=self._verifier,
            login_hint=login_hint,
        )
        log(f"Opening the system browser for Google sign-in; reply expected on {self._redirect}")
        if not QDesktopServices.openUrl(QUrl(url)):
            self._fail(
                "QGIS could not open a web browser for the Google sign-in. Sign-in needs "
                "a browser on this machine."
            )

    def abort(self) -> None:
        """Give up quietly. Called from ``unload()`` and after a completed attempt."""
        self._done = True
        if self._timeout is not None:
            self._timeout.stop()
            self._timeout = None
        if self._server is not None:
            self._server.close()
            self._server = None

    # --- the redirect ----------------------------------------------------------

    def _accept(self) -> None:
        if self._server is None:
            return
        socket = self._server.nextPendingConnection()
        if socket is None:
            return
        socket.readyRead.connect(lambda: self._read(socket))
        socket.disconnected.connect(socket.deleteLater)

    def _read(self, socket: Any) -> None:
        # One request line is all this speaks. Reading in a loop because a TCP segment
        # boundary can land mid-line, and a truncated line parses as "no code" -- which
        # would report a working sign-in as a failure.
        self._buffer += bytes(socket.readAll())
        if b"\r\n" not in self._buffer and len(self._buffer) < 8192:
            return

        line = self._buffer.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
        target = oauth.request_target(line)
        if not target:
            # Not the browser. Port scanners and other software do connect to loopback
            # ports; answering one as though it were the redirect would compare `state`
            # against nothing.
            self._buffer = b""
            socket.disconnectFromHost()
            return

        try:
            code = oauth.parse_callback(target, self._state)
        except oauth.SignInError as exc:
            self._respond(socket, f"Sign-in failed: {exc}")
            self._fail(str(exc))
            return

        self._respond(socket, "Signed in. QGIS is finishing up.")
        if self._done:
            return
        self._done = True
        verifier, redirect = self._verifier, self._redirect
        self.abort()
        self.codeReceived.emit(code, verifier, redirect)

    def _respond(self, socket: Any, message: str) -> None:
        try:
            socket.write(oauth.callback_page(message))
            # flush before disconnecting: closing the socket with the reply still buffered
            # leaves the person looking at a browser error after a sign-in that worked.
            socket.flush()
            socket.disconnectFromHost()
        except Exception as exc:  # noqa: BLE001 - a courtesy page must never break sign-in
            log_warning(f"Could not write the sign-in reply page: {exc}")

    def _on_timeout(self) -> None:
        self._fail(
            f"No reply from the browser within {oauth.CALLBACK_TIMEOUT_SECONDS} seconds, so "
            "the sign-in was abandoned. Nothing was changed."
        )

    def _fail(self, message: str) -> None:
        if self._done:
            return
        self._done = True
        self.abort()
        self.failed.emit(message)
