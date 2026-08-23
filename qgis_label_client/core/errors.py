"""Error types shared by the pure core and the QGIS-facing layer."""

from __future__ import annotations


class LabelClientError(Exception):
    """Base class for every error this plugin raises deliberately.

    Worth having a base class: ``QgsTask.run()`` swallows exceptions silently, so the
    task wrapper catches ``Exception`` and reports it. Being able to tell "a failure we
    anticipated and can phrase for a human" from "a bug" is what stops that broad catch
    from hiding real defects.
    """


class ConfigurationError(LabelClientError):
    """The plugin is not configured well enough to do what was asked."""


class BackendError(LabelClientError):
    """The backend answered, but not usefully -- HTTP error, bad JSON, wrong shape."""


class RegistryError(LabelClientError):
    """The class registry document could not be understood."""
