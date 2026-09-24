"""Provider policies that do not need a running QGIS application.

Native field/dialog/edit-buffer behavior still needs the existing QGIS harness
when validation is requested. These tests cover wire and retry boundaries.
"""

from types import FunctionType, SimpleNamespace

import pytest

from qgis_label_client import classprovider as provider
from qgis_label_client.core.errors import BackendError


def test_reregister_rebinds_retained_factory_without_replacing_live_providers(monkeypatch):
    class OldProvider:
        def __init__(self, *args):
            self.pending_edits = ["unsaved polygon"]

    old_globals = {"__name__": provider.__name__, "ClassLayerProvider": OldProvider}
    factory = FunctionType(provider.ClassProviderMetadata.createProvider.__code__, old_globals)
    metadata_type = type("RetainedMetadata", (), {"createProvider": factory})
    metadata = metadata_type()
    original = metadata.createProvider("saved-uri", None, None)
    registrations = []
    registry = SimpleNamespace(
        providerMetadata=lambda key: metadata,
        registerProvider=lambda value: registrations.append(value),
    )
    monkeypatch.setattr(provider, "QgsProviderRegistry", SimpleNamespace(instance=lambda: registry))
    # Model successive plugin reloads with distinct constructor objects.
    for generation in range(3):

        def constructor(*args, generation=generation):
            return generation, args

        monkeypatch.setattr(provider, "ClassLayerProvider", constructor)
        provider.register_provider()
        assert metadata.createProvider("new-uri", None, None) == (
            generation,
            ("new-uri", None, None),
        )
        assert provider._METADATA is metadata
        assert original.pending_edits == ["unsaved polygon"]
        assert isinstance(original, OldProvider)
    assert not registrations


@pytest.fixture
def provider_constructor(monkeypatch):
    """Exercise the real URI gate without network requests or a QGIS application."""
    loaded = []
    errors = []
    warnings = []
    monkeypatch.setattr(
        provider,
        "QgsDataSourceUri",
        lambda params: SimpleNamespace(
            param=lambda key: params.get(key, ""), authConfigId=lambda: "test-auth"
        ),
    )
    monkeypatch.setattr(
        provider, "QgsVectorLayer", lambda *args: SimpleNamespace(dataProvider=lambda: None)
    )

    def load(instance):
        loaded.append((instance._track, instance._collection_url, instance._read_only))
        instance._valid = True

    monkeypatch.setattr(provider.ClassLayerProvider, "_load", load)
    monkeypatch.setattr(
        provider.ClassLayerProvider,
        "pushError",
        lambda self, text: errors.append(text),
        raising=False,
    )
    monkeypatch.setattr(provider, "log_warning", warnings.append)
    return loaded, errors, warnings


@pytest.mark.parametrize("track", ["default", "dev", "another-track"])
@pytest.mark.parametrize("read_only", [False, True])
def test_class_provider_accepts_explicit_environments(provider_constructor, track, read_only):
    loaded, errors, _ = provider_constructor
    instance = provider.ClassLayerProvider(
        {
            "url": "https://api.example.org/class-layers?track=" + track,
            "typename": "cl_unclassified__polygon",
            "cviReadOnly": "1" if read_only else "0",
        }
    )
    assert instance.isValid()
    assert loaded == [
        (
            track,
            "https://api.example.org/class-layers/collections/cl_unclassified__polygon",
            read_only,
        )
    ]
    assert not errors


@pytest.mark.parametrize(
    "url,diagnostic",
    [
        ("https://api.example.org/class-layers", "No environment"),
        ("https://api.example.org/class-layers?track=", "No environment"),
        ("https://api.example.org/class-layers?track=%20", "No environment"),
        (
            "https://api.example.org/class-layers?track=default&recorded_at=2026-01-01",
            "Historical snapshots",
        ),
        ("/class-layers?track=dev", "HTTP(S) API URL"),
        ("file:///class-layers?track=dev", "HTTP(S) API URL"),
        ("https://api.example.org/collections?track=dev", "class-layer API"),
    ],
)
def test_class_provider_reports_specific_uri_failure(provider_constructor, url, diagnostic):
    loaded, errors, warnings = provider_constructor
    instance = provider.ClassLayerProvider(
        {
            "url": url,
            "typename": "cl_unclassified__polygon",
        }
    )
    assert not instance.isValid()
    assert not loaded
    assert errors == [instance.last_refresh_error]
    assert diagnostic in instance.last_refresh_error
    assert warnings == ["Could not open CVI class layer: " + instance.last_refresh_error]


