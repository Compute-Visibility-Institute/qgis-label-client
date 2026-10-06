"""A refused write is said as what is wrong with the data and what to do about it."""

import pytest

from qgis_label_client.core.refusals import describe, explain, is_refusal

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
    message = describe(PRODUCTION_SELF_INTERSECTION)
    assert message == (
        "The server refused a shape because its outline crosses itself near "
        "45.678901° N, 12.345679° E. Find it with Vector ▸ Geometry Tools ▸ Check "
        "Validity, fix it (or run the Fix geometries tool), then Save again."
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


def test_a_structural_refusal_under_the_same_code_keeps_its_own_sentence():
    payload = {
        "code": "GeometryInvalid",
        "description": (
            "the feature: the ring at coordinates[0] has 3 position(s). A closed ring needs "
            "at least four — three corners and a repeat of the first."
        ),
    }
    message = describe(payload)
    assert message.startswith("the feature: the ring at coordinates[0] has 3 position(s).")
    assert "because it is invalid" not in message
    assert message.endswith("then Save again.")


def test_a_validation_list_is_said_as_its_messages():
    payload = {
        "detail": [
            {"loc": ["body", "reason"], "msg": "Field required", "type": "missing"},
            {"loc": ["body", "item"], "msg": "Input should be an object"},
        ]
    }
    assert describe(payload) == "Field required; Input should be an object."


def test_other_refusals_keep_whole_sentences_and_gain_their_fix():
    long = "The layer is not in WGS84 degrees. " + "It was projected. " * 40
    message = describe({"code": "GeometryWrongCrs", "description": long})
    assert message.startswith("The layer is not in WGS84 degrees.")
    assert message.endswith("Reproject the layer to EPSG:4326, then Save again.")
    assert "projected. Reproject the layer" in message
    assert len(message) < 500


def test_a_fix_the_server_already_gave_is_not_repeated():
    payload = {
        "code": "GeometryWrongCrs",
        "description": (
            "the feature: coordinates[0] is [500000, 4500000], which is outside EPSG:4326. "
            "These look like projected metres — reproject the layer to EPSG:4326."
        ),
    }
    message = describe(payload)
    assert message.count("4326") == 2  # the server's two mentions, and none of ours
    assert message.endswith("Then Save again.")


def test_dropping_z_names_a_tool_that_exists():
    payload = {"code": "GeometryHasZ", "description": "the feature carries a third ordinate."}
    assert describe(payload).endswith(
        "In QGIS: Processing Toolbox ▸ Vector geometry ▸ Drop M/Z values, then Save again."
    )


def test_a_description_without_a_full_stop_still_ends_as_a_sentence():
    assert describe({"detail": "ID token could not be verified"}) == (
        "ID token could not be verified."
    )


@pytest.mark.parametrize(
    "status,payload,expected",
    [
        (401, {"detail": "ID token could not be verified"}, "Sign in again from the CVI panel"),
        (
            403,
            {"detail": "analyst@example.org is not on the access list"},
            "is not on the access list. Your account may not",
        ),
        (429, {}, "The server is busy."),
        (422, PRODUCTION_SELF_INTERSECTION, "its outline crosses itself"),
        (500, {}, "fallback text."),
    ],
)
def test_explain_gives_access_advice_for_access_refusals(status, payload, expected):
    assert expected in explain(status, payload, "fallback text")


@pytest.mark.parametrize("payload", [None, [], "text", {}, {"code": "X"}])
def test_nothing_usable_leaves_the_callers_wording(payload):
    assert describe(payload) is None


@pytest.mark.parametrize(
    "status,refused",
    [(400, True), (409, True), (422, True), (408, False), (500, False), (None, False)],
)
def test_only_an_answered_4xx_is_a_refusal(status, refused):
    assert is_refusal(status) is refused
