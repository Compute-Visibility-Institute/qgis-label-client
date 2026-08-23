"""The as-of-date control: pinning a layer to one instant of *valid* time.

TWO TIME AXES, AND THIS MODULE ONLY TOUCHES ONE

The backend is bitemporal. **Valid time** is when a thing was true on the ground;
**transaction time** is when we believed it. OGC API - Features has a standard parameter
for the first (``datetime``) and none at all for the second, so this control is
explicitly a valid-time control and says so in the UI. Reproducing a training set --
"the world as we understood it in January" -- is a transaction-time question and is
answered server-side, not here.

TWO MECHANISMS, BECAUSE ONE OF THEM DEPENDS ON SERVER BEHAVIOUR

``datetime`` is the standard and the default. It rides on the landing-page URL's query
string, which is the only place QGIS's OAPIF provider lets a plugin put an arbitrary
query parameter -- the provider builds its item requests from the links the server
returns, so a server that emits absolute ``items`` hrefs without propagating query
parameters will drop it.

``cql2`` is the fallback for exactly that case. ``filter`` *is* a first-class parameter
of the QGIS OAPIF URI and is appended to every items request as
``filter=...&filter-lang=cql2-text``, so it always survives. It expresses the same
question directly against the valid-time columns.

Both are here, both are tested, and which one a deployment uses is a setting rather than
a patch. That is the honest resolution: the standard mechanism first, a mechanism that
cannot silently no-op second.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from enum import Enum

from .fields import DEFAULT_FIELDS, CoreFields


class AsOfMechanism(str, Enum):
    """How an as-of instant is communicated to the server."""

    DATETIME = "datetime"
    CQL2 = "cql2"

    @classmethod
    def parse(cls, value: object) -> AsOfMechanism:
        """Coerce a stored setting, falling back to the standard mechanism."""
        try:
            return cls(str(value))
        except ValueError:
            return cls.DATETIME


def instant(value: date | datetime) -> str:
    """Render an as-of point as an RFC 3339 UTC instant.

    A bare ``date`` becomes midnight UTC. That is a choice worth stating: the analyst
    picks a day, and the day has to become a point on the valid-time axis somewhere. Do
    it here, once, rather than letting each call site guess -- and do it in UTC, because
    the imagery acquisition times in ``capture.datetime`` are UTC and a local-midnight
    interpretation would put a label on the wrong side of a capture by up to a day.
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        moment = datetime.combine(value, time.min, tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def datetime_query(value: date | datetime) -> dict[str, str]:
    """Query parameters for the OAPIF ``datetime`` mechanism."""
    return {"datetime": instant(value)}


def cql2_filter(value: date | datetime, fields: CoreFields = DEFAULT_FIELDS) -> str:
    """CQL2-text selecting the state valid at `value`.

    Mirrors the range containment the database performs (``valid @> :instant``) using
    only the flattened bounds the OAPIF view exposes. The upper bound is exclusive and
    NULL means "still true as far as we know" -- an unbounded ``tstzrange`` upper bound
    -- so the null test is load-bearing, not defensive: without it every currently-valid
    label disappears from the as-of view.
    """
    moment = instant(value)
    lo, hi = fields.valid_from, fields.valid_to
    return f"{lo} <= TIMESTAMP('{moment}') AND ({hi} IS NULL OR {hi} > TIMESTAMP('{moment}'))"


def describe(value: date | datetime | None, mechanism: AsOfMechanism) -> str:
    """One-line human summary for the panel's status label."""
    if value is None:
        return "As-of: off (current state)"
    return f"As-of {instant(value)} (valid time, via {mechanism.value})"
