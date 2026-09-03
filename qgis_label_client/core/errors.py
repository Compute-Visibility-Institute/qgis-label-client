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

    Carries the HTTP status where there was one, the ``Retry-After`` the server asked for,
    and the error document it sent. All three exist so a caller can *decide* rather than
    only report: a 429 from the auth edge is a request to wait, and a bootstrap that
    treats it as a refusal turns a throttled run into a half-written system of record.
    Everything else keeps working off ``str(exc)``, which is unchanged.

    ``payload`` is the JSON object verbatim, when the body was one. The prose in
    ``str(exc)`` is for a person and is truncated to fit a report; the bulk publish needs
    two machine-readable fields out of a refusal -- *which* feature was refused, and
    whether anything was created -- and reading those back out of the sentence would be
    parsing English to decide what to re-send.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        payload: object | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.payload = payload

    @property
    def throttled(self) -> bool:
        """True when the server refused *for now* rather than refusing the content."""
        return self.status == 429


class MixedGeometryError(BackendError):
    """A collection serves several geometry types at once, so no layer can show it whole.

    Its own type rather than a plain :class:`BackendError` because one caller has to *act*
    on it rather than only report it. The historical-view control remembers which
    collection serves transaction time and asks for it only once; a remembered mixed one
    would refuse every attempt from then on, with no control anywhere to change the
    answer. Recognising this particular failure is what lets that id be forgotten so the
    next attempt asks again -- and the geometry-typed collections are in the list it asks
    with.
    """


class RegistryError(LabelClientError):
    """The class registry document could not be understood."""
