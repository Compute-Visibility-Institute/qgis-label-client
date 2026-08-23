"""Logging into the QGIS Log Messages panel, with credential redaction built in.

A plugin that fails to import fails *silently* in the plugin manager, and an exception
escaping ``QgsTask.run()`` is swallowed with no traceback at all. The Log Messages panel
is where both become visible, so every diagnostic in this plugin goes through here rather
than through ``print``.

:func:`log_url` exists because the most useful thing to log while debugging imagery is a
URL, and a signed GCS URL is a credential for licensed Maxar data. Redaction is not left
to the call site.
"""

from __future__ import annotations

from qgis.core import Qgis, QgsMessageLog

from .core.assets import redact

#: Tag for the Log Messages panel. Matches the plugin name so it is findable.
LOG_TAG = "CVI Label Client"


def log(message: str, level: Qgis.MessageLevel = Qgis.MessageLevel.Info) -> None:
    """Write one line to the plugin's tab in the Log Messages panel."""
    QgsMessageLog.logMessage(message, LOG_TAG, level)


def log_warning(message: str) -> None:
    log(message, Qgis.MessageLevel.Warning)


def log_error(message: str) -> None:
    log(message, Qgis.MessageLevel.Critical)


def log_url(prefix: str, url: str, level: Qgis.MessageLevel = Qgis.MessageLevel.Info) -> None:
    """Log a URL with any query string removed.

    Always use this for imagery URLs. The signature in the query string is the access
    grant, and the Log Messages panel is copied into bug reports.
    """
    log(f"{prefix}: {redact(url)}", level)
