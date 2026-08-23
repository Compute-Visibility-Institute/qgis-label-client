"""The class registry.

Every fixture here is invented for the test. Nothing in the plugin knows these class
names or attribute names, which is the property being verified.
"""

from __future__ import annotations

import pytest

from qgis_label_client.core.errors import RegistryError
from qgis_label_client.core.registry import parse_registry

DOC = {
    "classes": [
        {
            "class_id": "widget",
            "geom_type": "MultiPolygon",
            "label_en": "Widget",
            "label_zh": "小部件",
            "sort_order": 20,
            "attr_schema": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "zeta": {"type": "string"},
                    "alpha": {"type": "integer", "minimum": 0, "maximum": 9},
                    "colour": {"type": "string", "enum": ["red", "blue"]},
                },
            },
            "form": {"order": ["colour", "alpha"], "widgets": {"colour": "select"}},
            "style": {"fill": "#112233ff", "stroke": "#445566", "stroke_width": 2},
        },
        {
            "class_id": "sprocket",
            "geom_type": "Point",
            "label_en": "Sprocket",
            "sort_order": 10,
            "active": False,
        },
    ]
}


def test_classes_sort_by_sort_order_then_id():
    registry = parse_registry(DOC)
    assert [cls.class_id for cls in registry] == ["sprocket", "widget"]


def test_retired_classes_are_kept_but_excluded_from_active():
    registry = parse_registry(DOC)
    assert len(registry) == 2
    assert [cls.class_id for cls in registry.active()] == ["widget"]


def test_value_map_offers_only_classes_the_server_still_accepts():
    registry = parse_registry(DOC)
    assert registry.value_map() == [("Widget (小部件)", "widget")]


def test_display_name_pairs_both_languages_when_both_exist():
    registry = parse_registry(DOC)
    assert registry.get("widget").display_name == "Widget (小部件)"
    assert registry.get("sprocket").display_name == "Sprocket"


def test_form_order_wins_and_undeclared_extras_are_appended_not_dropped():
    widget = parse_registry(DOC).get("widget")
    # 'zeta' is in the schema but not in form.order; it must still be offered.
    assert widget.attribute_names() == ["colour", "alpha", "zeta"]


def test_attribute_spec_exposes_the_schema_fragment():
    widget = parse_registry(DOC).get("widget")
    colour = widget.attribute("colour")
    assert colour.enum == ("red", "blue")
    assert widget.attribute("alpha").minimum == 0
    assert widget.attribute("alpha").maximum == 9
    assert widget.attribute("missing").schema == {}


def test_open_vocabulary_reflects_additional_properties():
    registry = parse_registry(DOC)
    assert registry.get("widget").open_vocabulary is True
    closed = parse_registry(
        {
            "classes": [
                {
                    "class_id": "c",
                    "geom_type": "Point",
                    "label_en": "C",
                    "attr_schema": {"type": "object", "additionalProperties": False},
                }
            ]
        }
    )
    assert closed.get("c").open_vocabulary is False


def test_help_text_lists_the_schema_not_a_builtin_list():
    widget = parse_registry(DOC).get("widget")
    text = widget.help_text()
    assert "colour" in text and "one of red, blue" in text
    assert "Additional attributes are accepted" in text


def test_widget_hint_comes_from_the_form_block():
    widget = parse_registry(DOC).get("widget")
    assert widget.widget_hint("colour") == "select"
    assert widget.widget_hint("alpha") is None


def test_bare_array_shape_is_accepted():
    registry = parse_registry(DOC["classes"])
    assert len(registry) == 2


def test_feature_collection_shape_is_accepted():
    document = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": cls} for cls in DOC["classes"]],
    }
    assert len(parse_registry(document)) == 2


def test_server_can_override_core_field_names():
    registry = parse_registry({"classes": DOC["classes"], "fields": {"class_id": "kind"}})
    assert registry.fields.class_id == "kind"
    # Unrelated names keep their defaults.
    assert registry.fields.label_id == "label_id"


def test_unknown_field_overrides_are_ignored_rather_than_fatal():
    registry = parse_registry({"classes": DOC["classes"], "fields": {"nonsense": "x"}})
    assert registry.fields.class_id == "class_id"


@pytest.mark.parametrize(
    "document",
    [
        {"classes": []},
        {"nothing": 1},
        "a string",
        42,
    ],
)
def test_unusable_documents_raise(document):
    with pytest.raises(RegistryError):
        parse_registry(document)


def test_entries_without_a_class_id_raise():
    with pytest.raises(RegistryError):
        parse_registry({"classes": [{"label_en": "no id"}]})
