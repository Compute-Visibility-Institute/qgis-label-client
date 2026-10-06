"""Trace information a labeler can copy from an error and send to the maintainers.

An error message tells a person what went wrong. A maintainer also needs where, on which
versions, and what happened just before. The trace is built at the moment of the error,
so later activity cannot push out the lines that explain it, and it leaves QGIS only
through the user's own click on "Copy trace information": nothing is sent anywhere.

Each request carries an ``X-Request-ID`` that the API edge logs with its own line for the
request, so an ID in a trace finds the server side of the same failure.

Credentials never enter the trace. Request URLs lose their query strings, plugin log
lines were redacted when written, and the account is named but no token is.
"""

from __future__ import annotations

import platform
import sys
import threading
import traceback
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone

from qgis.core import Qgis
from qgis.gui import QgsMessageViewer
from qgis.PyQt.QtCore import QT_VERSION_STR
from qgis.PyQt.QtWidgets import QApplication, QMessageBox, QPushButton

from . import __version__
from .core.assets import redact
from .log import LOG_TAG

COPY_LABEL = "Copy trace information"
COPY_TIP = "Copy technical details of this error to send to the CVI team"

# Requests are recorded on worker threads; everything is read on the main thread.
_LOCK = threading.Lock()
_LOG: deque[str] = deque(maxlen=80)
_REQUESTS: deque[str] = deque(maxlen=25)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def record_log(message: str, tag: str, level: Qgis.MessageLevel) -> None:
    """``QgsMessageLog.messageReceived`` slot: this plugin's lines, and others' problems.

    QGIS logs its own commit errors (a refused Save Layer Edits) under another tag, and
    those are often the most useful lines in a trace, so warnings from any tag are kept.
    """
    if tag != LOG_TAG and level not in (Qgis.MessageLevel.Warning, Qgis.MessageLevel.Critical):
        return
    name = {Qgis.MessageLevel.Warning: "WARNING", Qgis.MessageLevel.Critical: "ERROR"}.get(
        level, "INFO"
    )
    with _LOCK:
        _LOG.append(f"{_now()} {name} [{tag or 'General'}] {message}")


def record_request(method: str, url: str, status: int, request_id: str, error: str = "") -> None:
    """One HTTP exchange. Called from worker threads; the URL loses its query string."""
    outcome = str(status) if status else "no response"
    line = f"{_now()} {method} {redact(url)} -> {outcome} request-id={request_id or '-'}"
    with _LOCK:
        _REQUESTS.append(line + (f" ({error})" if error else ""))


def build_trace(message: str, details: str = "", context: tuple = (), *, shown: bool = True) -> str:
    """Plain text describing one error, captured now.

    Call it from the code that reports the error: inside an ``except`` block the
    exception's traceback is included without the caller passing it. A ``shown`` error
    also joins the recent log, so a later trace still says what came before it.
    """
    now = _now()
    failure = traceback.format_exc().rstrip() if sys.exc_info()[1] is not None else ""
    lines = [
        "CVI Label Client trace",
        f"Time: {now}",
        f"Plugin {__version__} · QGIS {Qgis.version()} · Qt {QT_VERSION_STR} · "
        f"Python {platform.python_version()} · {platform.platform()}",
        *(f"{label}: {value}" for label, value in context),
        "",
        "Message:",
        message,
    ]
    if details and details != message:
        lines += ["", "Details:", details]
    if failure:
        lines += ["", "Exception:", failure]
    with _LOCK:
        requests, log_lines = list(_REQUESTS), list(_LOG)
        if shown:
            _LOG.append(f"{now} SHOWN {message}" + (f"\n{failure}" if failure else ""))
    if requests:
        lines += ["", "Recent requests (newest last):", *requests]
    if log_lines:
        lines += ["", "Recent log (newest last):", *log_lines]
    return "\n".join(lines)


def trace_for(message: str, details: str = "", context_of=tuple) -> str:
    """:func:`build_trace` that cannot fail: a broken trace must never hide the error."""
    try:
        return build_trace(message, details, context_of())
    except Exception as exc:  # noqa: BLE001
        return f"{message}\n\n{details}\n\nTrace unavailable: {exc!r}"


def copy_to_clipboard(trace: str) -> None:
    QApplication.clipboard().setText(trace)


def copy_button(trace: str, parent=None) -> QPushButton:
    """A message-bar button that puts this error's trace on the clipboard."""
    button = QPushButton(COPY_LABEL, parent)
    button.setToolTip(COPY_TIP)

    def copy(_checked=False):
        copy_to_clipboard(trace)
        button.setText("Copied")

    button.clicked.connect(copy)
    return button


def details_button(title: str, text: str, details: str, parent=None) -> QPushButton:
    """The message bar's own "Show more", which a bar item with extra buttons loses."""
    button = QPushButton("Show more", parent)

    def show(_checked=False):
        viewer = QgsMessageViewer(parent)
        viewer.setTitle(title)
        viewer.setMessageAsPlainText(f"{text}\n\n{details}")
        viewer.showMessage(True)

    button.clicked.connect(show)
    return button


def add_copy_button(box: QMessageBox, trace_of) -> QPushButton:
    """Give a message box a copy button that leaves the box open.

    ``trace_of`` is called on each click, because one box may be reused for later
    errors. QMessageBox closes on every button it owns; dropping its own connection to
    this one keeps the box open, so the user can still read what they are sending.
    """
    button = box.addButton(COPY_LABEL, QMessageBox.ButtonRole.ActionRole)
    button.setToolTip(COPY_TIP)
    with suppress(TypeError, RuntimeError):
        button.clicked.disconnect()

    def copy(_checked=False):
        copy_to_clipboard(trace_of())
        button.setText("Copied")

    button.clicked.connect(copy)
    return button


def reset() -> None:
    """Forget captured lines. For tests."""
    with _LOCK:
        _LOG.clear()
        _REQUESTS.clear()
