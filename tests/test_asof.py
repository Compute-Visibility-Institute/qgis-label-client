"""The as-of control. Both mechanisms, because either can be the one a deployment uses."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from qgis_label_client.core.asof import (
    AsOfMechanism,
    cql2_filter,
    datetime_query,
    describe,
    instant,
)
from qgis_label_client.core.fields import CoreFields


def test_a_bare_date_becomes_midnight_utc():
    assert instant(date(2026, 4, 21)) == "2026-04-21T00:00:00Z"


def test_naive_datetimes_are_read_as_utc():
    assert instant(datetime(2026, 4, 21, 3, 40, 14)) == "2026-04-21T03:40:14Z"


def test_aware_datetimes_are_converted_not_relabelled():
    # +02:00 03:40 is 01:40 UTC. Relabelling instead of converting would place a label on
    # the wrong side of a capture time.
    aware = datetime(2026, 4, 21, 3, 40, 14, tzinfo=timezone(timedelta(hours=2)))
    assert instant(aware) == "2026-04-21T01:40:14Z"


def test_datetime_query_uses_the_standard_parameter_name():
    assert datetime_query(date(2026, 4, 21)) == {"datetime": "2026-04-21T00:00:00Z"}


def test_cql2_filter_covers_the_open_upper_bound():
    # The null test is load-bearing: an unbounded tstzrange upper bound means "still true
    # as far as we know", and without this clause every current label vanishes.
    text = cql2_filter(date(2026, 4, 21))
    assert "\"valid_from\" <= '2026-04-21T00:00:00Z'" in text
    assert '"valid_to" IS NULL' in text
    assert "\"valid_to\" > '2026-04-21T00:00:00Z'" in text


def test_cql2_filter_is_a_qgis_expression_not_literal_cql2():
    # Regression. The provider's `filter` URI parameter is parsed with QgsExpression and
    # compiled to CQL2 by QGIS; literal CQL2 does not merely get ignored, it makes the
    # QgsVectorLayer INVALID. TIMESTAMP() is valid CQL2 and is not a QGIS expression
    # function -- QGIS 3.44 answers "Function TIMESTAMP is not known" and refuses the
    # layer, so the analyst gets no data at all. Verified against QGIS 3.44.13.
    text = cql2_filter(date(2026, 4, 21))
    assert "TIMESTAMP(" not in text
    # to_datetime() parses but does not compile to CQL2, which silently downgrades the
    # filter to client-side evaluation and downloads the whole collection.
    assert "to_datetime(" not in text


def test_cql2_filter_honours_server_supplied_field_names():
    fields = CoreFields().merged({"valid_from": "vf", "valid_to": "vt"})
    text = cql2_filter(date(2026, 4, 21), fields)
    assert '"vf" <=' in text
    assert '"vt" IS NULL' in text
    assert "valid_from" not in text


def test_mechanism_parsing_falls_back_to_the_standard():
    assert AsOfMechanism.parse("cql2") is AsOfMechanism.CQL2
    assert AsOfMechanism.parse("datetime") is AsOfMechanism.DATETIME
    assert AsOfMechanism.parse("nonsense") is AsOfMechanism.DATETIME
    assert AsOfMechanism.parse(None) is AsOfMechanism.DATETIME


def test_describe_states_which_time_axis_it_is():
    assert "valid time" in describe(date(2026, 4, 21), AsOfMechanism.DATETIME)
    assert "off" in describe(None, AsOfMechanism.DATETIME)
