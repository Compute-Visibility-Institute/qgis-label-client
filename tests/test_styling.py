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


# --- the historical variant --------------------------------------------------
#
# Three visual states have to be distinguishable at a glance, because two of the layers
# they appear on look otherwise identical:
#
#   live                              solid stroke, full opacity, class colour
#   believed, still true today        dashed stroke, 55% opacity, class colour
#   believed, since deleted/corrected dashed stroke in the alert colour
#
# The class colours stay put in all three. A historical layer is still a labels layer, and
# the colours are how people read it -- changing them would make the two layers harder to
# compare, which is the whole reason both are open at once.

from qgis_label_client.core.fields import CoreFields  # noqa: E402
from qgis_label_client.core.styling import (  # noqa: E402
    HISTORICAL_OPACITY,
    SUPERSEDED_STROKE,
    superseded_stroke_expression,
)


def test_a_historical_polygon_is_dashed_but_keeps_its_class_colour():
    # A dashed boundary is the strongest "not editable" convention in GIS, and it costs
    # one property.
    style = {"fill": "#4f9dde66", "stroke": "#e8c547", "stroke_width": 2}
    live = fill_symbol_properties(style)
    believed = fill_symbol_properties(style, historical=True)
    assert live["outline_style"] == "solid"
    assert believed["outline_style"] == "dash"
    assert believed["outline_color"] == live["outline_color"]
    assert believed["color"] == live["color"]


def test_a_historical_line_and_marker_are_dashed_too():
    assert line_symbol_properties({"stroke": "#f2f2f2"}, historical=True)["line_style"] == "dash"
    assert marker_symbol_properties({"radius": 3}, historical=True)["outline_style"] == "dash"


def test_a_class_that_is_already_dashed_stays_dashed():
    # A style block with its own dash array wins on the pattern; the historical flag must
    # not undo it or fight it.
    properties = fill_symbol_properties({"dash": [6, 3]}, historical=True)
    assert properties["outline_style"] == "dash"
    assert properties["customdash"] == "6;3"


def test_symbol_properties_passes_the_flag_through():
    assert symbol_properties("MultiPolygon", {}, True)["outline_style"] == "dash"
    assert symbol_properties("MultiLineString", {}, True)["line_style"] == "dash"
    assert symbol_properties("Point", {}, True)["outline_style"] == "dash"
    assert symbol_properties("MultiPolygon", {})["outline_style"] == "solid"


def test_a_belief_that_has_since_ended_is_coloured_by_a_data_defined_expression():
    """A data-defined property on the ordinary symbol, not a rule-based renderer.

    The deciding reason is survival: every re-point exports the style to a QDomDocument and
    re-imports it, and data-defined properties are plain QGIS symbology that survives that
    round trip. A track switch would otherwise silently strip the distinction between a
    label the team still believes and one it deleted.
    """
    expression = superseded_stroke_expression({"stroke": "#e8c547"})
    assert expression.startswith('if("superseded", ')
    # The alert colour and the class colour, both in the unambiguous r,g,b,a form.
    assert qgis_color(SUPERSEDED_STROKE) in expression
    assert "232,197,71,255" in expression


def test_the_superseded_expression_honours_server_supplied_field_names():
    fields = CoreFields().merged({"superseded": "belief_ended"})
    assert superseded_stroke_expression({}, fields).startswith('if("belief_ended", ')


def test_a_historical_layer_reads_as_a_ghost_without_becoming_invisible():
    # One call on the layer rather than a per-symbol alpha: it survives any renderer and
    # cannot be undone by a class whose own style block sets an opaque fill.
    assert 0.3 < HISTORICAL_OPACITY < 0.8


# ── classes are offered where they can occur, and nowhere else ────────────────


def test_a_class_matches_its_own_geometry_family() -> None:
    """MultiPolygon and Polygon are the same KIND of thing, and both spellings occur:
    label_class.geom_type says MultiPolygon, QGIS says Polygon."""
    from qgis_label_client.core.registry import LabelClass

    compound = LabelClass(class_id="compound", geom_type="MultiPolygon", label_en="Compound")
    assert compound.matches_geometry("Polygon")
    assert compound.matches_geometry("MultiPolygon")
    assert not compound.matches_geometry("LineString")
    assert not compound.matches_geometry("Point")


def test_an_any_class_is_offered_everywhere() -> None:
    """`unclassified` exists because the shape is known before the meaning is, so it has
    to be available wherever a shape can be drawn."""
    from qgis_label_client.core.registry import LabelClass

    unclassified = LabelClass(class_id="unclassified", geom_type="Any", label_en="Unclassified")
    assert all(unclassified.matches_geometry(f) for f in ("Polygon", "Point", "LineString"))


def test_an_unknown_family_matches_rather_than_hides() -> None:
    """Asked when building a legend. Omitting a class because this plugin did not
    recognise a geometry name is worse than showing one too many."""
    from qgis_label_client.core.registry import LabelClass

    powerline = LabelClass(class_id="powerline", geom_type="MultiLineString", label_en="Powerline")
    assert powerline.matches_geometry("")
    assert powerline.matches_geometry("CircularString")


def test_the_catch_all_borrows_the_unclassified_style() -> None:
    """It is already drab and dashed on purpose, which is the right reading for a feature
    whose class this layer did not expect."""
    from qgis_label_client.core.registry import ClassRegistry, LabelClass

    reg = ClassRegistry(
        classes=(
            LabelClass(class_id="compound", geom_type="MultiPolygon", label_en="Compound"),
            LabelClass(class_id="unclassified", geom_type="Any", label_en="Unclassified"),
        )
    )
    assert reg.unclassified_or_first().class_id == "unclassified"


def test_a_registry_without_unclassified_still_yields_a_symbol() -> None:
    """The bucket only has to be VISIBLE; being pretty is not the job."""
    from qgis_label_client.core.registry import ClassRegistry, LabelClass

    reg = ClassRegistry(
        classes=(LabelClass(class_id="compound", geom_type="MultiPolygon", label_en="Compound"),)
    )
    assert reg.unclassified_or_first().class_id == "compound"
