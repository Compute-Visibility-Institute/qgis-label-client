"""URI construction. A wrong URI is a silently empty layer, so this is tested hard."""

from __future__ import annotations

import pytest

from qgis_label_client.core.uri import (
    build_oapif_uri,
    encode_datasource_uri,
    encode_uri_value,
    header_params,
)


def test_values_are_always_quoted():
    assert encode_uri_value("simple") == "'simple'"


def test_quotes_and_backslashes_are_escaped():
    assert encode_uri_value("it's") == r"'it\'s'"
    assert encode_uri_value("a\\b") == r"'a\\b'"


def test_url_query_survives_quoting():
    # The whole reason values are always quoted: an unquoted & would end the value.
    value = "https://host/oapif?datetime=2026-04-21T00:00:00Z&x=1"
    assert encode_uri_value(value) == f"'{value}'"


def test_none_values_are_dropped():
    assert encode_datasource_uri({"a": "1", "b": None}) == "a='1'"


def test_booleans_become_one_and_zero():
    assert encode_datasource_uri({"a": True, "b": False}) == "a='1' b='0'"


def test_minimal_oapif_uri():
    uri = build_oapif_uri(landing_url="https://host/oapif", collection_id="label")
    assert "url='https://host/oapif'" in uri
    assert "typename='label'" in uri
    assert "restrictToRequestBBOX='1'" in uri
    assert "authcfg" not in uri
    assert "pageSize" not in uri
    assert "filter" not in uri


def test_authcfg_is_a_reference_not_a_token():
    uri = build_oapif_uri(
        landing_url="https://host/oapif", collection_id="label", authcfg="a1b2c3d"
    )
    assert "authcfg='a1b2c3d'" in uri


def test_page_size_zero_is_omitted_rather_than_sent_as_zero():
    uri = build_oapif_uri(landing_url="https://host", collection_id="label", page_size=0)
    assert "pageSize" not in uri


def test_cql_filter_is_included_verbatim():
    uri = build_oapif_uri(
        landing_url="https://host",
        collection_id="label",
        cql_filter="valid_to IS NULL",
    )
    assert "filter='valid_to IS NULL'" in uri


def test_collection_id_is_required():
    with pytest.raises(ValueError):
        build_oapif_uri(landing_url="https://host", collection_id="")


def test_two_http_headers_coexist_in_one_uri():
    """The property the header-versus-query decision turns on.

    Two things now have to ride on every request the provider makes: which history track
    this is, and -- on a historical layer -- which transaction-time instant. Both are
    ``http-header:`` parameters, one per header, and they live in each layer's OWN data
    source. That is what lets a live layer and a historical one share a connection, and two
    historical layers at different instants coexist in one project.
    """
    uri = build_oapif_uri(
        landing_url="https://host/oapif",
        collection_id="label_asof",
        headers={"X-Track": "some_track", "X-Recorded-At": "2026-01-15T08:00:00Z"},
    )
    assert "http-header:X-Track='some_track'" in uri
    assert "http-header:X-Recorded-At='2026-01-15T08:00:00Z'" in uri


def test_a_blank_header_value_is_dropped_rather_than_sent_empty():
    # A blank X-Recorded-At is not "no instant", it is a value the edge has to decide what
    # to do with. Same rule for both headers, in one place.
    params = header_params({"X-Track": "some_track", "X-Recorded-At": ""})
    assert params == {"http-header:X-Track": "some_track"}
