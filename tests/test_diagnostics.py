"""What a copied error trace contains, and what it must never contain."""

from __future__ import annotations

import pytest
from qgis.core import Qgis

from qgis_label_client import diagnostics
from qgis_label_client.log import LOG_TAG


@pytest.fixture(autouse=True)
def _fresh():
    diagnostics.reset()
    yield
    diagnostics.reset()


def test_a_trace_built_while_handling_an_exception_carries_its_traceback():
    try:
        raise ValueError("feature refused")
    except ValueError:
        trace = diagnostics.build_trace("Save failed", context=(("Track", "dev"),))
    assert "Track: dev" in trace
    assert "Message:\nSave failed" in trace
    assert "ValueError: feature refused" in trace
    assert "Exception:" not in diagnostics.build_trace("No exception in scope")


def test_request_lines_carry_the_request_id_but_never_a_query_string():
    diagnostics.record_request(
        "POST", "https://api.example.org/items?token=secret", 500, "abc123", "server error"
    )
    trace = diagnostics.build_trace("Save failed")
    assert "POST https://api.example.org/items?<signature redacted> -> 500" in trace
    assert "request-id=abc123 (server error)" in trace
    assert "secret" not in trace


def test_a_later_trace_still_names_the_error_shown_before_it():
    assert "SHOWN First failure" not in diagnostics.build_trace("First failure")
    later = diagnostics.build_trace("Copied", shown=False)
    assert "SHOWN First failure" in later
    assert "SHOWN Copied" not in diagnostics.build_trace("Again", shown=False)


def test_the_log_keeps_this_plugins_lines_and_other_components_problems_only():
    diagnostics.record_log("Plugin loaded.", LOG_TAG, Qgis.MessageLevel.Info)
    diagnostics.record_log("Rendering took 3 ms", "Rendering", Qgis.MessageLevel.Info)
    diagnostics.record_log("Commit errors: 1 feature(s) not added", "", Qgis.MessageLevel.Warning)
    trace = diagnostics.build_trace("Save failed")
    assert f"INFO [{LOG_TAG}] Plugin loaded." in trace
    assert "Rendering took" not in trace
    assert "WARNING [General] Commit errors" in trace


def test_a_broken_context_still_yields_the_message():
    def broken():
        raise RuntimeError("layer already deleted")

    trace = diagnostics.trace_for("Save failed", "details", broken)
    assert trace.startswith("Save failed")
    assert "layer already deleted" in trace
