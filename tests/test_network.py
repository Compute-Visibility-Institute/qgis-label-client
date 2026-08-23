"""What the plugin says about a failed request, and what it does with a 429.

Both are pure functions of a status and a body, and both are load-bearing for the
bootstrap publish. The feature service does not catch the database trigger's exception, so
a row the class schema refuses comes back as a framework's default HTML 500: the message
the operator reads has to name that as the likely cause, and the body has to be reduced to
something a report can hold. A 429 has to survive as a *fact* -- status and Retry-After --
rather than only as prose, because the publish loop has to tell "not yet" from "no".
"""

from __future__ import annotations

from qgis_label_client.core.errors import BackendError
from qgis_label_client.network import _body_detail, _describe_status

FLASK_500 = (
    b"<!doctype html>\n<html lang=en>\n<title>500 Internal Server Error</title>\n"
    b"<h1>Internal Server Error</h1>\n<p>The server encountered an internal error and was "
    b"unable to complete your request.</p>\n"
)


def test_an_html_error_page_is_reduced_to_what_it_actually_says():
    # Rendering 300 characters of markup where the useful line should be is how a report
    # stops being read.
    detail = _body_detail(FLASK_500)
    assert "<h1>" not in detail
    assert "500 Internal Server Error" in detail
    assert "carries no detail" in detail


def test_a_json_error_body_is_kept_because_it_says_something():
    detail = _body_detail(b'{"code":"InvalidParameterValue","description":"bad bbox"}')
    assert "bad bbox" in detail


def test_a_server_error_names_the_cause_that_actually_produces_one_on_a_write():
    # The trigger enforces the class's JSON Schema, the geometry type and ST_IsValid, and
    # the feature service reports the exception as a bare 500. Naming only the cold start
    # sends the reader to the deployment logs for a problem that is in their data.
    message = _describe_status(500, "https://api.example.org/oapif/collections/x/items", FLASK_500)
    assert "database refusing the row" in message
    assert "cold start" in message


def test_a_throttle_is_reported_as_a_wait_not_a_refusal():
    assert "Rate limited" in _describe_status(429, "https://api.example.org/oapif", b"")


def test_a_backend_error_carries_the_status_and_the_wait_the_server_asked_for():
    # The publish loop decides on these: a 429 is "not yet" and is waited out, everything
    # else is a refusal of the content and is never re-sent.
    throttled = BackendError("HTTP 429", status=429, retry_after=2.5)
    assert throttled.throttled is True
    assert throttled.retry_after == 2.5

    refused = BackendError("HTTP 422")
    assert refused.throttled is False
    assert refused.status is None
    assert refused.retry_after is None
