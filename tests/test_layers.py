"""Finding the plugin's own layers again, which is harder than it sounds.

The QA tools do not look up layers by collection name -- a deployment names its
collections whatever it likes -- so they look them up by the fields they expose. That
works only if the field sets actually discriminate, and one pair does not: the audit
collection carries the same ``label_id`` and ``class_id`` as the label collection.
"""

from __future__ import annotations

from qgis_label_client import layers as layer_tools
from qgis_label_client.core.registry import parse_registry

REGISTRY = parse_registry({"classes": [{"class_id": "alpha", "label_en": "Alpha"}]})


class _Field:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _FakeLayer:
    """Only what find_layer_with_fields touches."""

    def __init__(self, name: str, field_names: list[str]) -> None:
        self._name = name
        self._fields = [_Field(n) for n in field_names]

    def name(self) -> str:
        return self._name

    def fields(self) -> list[_Field]:
        return self._fields


LABEL_FIELDS = ["label_id", "class_id", "names", "attrs", "valid_from", "valid_to"]
AUDIT_FIELDS = ["history_id", "label_id", "operation", "changed", "actor", "class_id"]
EXTENT_FIELDS = ["extent_id", "class_id", "completeness", "caveat"]


def _with_layers(monkeypatch, *layers):
    monkeypatch.setattr(layer_tools, "plugin_layers", lambda project=None: list(layers))


def test_the_audit_layer_is_not_mistaken_for_the_label_layer(monkeypatch):
    # Regression. label_history is keyed on the same label_id and carries the class_id of
    # each superseded state, so identity-and-class does not tell the two apart. Loading
    # both collections and picking the wrong one runs the coverage check over every
    # historical revision -- counting a label once per edit and classifying geometry that
    # is no longer on the map. mapLayers() order is not something the plugin controls, so
    # this has to be decided by the fields, not by luck.
    audit = _FakeLayer("audit", AUDIT_FIELDS)
    label = _FakeLayer("label", LABEL_FIELDS)
    _with_layers(monkeypatch, audit, label)
    assert layer_tools.find_label_layer(REGISTRY) is label

    # ...and in the other order, because that is exactly what varies.
    _with_layers(monkeypatch, label, audit)
    assert layer_tools.find_label_layer(REGISTRY) is label


def test_the_extent_layer_is_found_by_its_completeness_column(monkeypatch):
    extent = _FakeLayer("extent", EXTENT_FIELDS)
    _with_layers(monkeypatch, _FakeLayer("label", LABEL_FIELDS), extent)
    assert layer_tools.find_extent_layer(REGISTRY) is extent


def test_no_label_layer_loaded_is_none_rather_than_a_wrong_guess(monkeypatch):
    _with_layers(monkeypatch, _FakeLayer("audit", AUDIT_FIELDS))
    assert layer_tools.find_label_layer(REGISTRY) is None
