"""Incremental uploads must distinguish existing, new, and uncertain source features."""

from copy import deepcopy
from threading import Event
from types import SimpleNamespace

import pytest

from qgis_label_client.core.errors import ConfigurationError
from qgis_label_client.core.fields import DEFAULT_FIELDS
from qgis_label_client.core.pushlocal import UploadLedger, fingerprint, geometry_key
from qgis_label_client.pushlocal import ExistingFeatures


def feature(geometry=None, attrs=None):
    return {
        "type": "Feature",
        "geometry": geometry or {"type": "Point", "coordinates": [105.123456789123, 37.5]},
        "properties": {
            "class_id": "test",
            "names": {"en": "Example"},
            "attrs": attrs or {"power": 3},
        },
    }


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "Point", "coordinates": [1, 2]},
        {"type": "LineString", "coordinates": [[1, 2], [3, 4]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
    ],
)
def test_single_and_single_part_multi_geometries_compare_equal(geometry):
    multi = {"type": "Multi" + geometry["type"], "coordinates": [geometry["coordinates"]]}
    assert geometry_key(geometry) == geometry_key(multi)


def test_roundtrip_precision_generated_fields_and_numeric_json_do_not_duplicate():
    original = feature()
    stored = deepcopy(original)
    stored["geometry"]["coordinates"][0] = 105.123456789
    stored["properties"].update(
        {"label_id": "server-id", "updated_at": "2026-09-22", "track_id": "server-track"}
    )
    stored["properties"]["attrs"]["power"] = 3.0
    assert fingerprint(original, DEFAULT_FIELDS) == fingerprint(stored, DEFAULT_FIELDS)


def test_polygon_ring_start_and_direction_are_equivalent_but_directed_lines_are_not():
    ring = [[0, 0], [1, 0], [1, 1], [0, 0]]
    rotated = [[1, 0], [0, 0], [1, 1], [1, 0]]
    assert geometry_key({"type": "Polygon", "coordinates": [ring]}) == geometry_key(
        {"type": "Polygon", "coordinates": [rotated]}
    )
    assert geometry_key({"type": "LineString", "coordinates": ring}) != geometry_key(
        {"type": "LineString", "coordinates": list(reversed(ring))}
    )


def test_ledger_survives_interruption_and_scopes_every_destination(tmp_path):
    path = tmp_path / "uploads.sqlite"
    first = UploadLedger(path, "https://api.example.org", "owner@example.org", "alpha")
    first.reserve("labels", ["fingerprint"], "bootstrap run chunk 0", [("source:1", "fingerprint")])
    first.close()
    reopened = UploadLedger(path, "https://api.example.org", "owner@example.org", "alpha")
    assert reopened.state("labels", "fingerprint") == "uncertain"
    assert reopened.source_fingerprint("labels", "source:1") == "fingerprint"
    for backend, email, track in [
        ("https://other.example.org", "owner@example.org", "alpha"),
        ("https://api.example.org", "other@example.org", "alpha"),
        ("https://api.example.org", "owner@example.org", "beta"),
    ]:
        different = UploadLedger(path, backend, email, track)
        assert different.state("labels", "fingerprint") == ""
        different.close()
    reopened.finish("labels", ["fingerprint"], "not-created")
    assert reopened.state("labels", "fingerprint") == ""
    assert reopened.source_fingerprint("labels", "source:1") == ""
    reopened.close()


@pytest.fixture
def guard(tmp_path):
    ledger = UploadLedger(
        tmp_path / "uploads.sqlite", "https://api.example.org", "owner@example.org", "alpha"
    )
    request = SimpleNamespace(
        base_url="https://api.example.org",
        authcfg="authcfg",
        track="alpha",
        fields=DEFAULT_FIELDS,
        layers=[object()],
        target_for=lambda _: "labels",
    )
    active = Event()
    active.set()
    result = ExistingFeatures(request, ledger, active)
    result.keys["labels"], result.spatial["labels"], result.identities["labels"] = set(), set(), {}
    yield result
    ledger.close()


def test_matching_server_feature_is_observed_and_not_recreated_if_deleted(guard):
    draft = feature()
    key = fingerprint(draft, DEFAULT_FIELDS)
    guard.keys["labels"].add(key)
    assert guard.skip("labels", draft, {}, ("source", "1"))[0] == "present"
    guard.keys["labels"].clear()
    assert guard.skip("labels", draft, {}, ("source", "1"))[0] == "review"


