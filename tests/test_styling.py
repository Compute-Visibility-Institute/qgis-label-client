"""Style translation from the registry's web-shaped vocabulary to QGIS symbols."""

from __future__ import annotations

import pytest

from qgis_label_client.core.styling import (
    fill_symbol_properties,
    line_symbol_properties,
    marker_symbol_properties,
    parse_color,
    qgis_color,
    symbol_kind,
    symbol_properties,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("#4f9dde", (79, 157, 222, 255)),
        ("#4f9dde66", (79, 157, 222, 102)),
        ("#00000000", (0, 0, 0, 0)),
        ("#abc", (170, 187, 204, 255)),
        ("12,34,56", (12, 34, 56, 255)),
        ("12,34,56,78", (12, 34, 56, 78)),
    ],
)
def test_css_colours_parse(value, expected):
    assert parse_color(value) == expected


def test_rrggbbaa_is_not_read_as_aarrggbb():
    # The registry uses CSS ordering. Qt's hex-with-alpha form is the other way round,
    # and the two are silently compatible-looking.
    assert parse_color("#4f9dde66")[0] == 0x4F


@pytest.mark.parametrize("value", [None, 42, "", "not a colour", "#12345"])
def test_unparseable_colours_fall_back_rather_than_raise(value):
    assert parse_color(value) == (0, 0, 0, 0)


def test_qgis_colour_is_the_unambiguous_rgba_string():
    assert qgis_color("#4f9dde66") == "79,157,222,102"


def test_transparent_fill_becomes_no_fill_not_an_invisible_one():
    # An invisible fill still makes the whole polygon clickable, which on a national
    # layer means every click selects a compound.
    properties = fill_symbol_properties({"fill": "#00000000", "stroke": "#e8c547"})
    assert properties["style"] == "no"
    assert properties["outline_color"] == "232,197,71,255"


def test_opaque_fill_is_drawn():
    assert fill_symbol_properties({"fill": "#4f9dde66"})["style"] == "solid"


def test_widths_are_in_pixels_to_match_the_web_viewer():
    properties = fill_symbol_properties({"stroke_width": 2})
    assert properties["outline_width"] == "2"
    assert properties["outline_width_unit"] == "Pixel"


def test_dash_arrays_become_custom_dashes():
    properties = fill_symbol_properties({"dash": [6, 3]})
    assert properties["outline_style"] == "dash"
    assert properties["customdash"] == "6;3"
    assert properties["use_custom_dash"] == "1"


def test_line_dash_arrays_too():
    properties = line_symbol_properties({"stroke": "#f2f2f2", "stroke_width": 1.5, "dash": [4, 2]})
    assert properties["line_style"] == "dash"
    assert properties["customdash"] == "4;2"
    assert properties["line_width"] == "1.5"


def test_radius_becomes_a_diameter():
    # MapLibre's circle-radius is a radius; QGIS's marker size is a diameter. Passing the
    # number through would draw 872 cooling units at twice their intended size.
    assert marker_symbol_properties({"radius": 3})["size"] == "6"


def test_marker_defaults_when_no_radius_is_declared():
    assert marker_symbol_properties({})["size"] == "6"


@pytest.mark.parametrize(
    "geom_type,expected",
    [
        ("Point", "marker"),
        ("MultiPoint", "marker"),
        ("LineString", "line"),
        ("MultiLineString", "line"),
        ("Polygon", "fill"),
        ("MultiPolygon", "fill"),
        ("Any", "fill"),
        ("Nonsense", "fill"),
    ],
)
def test_symbol_kind_follows_geometry_type(geom_type, expected):
    assert symbol_kind(geom_type) == expected


def test_symbol_properties_dispatches():
    assert "size" in symbol_properties("Point", {"radius": 2})
    assert "line_color" in symbol_properties("MultiLineString", {})
    assert "outline_color" in symbol_properties("MultiPolygon", {})
