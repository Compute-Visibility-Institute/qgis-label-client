"""The historical-view control: pinning a layer to one instant of *transaction* time.

READ :mod:`.asof` FIRST. THESE TWO MODULES ARE THE TWO TIME AXES AND CONFUSING THEM IS
THE FAILURE THIS SPLIT EXISTS TO PREVENT.

    valid time        when a label was true ON THE GROUND      -> :mod:`.asof`
    transaction time  when WE BELIEVED it                      -> this module

A building demolished in March stops being valid in March, whoever noticed and whenever.
A polygon somebody deleted by mistake at 13:15 was never wrong about the ground -- the
team's belief changed. Both are stored, both are answerable, and they are different
questions. One file per axis, so that a wrong import is visible in a diff.

The vocabulary is kept disjoint on purpose. This axis says **believed** and never "as of";
:mod:`.asof` says **as-of** and never "believed". Two words, two axes, no overlap -- a
screenshot of the panel has to be unambiguous about which control produced it.

THE PIN TRAVELS ON THE LANDING URL. THIS WAS MEASURED, NOT REASONED.

``?recorded_at=<instant>`` on the layer's landing URL, plus ``X-Recorded-At`` in the URI's
``http-header:`` vocabulary. The query parameter is the one that does the work, and the
history of that sentence is worth keeping, because the reasoned answer was the opposite one
and it was wrong.

The argument used to be: the header rides the URI's ``http-header:`` parameters, therefore
it is on the ``OPTIONS`` editability probe, therefore the server can answer that probe
without the write verbs and QGIS greys the pencil out by itself; whereas
``QgsOapifProvider::computeCapabilities`` builds the probe without calling
``appendExtraQueryParameters``, so a landing-URL query parameter is absent from it.

Half of that survives measurement. Against QGIS 3.44.13:

* **``http-header:`` parameters never reach the wire at all.** Captured against a bare HTTP
  listener, a layer URI carrying ``X-Track``, ``X-Recorded-At`` and a marker header sent
  none of the three. The same three carried by an ``APIHeader`` auth configuration arrived
  intact -- which is why the *track* has always worked: :mod:`..auth` puts ``X-Track`` on
  the credential. The instant had no such channel, so it never arrived, and the historical
  layer resolved at ``now()``.
* **A landing-URL query parameter does survive**, on every request the provider builds
  except the probe: ``/``, ``/openapi``, ``/collections/{id}`` and both ``/items`` fetches.

The probe is genuinely unpinned, exactly as ``computeCapabilities`` predicts. That costs
nothing here: the historical collection is ``editable: false`` on the server, so the probe
answers ``Allow: HEAD, GET`` pinned or not, and QGIS reports the layer with no write
capabilities at all. Measured, not assumed -- and :func:`..layers.provider_advertises_writes`
raises if that ever stops being true.

The header is still sent. It costs one URI parameter, it is what curl and the web viewer
use, and a QGIS build that starts honouring ``http-header:`` would simply be sending the
same value twice.

THE ECHO IS A CANARY, AND IT MUST NOT BE A LAYER FILTER

``v_label_asof`` echoes the instant it actually resolved at on every row. The check is
:func:`echo_mismatch`, run against the *loaded* layer's own features: if the pin fails to
reach the database the view resolves at ``now()``, the echo does not match, and the layer
is refused **by name**, saying which instant arrived instead.

It used to be a filter -- ``"recorded_at" = '<what we asked for>'`` ANDed into the layer's
subset -- and that could never have worked. QGIS types an OAPIF property by SNIFFING ITS
VALUE, so an unprefixed RFC 3339 echo arrived as a DateTime field, and QGIS compiles a
filter on any DateTime field into ``?datetime=`` -- the VALID-time parameter. The canary
was therefore silently filtering the wrong axis, a deliberately *wrong* canary was observed
returning features rather than none, and the layer's Temporal Controller was being pinned
to one valid instant as a side effect. Three bugs in one clause, none of them visible.

Two things fix it and both are needed. The server prefixes the transaction-time renderings
with ``@`` so QGIS types them as strings (``db/migrations/014_asof_text_axis.sql``, which
carries the measured table), and the check moved out of the filter and into
:func:`echo_mismatch`, which compares INSTANTS rather than text and so cannot be defeated by
a rendering change on either side.

THE WIRE FORM IS A STRING, EVERYWHERE PAST :func:`instant`

:func:`instant` is the one place a date becomes text. The header, the canary, the layer
name and the layer's stored custom property then all carry that *same string*, so they
cannot drift from one another by a rounding rule or a timezone. :func:`parse_instant` is
the only reader, and it is strict for exactly that reason.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone

from . import asof
from .errors import ConfigurationError
from .fields import DEFAULT_FIELDS, CoreFields

#: Header naming the transaction-time instant a request is a view of.
#:
#: The client *asks* with this. The auth edge validates it and re-asserts it upstream under
#: a reserved prefix a client cannot forge, so sending it is a request, not a claim --
#: exactly the ask/assert boundary :data:`..tracks.TRACK_HEADER` already establishes.
RECORDED_AT_HEADER = "X-Recorded-At"

#: The query-parameter spelling -- and, for a QGIS layer, THE transport.
#:
#: It goes on the layer's landing URL, which is the only place a plugin can put an
#: arbitrary parameter that the native OAPIF provider will carry forward; the provider
#: drops ``http-header:`` parameters entirely. See the module docstring for the capture.
RECORDED_AT_QUERY = "recorded_at"

#: The instant format, second-precision UTC. What goes ON THE WIRE.
#:
#: The header value, the query parameter, the layer's stored custom property and the layer
#: name are all rendered from this one function, so they cannot drift from one another by a
#: rounding rule or a timezone.
INSTANT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: What the server prefixes its transaction-time renderings with. A CROSS-REPO CONTRACT,
#: asserted by a test in each repository.
#:
#: ``v_label_asof`` renders ``belief_from``, ``belief_to`` and the ``recorded_at`` echo as
#: ``@2026-01-15T08:00:00Z`` (``db/migrations/014_asof_text_axis.sql``). The ``@`` is
#: load-bearing: QGIS types an OAPIF property by sniffing its VALUE, and an unprefixed RFC
#: 3339 string becomes a DateTime field whose filters compile to ``?datetime=`` -- the
#: VALID-time parameter. With the prefix they stay strings, and the Temporal Controller's
#: field picker stops offering them.
#:
#: :func:`echo_instant` strips it. Nothing in this plugin ever renders it: the plugin sends
#: bare instants and reads prefixed ones.
ECHO_PREFIX = "@"

#: How far past "now" an instant may sit before it is refused.
#:
#: Not zero, because "now" is a legitimate thing to ask for and the annotator's clock is
#: not the server's. Small, because in practice a future instant is always a typo or a
#: timezone bug.
FUTURE_SKEW_SECONDS = 60

#: The word this axis uses in the UI. Never "as of" -- see the module docstring.
BELIEVED = "BELIEVED"

#: Said in the layer name itself, because a layer tree is where somebody decides what to
#: click on and the truncation happens from the right of a name they can rename anyway.
READ_ONLY = "read-only"


def instant(value: date | datetime) -> str:
    """Render a transaction-time point as an RFC 3339 UTC instant.

    Deliberately :func:`.asof.instant`, shared rather than copied. The rule -- a bare date
    means midnight UTC, an aware datetime is *converted* rather than relabelled -- has to
    be identical on both axes, and two implementations of one rule are two implementations
    that differ. What must stay separate is the *meaning*; the rendering is arithmetic.
    """
    return asof.instant(value)


def parse_instant(text: str) -> datetime | None:
    """Read back an instant this module rendered, or ``None``.

    STRICT, and that is the point: this reads the plugin's own stored values -- a layer's
    custom property, a remembered picker default -- back into the contract. Anything that
    is not exactly :data:`INSTANT_FORMAT` did not come from :func:`instant`, so treating it
    as an instant would put a value on the wire that the database's echo cannot match, and
    the annotator would get an empty layer with no explanation.

    Use :func:`parse_rfc3339` for values the *server* supplies; those are legitimately in
    any RFC 3339 form and refusing them would be this plugin insisting on its own dialect.
    """
    candidate = (text or "").strip()
    if not candidate:
        return None
    try:
        return datetime.strptime(candidate, INSTANT_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_rfc3339(text: str) -> datetime | None:
    """Read any RFC 3339 instant the backend sends, in UTC, or ``None``.

    Lenient on purpose, and only ever pointed at server-supplied values -- the earliest
    recorded belief on a track, which sets the picker's floor. The backend is free to send
    microseconds or a numeric offset and this plugin has no business rejecting either.

    ``Z`` is rewritten before parsing because ``datetime.fromisoformat`` only learned it in
    Python 3.11, and the interpreter here is whatever the user's QGIS ships -- 3.10 on some
    Linux packages.
    """
    candidate = (text or "").strip()
    if not candidate:
        return None
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def display_instant(moment: str) -> str:
    """The instant as a person reads it: ``2026-01-15 08:00Z``.

    A display form, never a wire form. The space and the dropped seconds make a layer name
    readable in a tree that truncates; :func:`instant` remains the only thing the server
    ever sees. Seconds are shown when they are not zero, because a name saying ``08:00Z``
    over a layer pinned to ``08:00:45Z`` would be a small lie in a feature whose entire
    value is exactness.

    An unrecognised string is returned unchanged rather than blanked: if this is ever
    handed something odd, the odd thing itself is the useful diagnostic.
    """
    parsed = parse_instant(moment)
    if parsed is None:
        return moment
    pattern = "%Y-%m-%d %H:%M" if parsed.second == 0 else "%Y-%m-%d %H:%M:%S"
    return parsed.strftime(pattern) + "Z"


def validate(value: date | datetime, *, now: datetime | None = None) -> str:
    """Render `value`, refusing an instant in the future. Returns the wire form.

    A future instant is not merely useless, it is *populated and wrong*: the belief set at
    a future time is the current one, so the layer would come back full of features under a
    caption asserting something nobody has ever believed. The backend refuses it too -- this
    is the copy that runs before a request is made, so the annotator is told by the control
    they used rather than by an HTTP status.

    Both instants are named in the message, because a future instant is nearly always a
    timezone bug and seeing the two side by side is what makes that obvious.
    """
    moment = instant(value)
    parsed = parse_instant(moment)
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if parsed is not None and parsed > reference + timedelta(seconds=FUTURE_SKEW_SECONDS):
        raise ConfigurationError(
            f"{moment} is in the future; it is now {instant(reference)}. Nobody has "
            "believed anything at that instant yet, so there is nothing to show. Check "
            "the time zone: this picker is UTC."
        )
    return moment


def headers(moment: str) -> dict[str, str]:
    """The request header pinning a layer to one recorded instant.

    Empty for an unpinned layer rather than a blank header: a blank ``X-Recorded-At`` is
    not "no instant", it is a value the edge has to decide what to do with -- the same rule
    :func:`..uri.header_params` applies to the track.
    """
    return {RECORDED_AT_HEADER: moment} if moment else {}


def echo_instant(value: object) -> datetime | None:
    """Read one served ``recorded_at`` echo back into an instant, or ``None``.

    TOLERANT ON PURPOSE, AND THAT IS THE WHOLE DESIGN OF THIS CHECK.

    The echo is a value the *server* chose, read back through whatever type QGIS decided the
    column had, so this must not insist on the plugin's own dialect. Three shapes turn up:

    * ``"@2026-01-15T08:00:00Z"`` -- what the server renders today.
    * ``"2026-01-15T08:00:00Z"`` -- an older backend, before the ``@`` marker existed.
    * a ``QDateTime`` or ``datetime`` -- what QGIS hands back if it ever types the column as
      a date again, which is exactly the regression :data:`ECHO_PREFIX` prevents. Accepting
      it here means the check keeps *working* through that regression rather than failing
      closed on every layer at once.

    Comparing instants rather than text is what makes all three equivalent, and it is why a
    rendering change on either side can no longer empty every historical layer.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    # QDateTime, without importing Qt into a module that must stay pure. `toString` with an
    # ISO format is the one method every Qt binding spells the same way.
    to_string = getattr(value, "toString", None)
    if to_string is not None and not isinstance(value, str):
        try:
            return parse_rfc3339(to_string("yyyy-MM-ddTHH:mm:ss") + "Z")
        except Exception:  # noqa: BLE001 - any binding-specific failure means "not an instant"
            return None
    text = str(value).strip()
    if text.startswith(ECHO_PREFIX):
        text = text[len(ECHO_PREFIX) :]
    return parse_rfc3339(text)


