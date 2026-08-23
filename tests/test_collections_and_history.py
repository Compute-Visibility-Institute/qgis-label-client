"""Parsing the two backend documents the plugin reads directly."""

from __future__ import annotations

import pytest

from qgis_label_client.core.collections import parse_collections
from qgis_label_client.core.errors import BackendError
from qgis_label_client.core.fields import CoreFields
from qgis_label_client.core.history import parse_history

COLLECTIONS = {
    "collections": [
        {
            "id": "zeta",
            "title": "Zeta collection",
            "description": "Something",
            "extent": {
                "spatial": {"bbox": [[84.8, 23.0, 125.1, 46.9]]},
                "temporal": {"interval": [["2026-04-21T00:00:00Z", None]]},
            },
            "links": [{"rel": "http://www.opengis.net/def/rel/ogc/1.0/create-replace-delete"}],
        },
        {"id": "alpha"},
    ]
}


def test_collections_sort_by_display_name():
    parsed = parse_collections(COLLECTIONS)
    assert [c.collection_id for c in parsed] == ["alpha", "zeta"]


def test_a_collection_without_a_title_falls_back_to_its_id():
    alpha = parse_collections(COLLECTIONS)[0]
    assert alpha.display_name == "alpha"


def test_bbox_and_interval_are_extracted():
    zeta = parse_collections(COLLECTIONS)[1]
    assert zeta.bbox == (84.8, 23.0, 125.1, 46.9)
    assert zeta.temporal_interval == ("2026-04-21T00:00:00Z", None)


def test_a_six_element_bbox_keeps_the_horizontal_corners():
    document = {
        "collections": [{"id": "a", "extent": {"spatial": {"bbox": [[1, 2, 0, 3, 4, 100]]}}}]
    }
    assert parse_collections(document)[0].bbox == (1.0, 2.0, 3.0, 4.0)


def test_transactional_is_tri_state():
    parsed = {c.collection_id: c for c in parse_collections(COLLECTIONS)}
    assert parsed["zeta"].transactional is True
    # Unknown, not False: guessing read-only would hide the editing capability that is
    # the whole reason QGIS is the editing surface.
    assert parsed["alpha"].transactional is None


@pytest.mark.parametrize("document", [{}, {"collections": "x"}, "nope"])
def test_non_collections_documents_raise(document):
    with pytest.raises(BackendError):
        parse_collections(document)


# --- history ----------------------------------------------------------------

HISTORY = {
    "type": "FeatureCollection",
    "features": [
        {
            "id": 1,
            "properties": {
                "history_id": 1,
                "label_id": "uuid-1",
                "operation": "INSERT",
                "changed": ["created"],
                "actor": "someone@example.org",
                "recorded_from": "2026-01-02T00:00:00Z",
                "recorded_to": "2026-03-01T00:00:00Z",
                "names": {"zh": "示例", "en": "Example"},
            },
        },
        {
            "id": 2,
            "properties": {
                "history_id": 2,
                "label_id": "uuid-1",
                "operation": "UPDATE",
                "changed": "{geom,attrs}",
                "actor": "someone@example.org",
                "reason": "re-digitised from newer imagery",
                "recorded_from": "2026-03-01T00:00:00Z",
                "recorded_to": None,
                "names": {"en": "Example"},
            },
        },
    ],
}


def test_history_is_newest_belief_first():
    entries = parse_history(HISTORY)
    assert [e.history_id for e in entries] == [2, 1]


def test_the_open_transaction_range_is_the_current_belief():
    newest = parse_history(HISTORY)[0]
    assert newest.is_current_belief is True
    assert parse_history(HISTORY)[1].is_current_belief is False


def test_postgres_array_literals_are_understood():
    newest = parse_history(HISTORY)[0]
    assert newest.changed == ("geom", "attrs")
    assert newest.changed_summary() == "geom, attrs"


def test_name_summary_prefers_chinese():
    # 82.6% of compounds have a Chinese name and 8.9% an English one.
    oldest = parse_history(HISTORY)[1]
    assert oldest.name_summary() == "示例"


def test_name_summary_falls_back_when_there_is_no_chinese_name():
    newest = parse_history(HISTORY)[0]
    assert newest.name_summary() == "Example"


def test_history_honours_server_supplied_field_names():
    fields = CoreFields().merged({"label_id": "lid"})
    document = {"features": [{"properties": {"lid": "uuid-9", "operation": "INSERT"}}]}
    assert parse_history(document, fields)[0].label_id == "uuid-9"


@pytest.mark.parametrize("document", ["nope", {"nothing": 1}])
def test_unusable_history_documents_raise(document):
    with pytest.raises(BackendError):
        parse_history(document)