def test_geometry_change_of_a_previously_uploaded_source_row_is_not_a_new_feature(guard):
    draft = feature()
    assert guard.skip("labels", draft, {}, ("source", "1")) == ("", "")
    guard.started("labels", [draft], "bootstrap chunk")
    guard.finished("labels", [draft], "confirmed")
    changed = deepcopy(draft)
    changed["geometry"]["coordinates"][0] += 1
    assert guard.skip("labels", changed, {}, ("source", "1"))[0] == "review"


def test_existing_server_identity_with_different_values_is_not_reported_as_pushed(guard):
    identity = "b164c48b-fc26-456c-8f27-4fbab10c3c5c"
    guard.identities["labels"][identity] = fingerprint(feature(), DEFAULT_FIELDS)
    changed = feature(attrs={"power": 4})
    assert guard.skip("labels", changed, {"label_id": identity})[0] == "review"


def test_uncertain_attempt_is_never_retried_even_under_a_new_source_id(guard):
    draft = feature()
    assert guard.skip("labels", draft, {}, ("source", "1")) == ("", "")
    guard.started("labels", [draft], "bootstrap chunk")
    guard.finished("labels", [draft], "uncertain")
    assert guard.skip("labels", draft, {}, ("another source", "2"))[0] == "review"


def test_same_geometry_with_different_attributes_needs_review(guard, monkeypatch):
    from qgis_label_client import pushlocal

    stored = feature()
    monkeypatch.setattr(
        pushlocal.client,
        "fetch_features",
        lambda *_args, **_kwargs: {"features": [stored], "numberMatched": 1},
    )
    guard.load(None)
    assert guard.skip("labels", feature(attrs={"power": 4}), {})[0] == "review"


def test_server_inventory_paginates_beyond_a_short_page_with_next_link(guard, monkeypatch):
    from qgis_label_client import pushlocal

    offsets = []
    first, second = feature(), feature({"type": "Point", "coordinates": [1, 2]})

    def fetch(_url, _collection, _auth, query, _feedback, **_kwargs):
        offsets.append(query["offset"])
        if query["offset"] == 0:
            return {
                "features": [first],
                "numberMatched": 2,
                "links": [{"rel": "next", "href": "ignored"}],
            }
        return {"features": [second], "numberMatched": 2}

    monkeypatch.setattr(pushlocal.client, "fetch_features", fetch)
    guard.load(None)
    assert offsets == [0, 1]
    assert guard.skip("labels", second, {})[0] == "present"


def test_context_change_blocks_even_reserved_uploads(guard):
    guard.active.clear()
    with pytest.raises(ConfigurationError, match="connection changed"):
        guard.started("labels", [feature()], "bootstrap chunk")
    assert guard.ledger.state("labels", fingerprint(feature(), DEFAULT_FIELDS)) == ""


@pytest.mark.parametrize(
    "verdict_state, expected",
    [
        ("created", "confirmed"),
        ("unknown", "uncertain"),
        ("not-created", ""),
    ],
)
def test_bulk_publisher_journals_before_network_and_keeps_uncertain_results(
    guard, monkeypatch, verdict_state, expected
):
    from qgis_label_client import publish
    from qgis_label_client.core import bulk
    from qgis_label_client.core.publish import LayerOutcome

    draft = feature()
    guard.skip("labels", draft, {}, ("source", "1"))
    guard.request.feature_guard = guard
    chunk = publish._Chunk(max_features=10, max_bytes=100000)
    chunk.add("test", draft, bulk.encoded_size(draft))
    outcome = LayerOutcome("local", "test")
    run = publish._BulkRun(SimpleNamespace(), "run")
    key = fingerprint(draft, DEFAULT_FIELDS)

    def send(*_args):
        assert guard.ledger.state("labels", key) == "uncertain"
        return bulk.ChunkVerdict(
            state=verdict_state, created=1 if verdict_state == bulk.CREATED else 0
        )

    monkeypatch.setattr(publish, "_post_chunk", send)
    publish._send_chunk(guard.request, "labels", chunk, outcome, run, None)
    assert guard.ledger.state("labels", key) == expected
