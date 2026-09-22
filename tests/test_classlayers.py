"""Capability negotiation, native routing and editable discovered attributes."""

from types import SimpleNamespace

import pytest

from qgis_label_client import client, layers
from qgis_label_client.core import classlayers
from qgis_label_client.core.collections import Collection, group_by_mode
from qgis_label_client.core.errors import BackendError
from qgis_label_client.core.tracks import Track
from qgis_label_client.settings import PluginSettings


def entry(family="polygon", geometry="Polygon"):
    return {
        "id": f"cl_alpha__{family}",
        "class_id": "alpha",
        "class_name": "Alpha",
        "title": f"Alpha — {family}",
        "geometry_type": geometry,
        "fields": [
            {"name": "class_id", "read_only": True},
            {"name": "revision", "read_only": True},
            {
                "name": "src_4e616d655f4368",
                "source_name": "Name_Ch",
                "type": "string",
                "read_only": False,
            },
            {"name": "attr_706f776572", "title": "Power", "type": "number", "read_only": False},
        ],
    }


def manifest(*entries):
    return {"enabled": True, "version": 1, "collections": list(entries or [entry()])}


@pytest.mark.parametrize("document", [{"enabled": False}, manifest()])
def test_server_capability_controls_class_discovery(monkeypatch, document):
    seen = []
    monkeypatch.setattr(
        client, "request_json", lambda url, **kwargs: seen.append((url, kwargs)) or document
    )
    result = client.fetch_class_layers("https://example.org/api", "AUTH123", track="dev")
    assert len(result) == (1 if document["enabled"] else 0)
    assert seen[0][0] == "https://example.org/api/v1/class-layers"
    assert seen[0][1]["track"] == "dev"


@pytest.mark.parametrize("status", [401, 403, 500, None])
def test_discovery_failure_never_silently_downgrades(monkeypatch, status):
    def fail(*args, **kwargs):
        raise BackendError("Discovery failed", status=status)

    monkeypatch.setattr(client, "request_json", fail)
    with pytest.raises(BackendError):
        client.fetch_class_layers("https://example.org", "AUTH123")


def test_old_server_404_keeps_existing_collections(monkeypatch):
    def missing(*args, **kwargs):
        raise BackendError("Missing", status=404)

    monkeypatch.setattr(client, "request_json", missing)
    assert client.fetch_class_layers("https://example.org", "AUTH123") == []
    original = [Collection("label_polygon", "Labels")]
    assert classlayers.visible_collections(original) == original


@pytest.mark.parametrize(
    "document", [{}, None, manifest(entry(), entry()), manifest({**entry(), "class_id": "other"})]
)
def test_bad_manifest_is_not_a_legacy_server(document):
    with pytest.raises(BackendError):
        classlayers.parse_manifest(document)


def test_all_geometries_group_by_class_and_keep_separate_providers():
    collections = classlayers.parse_manifest(
        manifest(
            entry("point", "Point"),
            entry("line", "LineString"),
            entry(),
        )
    )
    (group,) = group_by_mode(collections)
    assert group.display_name == "Alpha"
    assert len(group.members) == 3
    assert len(set(group.collection_ids)) == 3


def test_discovery_hides_old_editable_rows_only_in_loading_ui():
    legacy = Collection("labels_polygon", "Old editable")
    history = Collection("history_polygon", "History")
    (fresh,) = classlayers.parse_manifest(manifest())
    all_collections = [legacy, history, fresh]
    capability = SimpleNamespace(serves=lambda name: name == "labels_polygon")
    assert classlayers.visible_collections(all_collections, capability) == [fresh]
    assert all_collections == [legacy, history, fresh]


def test_metadata_and_historical_collections_are_not_main_or_reference_options():
    all_collections = [
        Collection(name, name)
        for name in (
            "label_polygon",
            "label_asof_polygon",
            "label_history",
            "label_class",
            "capture",
            "label_current_polygon",
            "labeled_extent",
        )
    ]
    assert [c.collection_id for c in classlayers.visible_collections(all_collections)] == [
        "label_polygon"
    ]
    assert classlayers.reference_collections(all_collections) == []


def test_native_class_uri_keeps_auth_track_and_relative_api_prefix():
    settings = PluginSettings()
    settings.set("api_base_url", "https://example.org/api")
    settings.set_authcfg_by_track({"dev": "TRACK01"})
    uri = layers.build_layer_uri(settings, "cl_alpha__polygon", None, Track("dev"))
    assert "url='https://example.org/api/class-layers?track=dev'" in uri
    assert "authcfg='TRACK01'" in uri
    assert "typename='cl_alpha__polygon'" in uri
    old_uri = layers.build_layer_uri(settings, "labels_polygon", None, Track("default"))
    assert "url='https://example.org/api?track=default'" in old_uri
    assert "/class-layers" not in old_uri


def test_pending_reads_use_same_class_root_as_native_provider(monkeypatch):
    seen = []
    monkeypatch.setattr(client, "request_json", lambda url, **kwargs: seen.append(url) or {})
    client.fetch_features(
        "https://example.org/api", "cl_alpha__line", "AUTH123", {"label_id": "abc"}, track="dev"
    )
    assert seen == [
        "https://example.org/api/class-layers/collections/cl_alpha__line/items?label_id=abc"
    ]


def test_original_fields_editable_identity_locked_and_new_class_defaulted():
    metadata = entry()
    names = [field["name"] for field in metadata["fields"]]
    aliases, read_only, defaults, props = {}, {}, {}, {}
    fields = SimpleNamespace(indexOf=lambda name: names.index(name) if name in names else -1)
    config = SimpleNamespace(
        setReadOnly=lambda index, value: read_only.update({names[index]: value})
    )
    layer = SimpleNamespace(
        fields=lambda: fields,
        editFormConfig=lambda: config,
        setEditFormConfig=lambda cfg: None,
        setCustomProperty=lambda key, value: props.update({key: value}),
        customProperty=lambda key, default: props.get(key, default),
        setFieldAlias=lambda index, value: aliases.update({names[index]: value}),
        setEditorWidgetSetup=lambda index, widget: None,
        setDefaultValueDefinition=lambda index, value: defaults.update({names[index]: value}),
    )
    layers.configure_class_columns(layer, metadata)
    assert read_only["class_id"] and read_only["revision"]
    assert not read_only["src_4e616d655f4368"]
    assert not read_only["attr_706f776572"]
    assert aliases["src_4e616d655f4368"] == "Name_Ch"
    assert aliases["attr_706f776572"] == "Power"
    assert "class_id" in defaults
    assert layers.class_layer_metadata(layer) == metadata


@pytest.mark.parametrize(
    "dirty,pending,editing,expected",
    [
        (False, "", True, ["provider", "extents", "paint"]),
        (False, "", False, ["reload", "paint"]),
        (True, "", True, []),
        (False, "uncertain", True, []),
    ],
)
def test_commit_refresh_gets_new_revision_ids_without_discarding_edits(
    monkeypatch, dirty, pending, editing, expected
):
    events = []
    monkeypatch.setattr(layers, "class_layer_metadata", lambda layer: entry())
    layer = SimpleNamespace(
        isModified=lambda: dirty,
        isEditable=lambda: editing,
        customProperty=lambda key, default: pending,
        dataProvider=lambda: SimpleNamespace(reloadData=lambda: events.append("provider")),
        updateExtents=lambda: events.append("extents"),
        reload=lambda: events.append("reload"),
        triggerRepaint=lambda: events.append("paint"),
    )
    assert layers.refresh_class_layer_after_commit(layer) is bool(expected)
    assert events == expected
