"""The as-of-date control: pinning a layer to one instant of *valid* time.

TWO TIME AXES, AND THIS MODULE ONLY TOUCHES ONE

The backend is bitemporal. **Valid time** is when a thing was true on the ground;
**transaction time** is when we believed it. OGC API - Features has a standard parameter
for the first (``datetime``) and none at all for the second, so this control is
explicitly a valid-time control and says so in the UI. Reproducing a training set --
"the world as we understood it in January" -- is a transaction-time question, and it is
answered by :mod:`.recorded`, which is a separate module for exactly this reason. The two
keep disjoint vocabulary: this one says **as-of** and never "believed"; that one says
**believed** and never "as of". A control that said "as of" and meant the other axis is
the single likeliest source of the confusion the split exists to prevent.

TWO MECHANISMS, BECAUSE ONE OF THEM DEPENDS ON SERVER BEHAVIOUR

``datetime`` is the standard and the default. It rides on the landing-page URL's query
string, which is the only place QGIS's OAPIF provider lets a plugin put an arbitrary
query parameter -- the provider builds its item requests from the links the server
returns, so a server that emits absolute ``items`` hrefs without propagating query
parameters will drop it.

``cql2`` is the fallback for exactly that case, and what it actually does is **not** what
this docstring claimed before ``docs/read-path.md`` read the provider source. ``filter``
*is* a first-class parameter of the QGIS OAPIF URI and does reach every items request, so
the clause is never silently dropped -- that much holds. But it does not travel as
``filter=...&filter-lang=cql2-text``: QGIS only enables its CQL2 path when the server
advertises the two ``ogcapi-features-3`` filter conformance classes, and pygeoapi 0.24.0
advertises neither. The provider therefore falls back to its **Part 1** compiler, which
translates the first conjunct of :func:`cql2_filter` into a ``datetime=`` range and leaves
the ``OR`` conjunct to be evaluated client-side (``translationState = PARTIAL``).

The practical consequences: the answer stays semantically right, it over-fetches, and the
name of this mechanism describes the expression's dialect rather than the wire format.
Both mechanisms are still here, and which one a deployment uses is a setting rather than a
patch, because the failure they guard against -- a dropped parameter -- is real and
differs between servers.

THE TRAP IN THAT SECOND MECHANISM, AND WHY :func:`cql2_filter` LOOKS THE WAY IT DOES

The provider's ``filter`` parameter does **not** take CQL2-text. It takes a *QGIS
expression*, which the provider then compiles to CQL2 itself and appends as
``filter=...&filter-lang=cql2-text``. Handing it literal CQL2 is not merely ignored: an
expression QGIS cannot parse makes ``QgsVectorLayer`` invalid, so the analyst gets no
layer at all rather than an unfiltered one.

Concretely, ``TIMESTAMP('...')`` -- correct CQL2, and the obvious thing to write -- is
not a QGIS expression function, and QGIS 3.44 answers "Function TIMESTAMP is not known"
and refuses the layer. Writing the comparison against a plain quoted string instead lets
QGIS do the conversion, and it emits exactly the CQL2 that was wanted::

    "valid_from" <= '2026-01-01T00:00:00Z'
        -> filter=("valid_from" <= TIMESTAMP('2026-01-01T00:00:00.000Z'))&filter-lang=cql2-text

Two consequences worth keeping in mind when editing this function: identifiers are
double-quoted because that is a QGIS expression's column reference, and only functions
QGIS knows *and* can compile may appear -- ``to_datetime()`` parses but does not compile,
which silently downgrades the filter to client-side evaluation and downloads the whole
collection.

That is the honest resolution: the standard mechanism first, a mechanism that cannot
silently no-op second.

ONE MORE THING WORTH KNOWING BEFORE EDITING EITHER MECHANISM

The Temporal Controller does not drive ``datetime`` and cannot be made to. Its filter is
built as ``make_datetime(...)`` -- a function node where the Part 1 compiler requires a
literal -- and every temporal mode wraps its comparison in ``OR <field> IS NULL``, a
top-level ``OR`` the compiler will not walk. So the controller filters entirely on the
client. That is why this control exists at all, and it is also why a controller sliding
over a :mod:`.recorded` layer is harmless: the two axes never share a code path.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from enum import Enum

from .expressions import identifier, literal
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
    """Filter expression selecting the state valid at `value`.

    Mirrors the range containment the database performs (``valid @> :instant``) using
    only the flattened bounds the OAPIF view exposes. The upper bound is exclusive and
    NULL means "still true as far as we know" -- an unbounded ``tstzrange`` upper bound
    -- so the null test is load-bearing, not defensive: without it every currently-valid
    label disappears from the as-of view.

    Rendered as a **QGIS expression**, not as CQL2-text: the provider parses this string
    with ``QgsExpression`` and does the CQL2 conversion itself. See the module docstring
    -- writing CQL2 here fails the layer outright.
    """
    moment = instant(value)
    lo, hi = identifier(fields.valid_from), identifier(fields.valid_to)
    at = literal(moment)
    return f"{lo} <= {at} AND ({hi} IS NULL OR {hi} > {at})"


def describe(value: date | datetime | None, mechanism: AsOfMechanism) -> str:
    """One-line human summary for the panel's status label."""
    if value is None:
        return "As-of: off (current state)"
    return f"As-of {instant(value)} (valid time, via {mechanism.value})"
