"""Read-only class views must coexist with editable copies without changing them."""

from types import SimpleNamespace

import pytest

from qgis_label_client import layers, layertree
from qgis_label_client.core.collections import Collection, CollectionGroup
from qgis_label_client.core.registry import parse_registry
from qgis_label_client.core.tracks import Track
from qgis_label_client.plugin import LabelClientPlugin


class Layer:
    def __init__(self, title, collection_id="cl_alpha__polygon", track="dev", readonly=False):
        self.title = title
        self.properties = {
            layers.COLLECTION_PROPERTY: collection_id,
            layers.TRACK_PROPERTY: track,
            "cvi/read_only_view": readonly,
        }
        self.readonly = readonly
        self.backend = "https://api.example.org"
        self.edit_buffer = ["an existing local edit"]

    def customProperty(self, key, default=""):  # noqa: N802
        return self.properties.get(key, default)

    def setCustomProperty(self, key, value):  # noqa: N802
        self.properties[key] = value

    def setReadOnly(self, value):  # noqa: N802
        self.readonly = value

    def source(self):
        return self.backend + "/class-layers?track=dev"


@pytest.fixture
def loading(fake_iface, monkeypatch):
    plugin = LabelClientPlugin(fake_iface)
    plugin.dock = SimpleNamespace(set_status=lambda text: None)
    plugin.settings.set("api_base_url", "https://api.example.org")
    plugin.settings.set("track", "dev")
    plugin.tracks = [Track("dev")]
    plugin.registry = parse_registry({"classes": [{"class_id": "alpha"}]})
    plugin.collections = [
        Collection(
            "cl_alpha__polygon",
            "Alpha",
            transactional=True,
            class_layer={
                "class_id": "alpha",
                "class_name": "Alpha",
                "fields": [
                    {"name": "src_4e616d655f4368", "source_name": "Name_Ch", "read_only": False}
                ],
            },
        )
    ]
    plugin.activities = SimpleNamespace(begin=lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(plugin, "_defer_until_fresh", lambda callback: False)
    monkeypatch.setattr(plugin, "_apply_access", lambda: None)
    monkeypatch.setattr(plugin, "_refresh_track_banner", lambda: None)
    monkeypatch.setattr(plugin, "_warn_on_track_mismatch", lambda *args: None)
    monkeypatch.setattr(plugin, "_refuse_unpinned_historical", lambda *args: False)
    project_layers = []
    monkeypatch.setattr(layers, "live_layers", lambda: project_layers)
    monkeypatch.setattr(layers, "belongs_to_backend", lambda layer, url: layer.backend == url)
    monkeypatch.setattr(layers, "apply_registry", lambda *args: None)
    plugin.test_configurations = []
    monkeypatch.setattr(
        layers,
        "configure_class_columns",
        lambda layer, metadata: plugin.test_configurations.append((layer, metadata)),
    )
    monkeypatch.setattr(
        layers,
        "create_layer",
        lambda settings, cid, title, registry, track, **kwargs: Layer(title, cid, track.name),
    )
    monkeypatch.setattr(
        layertree,
        "new_import_group",
        lambda project, track, caption: object(),
    )
    monkeypatch.setattr(
        layertree,
        "add_collection_layer",
        lambda project, layer, groups, **kwargs: project_layers.append(layer),
    )
    return plugin, project_layers


def test_readonly_signal_and_editable_signal_keep_distinct_modes(fake_iface, monkeypatch):
    plugin = LabelClientPlugin(fake_iface)
    calls = []
    monkeypatch.setattr(
        plugin, "load_collections", lambda ids, **kwargs: calls.append((ids, kwargs))
    )
    plugin.initGui()
    try:
        plugin.dock.loadReadOnlyLayersRequested.emit(["cl_alpha__polygon"])
        plugin.dock.loadLayersRequested.emit(["cl_alpha__polygon"])
        assert calls == [
            (["cl_alpha__polygon"], {"read_only": True}),
            (["cl_alpha__polygon"], {}),
        ]
    finally:
        plugin.unload()


@pytest.mark.parametrize("first_readonly", [False, True])
def test_mode_buttons_create_separate_copies_without_touching_existing_edits(
    loading, first_readonly
):
    plugin, project_layers = loading
    plugin.load_collections(["cl_alpha__polygon"], read_only=first_readonly)
    original = project_layers[0]
    plugin.load_collections(["cl_alpha__polygon"], read_only=not first_readonly)
    assert len(project_layers) == 2
    assert original.readonly is first_readonly
    assert original.edit_buffer == ["an existing local edit"]
    assert project_layers[1].readonly is not first_readonly
    assert next(layer for layer in project_layers if layer.readonly).title == "Alpha (read only)"
    assert [layer for layer, _ in plugin.test_configurations] == project_layers
    assert all(
        metadata["fields"][0]["source_name"] == "Name_Ch"
        for _, metadata in plugin.test_configurations
    )
    for read_only in (False, True):
        plugin.load_collections(["cl_alpha__polygon"], read_only=read_only)
    assert len(project_layers) == 2


@pytest.mark.parametrize("other_context", ["backend", "track"])
def test_another_connection_or_track_does_not_block_loading_current_mode(loading, other_context):
    plugin, project_layers = loading
    other = Layer("Other", readonly=True)
    if other_context == "backend":
        other.backend = "https://other.example.org"
    else:
        other.properties[layers.TRACK_PROPERTY] = "default"
    project_layers.append(other)
    plugin.load_collections(["cl_alpha__polygon"], read_only=True)
    assert len(project_layers) == 2
    assert other.edit_buffer == ["an existing local edit"]


def test_old_current_collection_stays_readonly_even_without_new_mode_flag(loading):
    plugin, project_layers = loading
    plugin.collections = [
        Collection("label_current_polygon", "Current labels", transactional=False)
    ]
    plugin.load_collections(["label_current_polygon"])
    assert project_layers[0].readonly
    plugin.load_collections(["label_current_polygon"], read_only=True)
    assert len(project_layers) == 1


class Group:
    def __init__(self, title):
        self.title = title
        self.properties = {}

    def name(self):
        return self.title

    def setName(self, title):  # noqa: N802
        self.title = title

    def customProperty(self, key, default=""):  # noqa: N802
        return self.properties.get(key, default)

    def setCustomProperty(self, key, value):  # noqa: N802
        self.properties[key] = value

    def setExpanded(self, value):  # noqa: N802
        pass

    def findLayers(self):  # noqa: N802
        return []


def test_tree_group_identity_separates_modes_and_labels_readonly_copy(monkeypatch):
    monkeypatch.setattr(
        layertree, "QgsDataSourceUri", lambda source: SimpleNamespace(param=lambda key: source)
    )
    groups = []

    def insert(index, title):
        group = Group(title)
        groups.insert(index, group)
        return group

    root = SimpleNamespace(findGroups=lambda recursive=True: groups, insertGroup=insert)
    metadata = CollectionGroup(
        "cl_alpha_",
        "Alpha",
        (
            Collection("cl_alpha__polygon", "Alpha polygons", class_layer={"class_id": "alpha"}),
            Collection("cl_alpha__line", "Alpha lines", class_layer={"class_id": "alpha"}),
        ),
    )
    editable = Layer("Editable")
    readonly = Layer("Readonly", readonly=True)
    editing_group = layertree._ensure_group(root, metadata, layertree._context(editable))
    reading_group = layertree._ensure_group(
        root, metadata, layertree._context(readonly), read_only=True
    )
    assert editing_group is not reading_group
    assert editing_group.title == "CVI Alpha"
    assert reading_group.title == "CVI Alpha (read only)"
    assert layertree._ensure_group(root, metadata, layertree._context(editable)) is editing_group