def echo_mismatch(asked: str, served: object) -> str:
    """An explanation if the layer is not showing the instant it asked for, else ``""``.

    THE CANARY, AND WHY IT IS A REFUSAL RATHER THAN A FILTER.

    Redundant when everything works, which is the point -- it checks the mechanism instead of
    duplicating it. The view echoes the instant it actually resolved at on every row, so if
    the pin fails to reach the database the view resolves at ``now()`` and this says so.

    It used to be a subset filter and could never have worked: QGIS compiled it into
    ``?datetime=``, the valid-time parameter (see the module docstring). Done here, on values
    already loaded, it cannot be rewritten by anything.

    A refusal beats an empty layer, too. "Empty" is indistinguishable from a backend outage,
    from an over-tight filter and from a track with no data; this names the instant that
    arrived instead, which is nearly always enough to see what went wrong.

    ``""`` when the layer has no rows to check: a historical view legitimately has none
    before the first label was drawn, and refusing that would refuse a correct answer.
    """
    wanted = parse_instant(asked)
    if wanted is None:
        return ""
    got = echo_instant(served)
    if got is not None and got == wanted:
        return ""
    arrived = instant(got) if got is not None else repr(served)
    return (
        f"This layer asked to be a view of {asked} but the server answered as of "
        f"{arrived}. The instant did not reach the database, so the layer would be showing "
        "a different belief from the one its name claims. Nothing here is trustworthy until "
        "that is fixed; check that the deployment is recent enough to accept "
        f"{RECORDED_AT_QUERY}."
    )


