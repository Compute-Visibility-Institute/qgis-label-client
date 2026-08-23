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
    """The backend answered, but not usefully -- HTTP error, bad JSON, wrong shape.

    Carries the HTTP status where there was one, and the ``Retry-After`` the server asked
    for. Both exist so a caller can *decide* rather than only report: a 429 from the auth
    edge is a request to wait, and a bootstrap that treats it as a refusal turns a
    throttled run into a half-written system of record. Everything else keeps working off
    ``str(exc)``, which is unchanged.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def throttled(self) -> bool:
        """True when the server refused *for now* rather than refusing the content."""
        return self.status == 429


class RegistryError(LabelClientError):
    """The class registry document could not be understood."""
