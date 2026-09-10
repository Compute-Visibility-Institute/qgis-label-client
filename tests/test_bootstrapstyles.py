"""Initial styling respects the server's decision and carries it into new layers."""

import pytest
from snapshot_fixtures import REGISTRY

from qgis_label_client import client
from qgis_label_client.core import bootstrapstyles
from qgis_label_client.core.errors import BackendError


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        {},
        {"bootstrap_style": {}},
        {"bootstrap_style": {"path": "https://evil.example/x"}},
    ],
)
def test_unknown_style_capability_does_not_enable_writes(document):
    assert not bootstrapstyles.supported(document)


@pytest.mark.parametrize("path", [bootstrapstyles.PATH, "/" + bootstrapstyles.PATH])
def test_recognizes_supported_init_endpoint(path):
    assert bootstrapstyles.supported({"bootstrap_style": {"path": path}})


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"class_id": "wrong", "status": "initialized", "style": {}},
        {"class_id": "compound", "status": "saved", "style": {}},
        {"class_id": "compound", "status": "initialized", "style": []},
    ],
)
def test_unconfirmed_style_result_is_not_credited(payload):
    with pytest.raises(BackendError):
        bootstrapstyles.parse_result(payload, "compound")


def test_authoritative_style_updates_registry_without_mutating_original():
    original = REGISTRY.get("compound").style
    result = bootstrapstyles.StyleResult("compound", "initialized", {"fill": "#95ff00"})
    updated = bootstrapstyles.update_registry(REGISTRY, [result])
    assert updated.get("compound").style == {"fill": "#95ff00"}
    assert REGISTRY.get("compound").style == original
    assert updated.fields == REGISTRY.fields
    assert updated.source_url == REGISTRY.source_url
    assert updated.get("datacenter_building") is REGISTRY.get("datacenter_building")


def test_style_post_carries_track_current_style_and_only_captured_properties(monkeypatch):
    calls = []
    monkeypatch.setattr(client, "post_json", lambda *args, **kwargs: calls.append((args, kwargs)))
    client.initialize_bootstrap_style(
        "https://api.example/oapif",
        "a/b",
        {"fill": "#95ff00"},
        {"fill": "blue", "min_zoom": 5},
        "auth-id",
        track="dev",
    )
    args, kwargs = calls[0]
    assert args[0] == "https://api.example/oapif/v1/classes/a%2Fb/bootstrap-style"
    assert args[1] == {
        "style": {"fill": "#95ff00"},
        "expected_style": {"fill": "blue", "min_zoom": 5},
    }
    assert kwargs["track"] == "dev"
    assert kwargs["authcfg"] == "auth-id"


def test_explicit_registry_fetch_bypasses_server_registry_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(
        client,
        "request_json",
        lambda url, **kwargs: calls.append(url) or {"classes": [{"class_id": "compound"}]},
    )
    registry = client.fetch_registry("https://api.example", "v1/classes", "auth-id")
    assert calls == ["https://api.example/v1/classes?refresh=true"]
    assert len(registry) == 1
