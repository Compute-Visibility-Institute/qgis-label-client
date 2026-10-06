"""A refused write is said as what is wrong with the data and what to do about it."""

import pytest

from qgis_label_client.core.refusals import describe, is_refusal

#: The shape of the refusal the API sends for a self-intersecting polygon.
PRODUCTION_SELF_INTERSECTION = {
    "code": "GeometryInvalid",
    "description": (
        "the feature is not a valid geometry: Self-intersection at "
        "POINT(12.34567891234567 45.67890123456). This is the same check "
        "app.label_check() makes on the way in, asked one step earlier so the answer "
        "reaches you instead of a log file. Use QGIS's Vector ▸ Geometry Tools ▸ Check "
        "Validity, or Fix Geometries, and redraw the offending part."
    ),
}


def test_a_self_intersection_names_the_problem_the_place_and_the_fix():
    message = describe(PRODUCTION_SELF_INTERSECTION, 422)
    assert message == (
        "The server refused a shape because its outline crosses itself near "
        "45.678901° N, 12.345679° E. Find it with Vector ▸ Geometry Tools ▸ Check "
        "Validity, fix it (or run Fix Geometries), then Save again."
    )
    assert "app.label_check" not in message
    assert "POINT(" not in message


def test_structured_reason_and_location_win_over_the_sentence():
    payload = {
        "code": "GeometryInvalid",
        "description": "features[2] is not a valid geometry: something else.",
        "reason": "Hole lies outside shell",
        "location": [-70.5, -33.25],
    }
    assert describe(payload).startswith(
        "The server refused a shape because a hole lies outside its outer ring near "
        "33.250000° S, 70.500000° W."
    )


def test_an_unknown_geometry_reason_is_passed_through_and_a_missing_place_is_omitted():
    payload = {"code": "GeometryInvalid", "description": "x is not a valid geometry: Spike."}
    assert describe(payload).startswith("The server refused a shape because Spike. Find it")
    assert "near" not in describe(payload)


def test_other_refusals_keep_whole_sentences_and_gain_their_fix():
    long = "The layer is not in WGS84 degrees. " + "It was projected. " * 40
    message = describe({"code": "GeometryWrongCrs", "description": long})
    assert message.startswith("The layer is not in WGS84 degrees.")
    assert message.endswith("Reproject the layer to EPSG:4326, then Save again.")
    assert "projected. Reproject the layer" in message
    assert len(message) < 500


@pytest.mark.parametrize("payload", [None, [], "text", {}, {"code": "X"}])
def test_nothing_usable_leaves_the_callers_wording(payload):
    assert describe(payload) is None


@pytest.mark.parametrize(
    "status,refused",
    [(400, True), (409, True), (422, True), (408, False), (500, False), (None, False)],
)
def test_only_an_answered_4xx_is_a_refusal(status, refused):
    assert is_refusal(status) is refused
