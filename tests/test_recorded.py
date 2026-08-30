"""The transaction-time control, and the boundary between it and the valid-time one.

WHAT IS ACTUALLY AT RISK HERE

Two things, and neither of them is "does the feature work".

The first is **the two axes being confused**. Valid time is when a label was true on the
ground; transaction time is when the team believed it. Both are stored, both are
answerable, and a person reading a map produced by one while thinking it was the other
draws a conclusion that is wrong in a way nothing on screen contradicts. So the tests
below are largely about vocabulary and about naming: the layer name, the status line, the
words each module is allowed to use.

The second is **an instant that fails to reach the database**. The view falls back to
``now()`` when no instant arrives, which would answer a request captioned "January" with
today's data -- populated and wrong, the failure this codebase keeps ruling against. That
is not hypothetical: it was the shipped behaviour until QGIS 3.44 was measured and found to
drop the URI's ``http-header:`` parameters entirely, so the pin now travels as a landing-URL
query parameter. The canary catches a pin that goes missing again, and it is a check on
values already loaded rather than a subset filter -- as a filter QGIS compiled it into
``?datetime=`` and it silently verified nothing while filtering the other axis.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from qgis_label_client.core import asof, recorded
from qgis_label_client.core.asof import AsOfMechanism
from qgis_label_client.core.errors import ConfigurationError
from qgis_label_client.core.fields import CoreFields
from qgis_label_client.settings import PluginSettings

MOMENT = "2026-01-15T08:00:00Z"
WITH_SECONDS = "2026-01-15T08:00:45Z"

# --- the cross-repo instant contract ----------------------------------------


def test_the_wire_format_is_the_one_the_database_echoes():
    """A CONTRACT WITH ANOTHER REPOSITORY, asserted as a literal on purpose.

    ``v_label_asof.recorded_at`` echoes the instant it resolved at, and the layer filter
    compares against the string this plugin sent. The two repositories release
    independently, so a "tidy-up" on either side -- milliseconds, an offset instead of Z --
    silently empties every historical layer and looks like a backend outage. If this
    assertion is ever changed, ``db/migrations/012_asof_recorded.sql`` changes with it.
    """
    assert recorded.INSTANT_FORMAT == "%Y-%m-%dT%H:%M:%SZ"
    assert recorded.instant(datetime(2026, 1, 15, 8, 0, 0, tzinfo=timezone.utc)) == MOMENT


def test_the_instant_rule_is_shared_with_the_valid_time_axis_not_reimplemented():
    # The *meaning* of the two axes must stay apart; the arithmetic of rendering a point in
    # time must not. Two implementations of one rule are two implementations that differ.
    moment = datetime(2026, 4, 21, 3, 40, 14, tzinfo=timezone(timedelta(hours=2)))
    assert recorded.instant(moment) == asof.instant(moment)
    assert recorded.instant(date(2026, 4, 21)) == asof.instant(date(2026, 4, 21))


def test_an_aware_instant_is_converted_rather_than_relabelled():
    aware = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert recorded.instant(aware) == MOMENT


def test_a_bare_date_is_midnight_utc():
    assert recorded.instant(date(2026, 1, 15)) == "2026-01-15T00:00:00Z"


def test_reading_the_plugins_own_instants_back_is_strict():
    """Strict because this reads values that are about to go back on the wire.

    A stored instant that is not exactly the contract format did not come from
    :func:`instant`, and sending it produces an empty layer with no explanation. Refusing
    it means the picker falls back to a default that works.
    """
    assert recorded.parse_instant(MOMENT) == datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
    for junk in (
        "",
        "  ",
        "2026-01-15T08:00:00.123Z",
        "2026-01-15T08:00:00+00:00",
        "2026-01-15 08:00:00Z",
        "2026-01-15",
        "now",
        "-4 minutes",
        "2026-01-01/2026-06-30",
    ):
        assert recorded.parse_instant(junk) is None, junk


def test_reading_the_backends_instants_back_is_lenient():
    """The floor comes from the server, which is free to use any RFC 3339 form.

    Insisting on this plugin's own dialect for a value the *backend* chose would be the
    plugin refusing a correct answer. Everything is normalised to UTC on the way in.
    """
    expected = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
    for text in (
        "2026-01-15T08:00:00Z",
        "2026-01-15T08:00:00z",
        "2026-01-15T08:00:00+00:00",
        "2026-01-15T10:00:00+02:00",
        "2026-01-15T08:00:00",
    ):
        assert recorded.parse_rfc3339(text) == expected, text
    assert recorded.parse_rfc3339("2026-01-15T08:00:00.500Z").second == 0
    assert recorded.parse_rfc3339("nonsense") is None
    assert recorded.parse_rfc3339("") is None


# --- refusals ---------------------------------------------------------------


def test_a_future_instant_is_refused_and_both_instants_are_named():
    """Refused because the answer would be POPULATED and wrong.

    ``label_asof_all(future)`` returns the current belief set quite happily, so the layer
    would be full of features under a caption asserting something nobody has ever believed.
    A future instant is nearly always a timezone bug, and seeing the two instants side by
    side is what makes that obvious.
    """
    now = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
    with pytest.raises(ConfigurationError) as caught:
        recorded.validate(now + timedelta(hours=8), now=now)
    message = str(caught.value)
    assert "2026-01-15T16:00:00Z" in message
    assert MOMENT in message


def test_the_present_is_not_refused_because_clocks_differ():
    # "Now" is a legitimate thing to ask for, and the annotator's clock is not the
    # server's. The tolerance is small because a real future instant is a typo.
    now = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
    assert recorded.validate(now, now=now) == MOMENT
    assert recorded.validate(now + timedelta(seconds=30), now=now)
    with pytest.raises(ConfigurationError):
        recorded.validate(now + timedelta(seconds=120), now=now)


def test_a_past_instant_is_always_allowed_however_far_back():
    # An instant before any data existed is a VALID question whose answer is an empty
    # layer. Refusing it would turn a fact into an error.
    now = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
    assert recorded.validate(date(1999, 1, 1), now=now) == "1999-01-01T00:00:00Z"


# --- the transport ----------------------------------------------------------


def test_the_instant_travels_as_a_header():
    """A header, not a query parameter, and the reason is the read-only enforcement.

    QGIS decides editability from an OPTIONS probe built by ``computeCapabilities``, which
    installs the URI's headers and appends NO query parameters. A query-only pin therefore
    reports the collection writable, QGIS enables editing, and an edit made on a historical
    map lands on the live row.
    """
    assert recorded.headers(MOMENT) == {"X-Recorded-At": MOMENT}
    assert recorded.RECORDED_AT_HEADER == "X-Recorded-At"


def test_an_unpinned_layer_sends_no_blank_header():
    # A blank X-Recorded-At is not "no instant", it is a value the edge has to decide what
    # to do with. Same rule the track header follows.
    assert recorded.headers("") == {}


def test_the_query_spelling_exists_but_is_not_what_a_layer_uses():
    # THE transport for a QGIS layer, not a convenience: it goes on the landing URL, which
    # is the only channel into the requests the native provider builds for itself.
    assert recorded.RECORDED_AT_QUERY == "recorded_at"


# --- the canary -------------------------------------------------------------


def test_the_echo_check_passes_on_the_instant_it_asked_for():
    """Redundant when everything works. That is the point -- it checks the mechanism.

    The server echoes the instant it actually resolved at on every row, prefixed with `@`.
    If the pin does not reach the database the view resolves at now() and this says so.
    """
    assert recorded.echo_mismatch(MOMENT, "@" + MOMENT) == ""


def test_the_echo_check_fails_and_names_both_instants():
    problem = recorded.echo_mismatch(MOMENT, "@2026-08-25T14:39:11Z")
    assert problem
    assert MOMENT in problem
    assert "2026-08-25T14:39:11Z" in problem


def test_the_echo_check_reads_the_server_marker_and_tolerates_its_absence():
    """The `@` is a CROSS-REPO CONTRACT, and this side must survive it changing.

    db/migrations/014_asof_text_axis.sql renders `@2026-01-15T08:00:00Z`, and the marker is
    load-bearing there: without it QGIS sniffs the value, types the column as a DateTime
    field, and compiles any filter on it into `?datetime=` -- the VALID-time parameter.
    Here it is merely stripped, because comparing instants rather than text is what stops a
    rendering change on either side from emptying every historical layer at once.
    """
    assert recorded.ECHO_PREFIX == "@"
    assert recorded.echo_instant("@2026-01-15T08:00:00Z") == recorded.echo_instant(MOMENT)


def test_the_echo_check_accepts_a_datetime_because_qgis_may_hand_one_back():
    # Exactly the regression ECHO_PREFIX prevents; accepting it means the check keeps
    # working through that regression rather than failing closed on every layer at once.
    served = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
    assert recorded.echo_instant(served) == served
    assert recorded.echo_mismatch(MOMENT, served) == ""


def test_a_naive_datetime_echo_is_read_as_utc():
    assert recorded.echo_mismatch(MOMENT, datetime(2026, 1, 15, 8, 0)) == ""


def test_an_unparseable_echo_is_a_mismatch_and_shows_what_arrived():
    problem = recorded.echo_mismatch(MOMENT, "not a time")
    assert problem
    assert "not a time" in problem


def test_an_unpinned_layer_is_never_checked():
    # Nothing was claimed, so there is nothing to verify. Refusing here would refuse every
    # live layer in the project.
    assert recorded.echo_mismatch("", "@" + MOMENT) == ""


def test_a_collection_is_recognised_by_its_echo_column_not_by_its_id():
    # Collection ids are a deployment's choice, exactly as class names are. The test is
    # "can this layer say which instant it answered at?", and the column name itself comes
    # from the registry.
    assert recorded.exposes_recorded_axis(["label_id", "class_id", "recorded_at"])
    assert not recorded.exposes_recorded_axis(["label_id", "class_id", "valid_from"])
    renamed = CoreFields().merged({"recorded_at": "resolved_at"})
    assert recorded.exposes_recorded_axis(["resolved_at"], renamed)
    assert not recorded.exposes_recorded_axis(["recorded_at"], renamed)


# --- naming -----------------------------------------------------------------


def test_the_discriminating_token_leads_because_the_layer_tree_truncates():
    """A layer tree truncates from the RIGHT, and this name has to survive that.

    The one thing a person must not have to guess is which of two similar layers they are
    about to draw on.
    """
    live = "Labels"
    name = recorded.layer_name(MOMENT, live)
    assert name.startswith("[BELIEVED 2026-01-15 08:00Z]")
    # Truncated to twelve characters, a historical layer and the live one it sits next to
    # are still unconfusable. Putting the base name first would make both read "Labels…".
    assert name[:12] != live[:12]
    assert "read-only" in name


def test_the_layer_name_says_believed_never_as_of():
    # The panel's other control is titled "As-of date (valid time)". If both said "as of",
    # a screenshot would not say which axis produced the map.
    assert "BELIEVED" in recorded.layer_name(MOMENT, "Labels")
    assert "AS-OF" not in recorded.layer_name(MOMENT, "Labels").upper()


def test_the_display_form_drops_zero_seconds_but_never_lies_about_them():
    assert recorded.display_instant(MOMENT) == "2026-01-15 08:00Z"
    assert recorded.display_instant(WITH_SECONDS) == "2026-01-15 08:00:45Z"


def test_an_unrecognised_instant_is_shown_rather_than_blanked():
    # If this is ever handed something odd, the odd thing itself is the diagnostic.
    assert recorded.display_instant("garbage") == "garbage"


def test_the_collection_title_is_trimmed_to_a_layer_name():
    # The collection's own title has to explain itself in a list of collections; the layer
    # name already carries the instant and the words "read-only", so a trailing
    # parenthetical would say both a second time inside a tree that truncates.
    long_title = "Labels (as believed at a past instant, read-only)"
    assert recorded.base_name(long_title, "x") == "Labels"
    assert recorded.base_name("Labels", "x") == "Labels"
    # No title, or a title that is nothing but its own explanation, falls back to the id --
    # which is what Collection.display_name does too.
    assert recorded.base_name("", "label_asof") == "label_asof"
    assert recorded.base_name("(only a parenthetical)", "label_asof") == "label_asof"


# --- the two-axis status line -----------------------------------------------


def test_the_status_line_always_names_both_axes():
    """Both, even when one of them is off, and that is load-bearing.

    Each control on its own reads as "the" time control. A person who has only met one of
    them will assume the other axis is not in play; naming both means neither can be.
    """
    line = recorded.describe_axes(MOMENT, None, AsOfMechanism.DATETIME)
    assert "Believed: 2026-01-15 08:00Z (fixed)" in line
    assert "Valid: Temporal Controller" in line

    both = recorded.describe_axes(MOMENT, date(2026, 3, 1), AsOfMechanism.DATETIME)
    assert "Believed: 2026-01-15 08:00Z" in both
    assert "2026-03-01T00:00:00Z" in both


def test_a_live_session_still_names_the_transaction_time_axis():
    line = recorded.describe_axes("", None, AsOfMechanism.DATETIME)
    assert "Believed: now (live)" in line
    assert "Valid:" in line


def test_the_two_modules_never_borrow_each_others_word():
    """THE REGRESSION THIS WHOLE SPLIT EXISTS TO PREVENT.

    Two controls that both say "as of" and mean different axes is the failure mode. The
    vocabulary is kept disjoint in what each module *renders*, so a screenshot of the panel
    is unambiguous about which question produced it.
    """
    for text in (
        recorded.describe(MOMENT),
        recorded.describe(""),
        recorded.layer_name(MOMENT, "Labels"),
        recorded.read_only_reason(MOMENT),
        recorded.empty_view_message(MOMENT, "some_track", "2026-01-01T00:00:00Z"),
    ):
        lowered = text.lower()
        assert "as of" not in lowered, text
        assert "as-of" not in lowered, text

    for text in (
        asof.describe(date(2026, 1, 15), AsOfMechanism.DATETIME),
        asof.describe(None, AsOfMechanism.CQL2),
    ):
        assert "believ" not in text.lower(), text


# --- explaining the read-only layer ------------------------------------------


def test_the_read_only_reason_says_why_rather_than_only_that():
    """A control that silently stops working is indistinguishable from a broken one.

    QGIS greys the pencil out by itself here, which is the right outcome and a mystifying
    one. The sentence has to name the mechanism and the alternative.
    """
    text = recorded.read_only_reason(MOMENT)
    assert "2026-01-15 08:00Z" in text
    assert "OPTIONS" not in text  # phrased for an annotator, not for a protocol reader
    assert "editability probe" in text
    assert "live layer" in text


def test_an_empty_historical_layer_is_reported_as_a_fact_not_a_failure():
    # An empty layer and a broken one look identical on screen. Saying which of the two it
    # is, with the floor when the backend published one, is the whole value here.
    text = recorded.empty_view_message("2025-06-01T00:00:00Z", "some_track", "2026-08-25T12:57:00Z")
    assert "2025-06-01 00:00Z" in text
    assert "some_track" in text
    assert "2026-08-25 12:57Z" in text
    assert "loaded correctly" in text


def test_a_backend_with_no_floor_still_produces_a_usable_sentence():
    text = recorded.empty_view_message("2025-06-01T00:00:00Z")
    assert "2025-06-01 00:00Z" in text
    assert "how far back" in text


def test_refusing_a_collection_that_cannot_answer_names_the_column():
    text = recorded.cannot_be_pinned("some_collection")
    assert "some_collection" in text
    assert "recorded_at" in text


def test_the_unpinned_warning_explains_the_populated_and_wrong_failure():
    text = recorded.unpinned_warning("some_collection")
    assert "some_collection" in text
    assert "current state" in text


# --- the remembered picker default -------------------------------------------


def test_the_remembered_instant_is_a_picker_default_and_is_validated_on_the_way_out():
    """A hand-edited QGIS3.ini must not put an unsendable string on the wire.

    It would produce an empty layer with no explanation, which reads as a backend outage.
    """
    settings = PluginSettings()
    settings.set_recorded_at(MOMENT)
    assert settings.recorded_at == MOMENT
    settings.set("recorded_at", "last tuesday")
    assert settings.recorded_at == ""
    settings.set_recorded_at("")
    assert settings.recorded_at == ""