def exposes_recorded_axis(names: Iterable[str], fields: CoreFields = DEFAULT_FIELDS) -> bool:
    """True when a loaded layer carries the transaction-time echo column.

    How the plugin recognises a historical collection **without knowing its id**. Collection
    ids are a deployment's choice, exactly as class names are, so the test is "does this
    layer answer the transaction-time question?" and the column name itself comes from the
    registry rather than from here.

    Used two ways, and the second matters more than the first: to refuse to pin a layer
    that could not honour the pin, and to keep the QA tools from mistaking a historical
    layer for the live one.
    """
    return fields.recorded_at in set(names)


def base_name(title: str, collection_id: str) -> str:
    """The short name a historical layer is built from, out of a collection's title.

    A collection title has to explain itself in a list of collections -- "Labels (as
    believed at a past instant, read-only)" is the right title and the wrong layer name.
    The layer name already carries the instant and the words ``read-only``, so a trailing
    parenthetical would say both a second time inside a tree that truncates from the right.

    Falls back to the id when a deployment sends no title, which is what
    :attr:`..collections.Collection.display_name` does too.
    """
    head, _, _ = (title or "").strip().partition("(")
    return head.strip() or collection_id


def layer_name(moment: str, base: str) -> str:
    """The name a historical layer wears in the Layers panel.

    ``[BELIEVED 2026-01-15 08:00Z] Labels - read-only``

    THE DISCRIMINATING TOKEN LEADS, because the layer tree truncates from the right and the
    one thing a person must not have to guess is which of two similar layers they are about
    to draw on. Truncated to twelve characters these are still unconfusable against a live
    layer's plain name.

    ``BELIEVED`` rather than ``AS-OF``: the panel's other control is titled "As-of date
    (valid time)", and if both said "as of" a screenshot would be genuinely ambiguous.
    """
    return f"[{BELIEVED} {display_instant(moment)}] {base} — {READ_ONLY}"