def test_physical_row_identity_survives_revision_and_distinguishes_valid_versions():
    first = {"id": "12.aabb", "properties": {"label_id": "same-label"}}
    next_revision = {"id": "12.ccdd", "properties": {"label_id": "same-label"}}
    other_valid_version = {"id": "13.aabb", "properties": {"label_id": "same-label"}}
    assert provider._row_identity(first) == provider._row_identity(next_revision)
    assert provider._row_identity(first) != provider._row_identity(other_valid_version)


@pytest.mark.parametrize("identity", [None, "", "12", "12/revision", "0.aa", "01.aa"])
def test_invalid_row_identity_is_rejected(identity):
    with pytest.raises(ValueError):
        provider._row_identity({"id": identity})


@pytest.mark.parametrize(
    "name,has_values,local,populated,expected",
    [
        ("src_6964", False, set(), set(), True),
        ("name_en", False, set(), set(), True),
        ("label_id", False, set(), set(), False),
        ("src_6964", False, {"src_6964"}, set(), False),
        ("src_6964", False, set(), {"src_6964"}, False),
        ("src_6964", True, set(), set(), False),
        ("src_6964", None, set(), set(), False),
    ],
)
def test_pruning_never_removes_sync_keys_local_columns_or_fresh_values(
    name, has_values, local, populated, expected
):
    definition = {"name": name, "has_values": has_values}
    assert (
        provider._omit_unused(definition, enabled=True, local_wires=local, populated=populated)
        is expected
    )
    assert not provider._omit_unused(
        definition, enabled=False, local_wires=local, populated=populated
    )


def test_local_field_restores_integer_width_and_precision(monkeypatch):
    monkeypatch.setattr(provider, "QgsField", lambda *args: args)
    restored = provider._local_field(
        {
            "name": "Count",
            "type": "integer",
            "variant_type": int(provider.QVariant.Int),
            "type_name": "integer",
            "length": 10,
            "precision": 0,
            "comment": "Installed",
        }
    )
    assert restored == ("Count", provider.QVariant.Int, "integer", 10, 0, "Installed")


def test_create_preserves_validity_defaults_but_excludes_immutable_identity():
    names = ["label_id", "valid_from", "valid_to", "New field"]
    fields = [SimpleNamespace(name=lambda name=name: name) for name in names]
    state = SimpleNamespace(
        fields=lambda: fields,
        _wire_by_name=dict(
            zip(names, ["label_id", "valid_from", "valid_to", "src_4e6577"], strict=True)
        ),
        _definitions={name: {"read_only": True} for name in names[:3]},
    )
    result = provider.ClassLayerProvider._properties(
        state, ["do-not-send", "2026-09-22T00:00:00Z", None, 0]
    )
    assert result == {"valid_from": "2026-09-22T00:00:00Z", "valid_to": None, "src_4e6577": 0}


def test_read_only_provider_refuses_direct_writes():
    with pytest.raises(ValueError, match="not editable"):
        provider.ClassLayerProvider._require_writable(SimpleNamespace(_read_only=True, _valid=True))


@pytest.mark.parametrize("status,uncertain", [(None, True), (408, True), (502, True), (422, False)])
def test_unknown_create_outcome_blocks_repeat_post(status, uncertain):
    calls = []
    errors = []

    def http(*args):
        calls.append(args)
        raise BackendError("write failed", status=status)

    state = SimpleNamespace(
        _require_writable=lambda: None,
        _uncertain_create=False,
        _created={},
        _created_payloads={},
        _properties=lambda feature: {},
        _collection_url="https://api.example/class-layers/collections/cl_test__point",
        _http=http,
        pushError=errors.append,
    )
    state._mark_uncertain_create = lambda: setattr(state, "_uncertain_create", True)
    feature = SimpleNamespace(
        id=lambda: -1,
        geometry=lambda: SimpleNamespace(asJson=lambda: '{"type":"Point","coordinates":[1,2]}'),
    )
    assert provider.ClassLayerProvider.addFeatures(state, [feature]) == (False, [])
    assert state._uncertain_create is uncertain
    if uncertain:
        assert provider.ClassLayerProvider.addFeatures(state, [feature]) == (False, [])
        assert len(calls) == 1
        assert "unknown save outcome" in errors[-1]
