"""Backend URL assembly."""

from __future__ import annotations

import pytest

from qgis_label_client.core.errors import ConfigurationError
from qgis_label_client.core.urls import (
    collections_url,
    items_url,
    join_path,
    normalise_base_url,
    with_query,
)


def test_trailing_slashes_are_stripped():
    assert normalise_base_url("https://host/oapif/") == "https://host/oapif"


def test_query_and_fragment_are_discarded_from_the_base():
    assert normalise_base_url("https://host/oapif?a=1#x") == "https://host/oapif"


@pytest.mark.parametrize("bad", ["", "   ", "host/oapif", "ftp://host", "/oapif"])
def test_non_absolute_urls_are_rejected(bad):
    with pytest.raises(ConfigurationError):
        normalise_base_url(bad)


def test_deployment_path_prefix_is_preserved():
    # urljoin would discard '/oapif' here. Doing so would 404 in a way that looks like a
    # backend outage, which is why join_path exists at all.
    assert join_path("https://host/oapif", "/collections") == "https://host/oapif/collections"


def test_multi_segment_paths():
    assert items_url("https://host/oapif", "label") == "https://host/oapif/collections/label/items"


def test_collection_ids_are_percent_encoded():
    assert "label%2Fone" in items_url("https://host", "label/one")


def test_collections_url():
    assert collections_url("https://host") == "https://host/collections"


def test_with_query_merges_rather_than_replaces():
    result = with_query("https://host/items?limit=10", {"datetime": "2026-04-21T00:00:00Z"})
    assert "limit=10" in result
    assert "datetime=2026-04-21T00%3A00%3A00Z" in result


def test_with_query_drops_none_and_renders_booleans_for_ogc():
    result = with_query("https://host/items", {"a": None, "b": True, "c": False})
    assert "a=" not in result
    assert "b=true" in result
    assert "c=false" in result