def describe(moment: str) -> str:
    """The transaction-time half of the panel's two-axis status line."""
    if not moment:
        return f"{BELIEVED.title()}: now (live)"
    return f"{BELIEVED.title()}: {display_instant(moment)} (fixed)"


def describe_axes(moment: str, as_of, mechanism) -> str:
    """One line naming BOTH axes, always.

    Load-bearing rather than tidy. Each control on its own reads as "the" time control, and
    a person who has only ever seen one of them will reasonably assume the other axis is
    not in play. Naming both -- including when one of them is off -- means neither can be
    read as the only one.

    The valid half says *Temporal Controller* when the as-of control is off, because that
    is then the truth: QGIS filters valid time on the client (verified from source -- the
    controller's expression cannot compile to ``datetime=``), and it is still filtering.
    """
    if as_of is None:
        valid = "Valid: Temporal Controller (client-side)"
    else:
        valid = f"Valid: pinned to {asof.instant(as_of)} (via {mechanism.value})"
    return f"{describe(moment)}  ·  {valid}"


def read_only_reason(moment: str) -> str:
    """Why the pencil is greyed out on this layer, phrased for the person who noticed.

    The design brief's requirement, and it is a real one: a control that silently stops
    working is indistinguishable from a broken one. QGIS disables editing here by itself --
    it probes ``/collections/{id}/items`` with ``OPTIONS`` and finds no ``POST`` in the
    ``Allow`` header, because the request carried an instant -- and a person who does not
    know that files a bug.
    """
    return (
        f"This layer is what the team believed at {display_instant(moment)}, not what is "
        "true now. Editing is off on purpose and QGIS turns it off by itself: the server "
        "answers the editability probe without the write verbs when a request names a "
        "transaction-time instant, and this plugin marks the layer read-only as well. A "
        "past belief is a record, not a draft - it cannot be edited, only superseded. "
        "Make the correction on the live layer instead."
    )


def unpinned_warning(collection_id: str) -> str:
    """Why a historical collection cannot simply be checked in the collection list.

    The failure this prevents is the one this codebase keeps ruling against: the layer
    would load, be full of features, be named after a past-belief view, and show the
    *present* -- because the view falls back to ``now()`` when no instant reaches it.
    """
    return (
        f"{collection_id!r} serves the world as it was BELIEVED at a chosen instant, so it "
        "cannot be loaded from the collection list: a layer built that way carries no "
        "instant, and the server would answer it with the current state under a name that "
        "says otherwise. Use 'Historical view (transaction time)' in the panel, which "
        "pins the instant into the layer's own data source."
    )


def cannot_be_pinned(collection_id: str, fields: CoreFields = DEFAULT_FIELDS) -> str:
    """Why an instant was refused for a collection that cannot answer it."""
    return (
        f"{collection_id!r} has no {fields.recorded_at!r} column, so it cannot say which "
        "instant it answered at and a historical view of it could not be verified. Only "
        "the collection built for transaction time can be pinned; check which collection "
        "the panel is configured to use."
    )


def empty_view_message(moment: str, track_name: str = "", earliest: str = "") -> str:
    """What to say when a historical layer loads correctly and contains nothing.

    An empty layer is a perfectly valid answer here -- ask about an instant before the
    dataset existed and the honest reply is "nothing" -- but on screen it is indis-
    tinguishable from a broken one. Saying which of the two it is, and giving the floor
    when the backend published one, is the difference between a fact and a support call.
    """
    where = f" on track {track_name}" if track_name else ""
    parsed = parse_rfc3339(earliest)
    floor = (
        f" The earliest belief recorded{where} is {display_instant(instant(parsed))}."
        if parsed is not None
        else " The backend did not say how far back its record goes."
    )
    return (
        f"Nothing was believed to exist at {display_instant(moment)}{where}.{floor} "
        "The layer loaded correctly; the belief set at that instant was simply empty."
    )
