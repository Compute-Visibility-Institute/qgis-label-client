"""Reading QGIS symbology back into the registry's style block.

THE ROUND TRIP IS THE TEST THAT MATTERS. Everything else here checks one conversion in
isolation; the round trip checks that this module and `styling` agree about all three at
once -- the millimetres, the radius/diameter, and the colour byte order -- and nothing
else catches any of them. Each has the same failure shape: the captured style looks
correct in the analyst's own QGIS and wrong in the browser, so the person who made the
mistake is the one person who cannot see it.

The colours below are deliberately not fully opaque. An alpha of 0xFF is the same byte in
CSS order and in Qt's, so a byte-order bug hides behind it.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from qgis_label_client.core import styling
from qgis_label_client.core.stylecapture import (
    ATTRIBUTE_RENDERERS,
    PIXELS_PER_MILLIMETRE,
    NoteKind,
    RendererDescription,
    SymbolDescription,
    SymbolLayerDescription,
    Unit,
    capture_style,
    css_color,
    normalise_unit,
)

# ── the round trip ────────────────────────────────────────────────────────────


def _dash_from(properties: dict[str, str]) -> tuple[tuple[float, ...], str]:
    if properties.get("use_custom_dash") != "1":
        return (), ""
    return (
        tuple(float(part) for part in properties["customdash"].split(";")),
        properties["customdash_unit"],
    )


def _read_back(kind: str, properties: dict[str, str]) -> SymbolLayerDescription:
    """The QGIS-facing reader this module is waiting for, standing in for itself.

    Every value comes out of `styling`'s own output under `styling`'s own key names,
    units included -- and the units matter more than they look: QGIS spells the unit
    "Pixel", `styling` writes "Pixel", and a capture that only understood its own tidier
    spelling would refuse every symbol in existence while looking fine in isolation.
    """
    dash, dash_unit = _dash_from(properties)
    if kind == "fill":
        return SymbolLayerDescription(
            type_name="SimpleFill",
            fill_color=properties["color"],
            brush_style=properties["style"],
            stroke_color=properties["outline_color"],
            stroke_width=float(properties["outline_width"]),
            stroke_width_unit=properties["outline_width_unit"],
            stroke_style=properties["outline_style"],
            dash_pattern=dash,
            dash_pattern_unit=dash_unit or Unit.PIXELS,
        )
    if kind == "line":
        return SymbolLayerDescription(
            type_name="SimpleLine",
            stroke_color=properties["line_color"],
            stroke_width=float(properties["line_width"]),
            stroke_width_unit=properties["line_width_unit"],
            stroke_style=properties["line_style"],
            dash_pattern=dash,
            dash_pattern_unit=dash_unit or Unit.PIXELS,
        )
    return SymbolLayerDescription(
        type_name="SimpleMarker",
        fill_color=properties["color"],
        stroke_color=properties["outline_color"],
        stroke_width=float(properties["outline_width"]),
        stroke_width_unit=properties["outline_width_unit"],
        size=float(properties["size"]),
        size_unit=properties["size_unit"],
        marker_shape=properties["name"],
    )


def _through_qgis(geom_type: str, style: dict) -> RendererDescription:
    """A style block, rendered to QGIS symbol properties and described back."""
    properties = styling.symbol_properties(geom_type, style)
    layer = _read_back(styling.symbol_kind(geom_type), properties)
    return RendererDescription(
        renderer_type="singleSymbol", symbol=SymbolDescription(layers=(layer,))
    )


def _in_millimetres(renderer: RendererDescription) -> RendererDescription:
    """The same symbol, measured in QGIS's default unit instead of the viewer's.

    `styling` writes every width in pixels, so a trip out through it and back multiplies
    by nothing: a millimetre factor of 1.0 would survive that round trip untouched. The
    symbol an analyst actually hands over is this one -- QGIS gives a new symbol layer
    millimetres -- and the same block has to come back out of it.
    """
    layer = renderer.symbol.layers[0]
    in_mm = replace(
        layer,
        stroke_width=(
            None if layer.stroke_width is None else layer.stroke_width / PIXELS_PER_MILLIMETRE
        ),
        stroke_width_unit="MM",
        size=None if layer.size is None else layer.size / PIXELS_PER_MILLIMETRE,
        size_unit="MM",
        dash_pattern=tuple(part / PIXELS_PER_MILLIMETRE for part in layer.dash_pattern),
        dash_pattern_unit="MM",
    )
    return replace(renderer, symbol=replace(renderer.symbol, layers=(in_mm,)))


#: Every shape of style block the seed and the classes it describes actually use.
ROUND_TRIP_BLOCKS = [
    # A translucent fill with an opaque outline: the shape most classes are.
    ("MultiPolygon", {"fill": "#4f9dde66", "stroke": "#4f9dde", "stroke_width": 1.5}),
    # Outline-only, which the seed writes as a fully transparent fill.
    ("MultiPolygon", {"fill": "#00000000", "stroke": "#e8c547", "stroke_width": 2}),
    # Transparent but coloured: the RGB has to survive even though nothing draws it, or a
    # re-import proposes a change that renders identically.
    ("MultiPolygon", {"fill": "#4f9dde00", "stroke": "#4f9dde", "stroke_width": 1}),
    (
        "MultiPolygon",
        {"fill": "#00000000", "stroke": "#e8c547", "stroke_width": 2, "dash": [6, 3]},
    ),
    ("Polygon", {"fill": "#5ec8a066", "stroke": "#2f7f63", "stroke_width": 0.75}),
    ("MultiLineString", {"stroke": "#f2f2f2cc", "stroke_width": 1.5}),
    ("LineString", {"stroke": "#e8c547", "stroke_width": 2, "dash": [4, 2]}),
    ("Point", {"fill": "#5ec8a0", "stroke": "#2f7f63", "stroke_width": 0.5, "radius": 3}),
    (
        "MultiPoint",
        {"fill": "#4f9dde66", "stroke": "#4f9dde", "stroke_width": 1, "radius": 4.5},
    ),
]


@pytest.mark.parametrize("geom_type,style", ROUND_TRIP_BLOCKS)
def test_a_style_block_survives_the_trip_out_to_qgis_and_back(geom_type, style):
    """The one test that catches an inverted conversion.

    Each of the three -- millimetres for pixels, diameter for radius, Qt's byte order for
    CSS's -- produces a style that is self-consistently wrong: it renders correctly in the
    QGIS it was read from, so nothing short of comparing the block to itself notices.
    """
    assert capture_style(_through_qgis(geom_type, style)).style == style


@pytest.mark.parametrize("geom_type,style", ROUND_TRIP_BLOCKS)
def test_the_same_symbol_in_millimetres_captures_to_the_same_block(geom_type, style):
    # The half of the round trip the forward path cannot exercise, because it only ever
    # writes pixels. Without this, a millimetre factor of 1.0 passes every test here.
    assert capture_style(_in_millimetres(_through_qgis(geom_type, style))).style == style


def test_the_round_trip_would_notice_a_doubled_radius():
    # Guarding the guard: if the halving were removed, the assertion above has to fail
    # rather than pass on a symmetric mistake.
    captured = capture_style(_through_qgis("Point", {"radius": 3, "stroke_width": 0.5})).style
    assert captured["radius"] == 3
    assert styling.marker_symbol_properties({"radius": 3})["size"] == "6"


def test_the_unit_spelling_the_forward_path_writes_is_the_one_that_comes_back():
    # `styling` writes "Pixel"; QgsUnitTypes.encodeUnit() says "Pixel". A capture keyed on
    # this module's own "pixel" refuses both, which is a loud failure and a total one.
    properties = styling.fill_symbol_properties({"stroke_width": 2})
    assert properties["outline_width_unit"] == "Pixel"
    layer = SymbolLayerDescription(
        type_name="SimpleFill",
        stroke_color="0,0,0,255",
        stroke_width=2.0,
        stroke_width_unit="Pixel",
    )
    result = capture_style(RendererDescription("singleSymbol", SymbolDescription(layers=(layer,))))
    assert result.captured
    assert result.style["stroke_width"] == 2


# ── widths and sizes are pixels; QGIS's are not ───────────────────────────────


def _fill(**kwargs) -> RendererDescription:
    layer = SymbolLayerDescription(type_name="SimpleFill", **kwargs)
    return RendererDescription("singleSymbol", SymbolDescription(layers=(layer,)))


def _marker(**kwargs) -> RendererDescription:
    layer = SymbolLayerDescription(type_name="SimpleMarker", **kwargs)
    return RendererDescription("singleSymbol", SymbolDescription(layers=(layer,)))


def test_qgis_default_hairline_is_one_pixel_not_a_quarter_of_one():
    # 0.26 mm is QGIS's default outline width. Captured unconverted it is a quarter of a
    # pixel and the web viewer draws nothing at all.
    result = capture_style(_fill(stroke_width=0.26, stroke_width_unit="MM"))
    assert result.style["stroke_width"] == 0.98


@pytest.mark.parametrize(
    "value,unit,expected",
    [
        (1.0, "MM", 3.78),
        (2.0, "MM", 7.56),
        (1.0, "Point", 1.33),
        (0.5, "Inch", 48),
        (1.5, "Pixel", 1.5),
    ],
)
def test_every_convertible_unit_lands_in_pixels(value, unit, expected):
    assert (
        capture_style(_fill(stroke_width=value, stroke_width_unit=unit)).style["stroke_width"]
        == expected
    )


def test_a_conversion_is_reported_because_the_numbers_stop_matching_the_dialog():
    result = capture_style(_fill(stroke_width=1.0, stroke_width_unit="MM"))
    assert any("converted to pixels" in note.detail for note in result.of_kind(NoteKind.ASSUMED))


def test_a_width_already_in_pixels_is_not_reported_as_converted():
    # Nothing happened to it, and a report that cries wolf about every symbol is one
    # nobody reads by the third layer.
    result = capture_style(_fill(stroke_width=2.0, stroke_width_unit="Pixel"))
    assert result.notes == ()


@pytest.mark.parametrize("value", [1.234, 0.004])
def test_a_pixel_width_is_the_analysts_own_number_and_stays_exact(value):
    # Rounding exists for the seventeen digits a millimetre conversion produces. Applied
    # to a pixel width it invents a change, and 0.004 -> 0 is a stroke the viewer draws
    # nothing for -- the failure the conversion is there to prevent, reintroduced by the
    # tidying.
    assert (
        capture_style(_fill(stroke_width=value, stroke_width_unit="Pixel")).style["stroke_width"]
        == value
    )


def test_an_integral_width_is_written_as_an_integer():
    # 010_classes.sql writes 2, not 2.0, and a capture has to match it to be recognised as
    # proposing no change.
    style = capture_style(_fill(stroke_width=2.0, stroke_width_unit="Pixel")).style
    assert repr(style["stroke_width"]) == "2"


def test_a_dash_pattern_is_converted_too():
    result = capture_style(
        _fill(
            stroke_width=1.0,
            stroke_width_unit="Pixel",
            dash_pattern=(2.0, 1.0),
            dash_pattern_unit="MM",
        )
    )
    assert result.style["dash"] == [7.56, 3.78]


# ── radius is a radius; QGIS's marker size is a diameter ──────────────────────


def test_marker_size_is_halved_into_a_radius():
    # Passing it through draws 872 cooling units on one campus at twice their size: the
    # difference between dots and a solid blanket.
    assert capture_style(_marker(size=6.0, size_unit="Pixel")).style["radius"] == 3


def test_the_halving_happens_after_the_unit_conversion_not_before():
    # 4 mm is 15.12 px across, so 7.56 px out from the centre. Halving the millimetres
    # first and converting after gives the same number here only because both operations
    # are linear -- the test is that the answer is in pixels at all.
    assert capture_style(_marker(size=4.0, size_unit="MM")).style["radius"] == 7.56


def test_a_marker_shape_that_is_not_a_circle_is_captured_and_reported():
    # `radius` is MapLibre's circle-radius and the forward path hardcodes a circle, so a
    # square in QGIS is a circle in the browser. That is the module's worst failure shape,
    # so it cannot sit in the silent set.
    result = capture_style(_marker(size=6.0, size_unit="Pixel", marker_shape="square"))
    assert result.captured
    assert any("square" in note.detail for note in result.of_kind(NoteKind.IGNORED))


def test_a_circle_is_not_reported_as_a_loss():
    result = capture_style(_marker(size=6.0, size_unit="Pixel"))
    assert result.of_kind(NoteKind.IGNORED) == ()


def test_a_markers_dashed_outline_is_ignored_and_said_so():
    result = capture_style(
        _marker(size=6.0, size_unit="Pixel", stroke_style="dash", dash_pattern=(2.0, 1.0))
    )
    assert "dash" not in result.style
    assert any("dash" in note.detail for note in result.of_kind(NoteKind.IGNORED))


# ── colours are CSS #RRGGBBAA, never Qt's #AARRGGBB ───────────────────────────


def test_a_qcolor_in_rgba_order_captures_in_css_order():
    # #4f9dde66 and #664f9dde both parse and only one is the colour the analyst chose.
    result = capture_style(_fill(fill_color="79,157,222,102"))
    assert result.style["fill"] == "#4f9dde66"
    assert result.style["fill"] != "#664f9dde"


def test_the_alpha_is_the_last_byte_and_not_the_first():
    assert css_color("79,157,222,102").startswith("#4f9d")
    assert css_color("79,157,222,102").endswith("66")


def test_this_is_why_the_contract_asks_for_r_g_b_a():
    # QColor.name(HexArgb) is #AARRGGBB. It is a valid CSS colour, it parses without
    # complaint, and it is a different colour -- there is no way to tell the two apart
    # from the string, which is why the description asks for the unambiguous form.
    assert css_color("#4f9dde66") != css_color("#664f9dde")


def test_an_opaque_colour_is_six_digits_so_an_unchanged_style_proposes_nothing():
    # The seeded rows hold "#4f9dde". Proposing "#4f9ddeff" is a change that is not a
    # change, and 018_class_registry.sql would record it in class_history as one.
    assert capture_style(_fill(fill_color="79,157,222,255")).style["fill"] == "#4f9dde"


def test_a_transparent_fill_keeps_the_colour_the_analyst_chose():
    # "No brush" is how a polygon class says outline-only. Collapsing it to #00000000
    # throws away an RGB that is still in the symbol and still meaningful.
    result = capture_style(_fill(fill_color="79,157,222,0", brush_style="no"))
    assert result.style["fill"] == "#4f9dde00"


def test_an_outline_only_symbol_with_no_colour_at_all_is_still_outline_only():
    result = capture_style(_fill(brush_style="no", stroke_color="232,197,71,255"))
    assert result.style["fill"] == "#00000000"


def test_an_explicit_no_outline_is_recorded_rather_than_left_to_a_default():
    # Omitting the key lets the forward path's default paint an outline nobody asked for.
    result = capture_style(_fill(fill_color="79,157,222,102", stroke_style="no"))
    assert result.style["stroke"] == "#00000000"


def test_a_colour_that_does_not_parse_is_refused_rather_than_captured_as_invisible():
    # styling.parse_color falls back to transparent instead of raising, which is right for
    # loading a registry and wrong for proposing a style: the fallback would be captured
    # as a success and paint the class out of existence.
    result = capture_style(_fill(fill_color="not a colour"))
    assert not result.captured
    assert "not a colour" in result.refusal.reason
    assert "r,g,b,a" in result.refusal.remedy


# ── opacity is three things in QGIS and one thing here ────────────────────────


def test_layer_and_symbol_opacity_are_multiplied_into_the_alpha():
    layer = SymbolLayerDescription(type_name="SimpleFill", fill_color="79,157,222,255")
    result = capture_style(
        RendererDescription(
            "singleSymbol",
            SymbolDescription(layers=(layer,), opacity=0.5),
            layer_opacity=0.5,
        )
    )
    # 255 * 0.5 * 0.5, which is what the analyst is looking at on screen.
    assert result.style["fill"] == "#4f9dde40"


def test_composing_opacity_is_reported_because_it_stops_matching_the_colour_picker():
    layer = SymbolLayerDescription(type_name="SimpleFill", fill_color="79,157,222,255")
    result = capture_style(
        RendererDescription("singleSymbol", SymbolDescription(layers=(layer,), opacity=0.5))
    )
    assert any("opacity" in note.detail for note in result.of_kind(NoteKind.ASSUMED))


def test_full_opacity_is_not_reported():
    result = capture_style(_fill(fill_color="79,157,222,102"))
    assert result.of_kind(NoteKind.ASSUMED) == ()


# ── units with no honest pixel value are refused ──────────────────────────────


@pytest.mark.parametrize("unit", ["MapUnit", "RenderMetersInMapUnits", "map_unit", "MapUnits"])
def test_map_units_are_refused_and_told_why(unit):
    # A width in map units is a different thickness at every zoom. There is no conversion
    # to make, honest or otherwise.
    result = capture_style(_fill(stroke_width=1.0, stroke_width_unit=unit))
    assert not result.captured
    assert "every zoom" in result.refusal.reason
    assert result.refusal.remedy


def test_a_map_unit_marker_size_is_refused_as_well_as_a_width():
    result = capture_style(_marker(size=6.0, size_unit="MapUnit"))
    assert not result.captured
    assert "marker size" in result.refusal.reason


def test_a_percentage_is_refused_in_its_own_words():
    result = capture_style(_fill(stroke_width=50.0, stroke_width_unit="Percentage"))
    assert not result.captured
    assert "percentage" in result.refusal.reason


def test_an_unrecognised_unit_names_the_spelling_it_did_not_recognise():
    # A unit QGIS grows after this table was written. Refusing is right; refusing without
    # saying which word was the problem sends the analyst looking in the wrong place.
    result = capture_style(_fill(stroke_width=1.0, stroke_width_unit="Furlongs"))
    assert not result.captured
    assert "Furlongs" in result.refusal.reason


@pytest.mark.parametrize(
    "spelling,expected",
    [
        ("MM", Unit.MILLIMETRES),
        ("Pixel", Unit.PIXELS),
        ("Point", Unit.POINTS),
        ("Inch", Unit.INCHES),
        ("MapUnit", Unit.MAP_UNITS),
        ("RenderMetersInMapUnits", Unit.METRES_AT_SCALE),
        ("Percentage", Unit.PERCENTAGE),
        ("Unknown", Unit.UNKNOWN),
        ("mm", Unit.MILLIMETRES),
        ("map units", Unit.MAP_UNITS),
        (Unit.PIXELS, Unit.PIXELS),
    ],
)
def test_every_qgsunittypes_spelling_is_understood(spelling, expected):
    # These are the strings `QgsUnitTypes.encodeUnit()` returns, and they are all the
    # QGIS-facing reader will ever have. A capture that understands only this module's
    # own enum values refuses 100% of real symbols -- including the pixel one, which
    # needs no conversion at all.
    assert normalise_unit(spelling) is expected


# ── renderers that mean "these are your classes" ──────────────────────────────


@pytest.mark.parametrize("renderer_type", sorted(ATTRIBUTE_RENDERERS))
def test_an_attribute_driven_renderer_is_refused_as_a_registry_question(renderer_type):
    # Styling by attribute value is exactly what the registry expresses as class
    # membership. Collapsing five symbols to one destroys the split the layer is already
    # drawing, and nobody finds out.
    result = capture_style(
        RendererDescription(renderer_type, category_count=5, category_field="kind")
    )
    assert not result.captured
    assert "5 symbols" in result.refusal.reason
    assert "separate classes" in result.refusal.remedy


def test_the_refusal_names_the_attribute_when_there_is_one():
    result = capture_style(RendererDescription("categorizedSymbol", category_field="kind"))
    assert "'kind'" in result.refusal.reason


def test_an_attribute_renderer_with_nothing_to_count_still_refuses_readably():
    result = capture_style(RendererDescription("graduatedSymbol"))
    assert "several symbols" in result.refusal.reason


def test_a_renderer_this_module_has_never_heard_of_is_refused_by_name():
    result = capture_style(RendererDescription("pointCluster"))
    assert not result.captured
    assert "'pointCluster'" in result.refusal.reason


def test_a_layer_with_no_renderer_at_all_is_refused():
    result = capture_style(RendererDescription("nullSymbol"))
    assert not result.captured
    assert result.refusal.remedy


def test_a_single_symbol_with_no_layers_is_refused():
    assert not capture_style(
        RendererDescription("singleSymbol", SymbolDescription(layers=()))
    ).captured


# ── one fill, one stroke, one radius is the whole vocabulary ──────────────────


def test_a_symbol_built_only_from_things_with_no_equivalent_is_refused_by_name():
    layer = SymbolLayerDescription(type_name="GradientFill")
    result = capture_style(
        RendererDescription("singleSymbol", SymbolDescription(layers=(layer,), symbol_type="fill"))
    )
    assert not result.captured
    assert "GradientFill" in result.refusal.reason
    assert result.refusal.remedy


def test_a_symbol_whose_layers_are_all_switched_off_says_so():
    layer = SymbolLayerDescription(type_name="SimpleFill", fill_color="1,2,3,4", enabled=False)
    result = capture_style(RendererDescription("singleSymbol", SymbolDescription(layers=(layer,))))
    assert not result.captured
    assert "turned off" in result.refusal.reason


def test_a_stacked_symbol_flattens_to_its_first_simple_layer_and_reports_the_rest():
    # A fill plus a hatch plus a marker is three drawn things and the vocabulary holds
    # one. Flattening is the only option; flattening quietly is not.
    layers = (
        SymbolLayerDescription(type_name="SimpleFill", fill_color="79,157,222,102"),
        SymbolLayerDescription(type_name="LinePatternFill"),
        SymbolLayerDescription(type_name="PointPatternFill"),
    )
    result = capture_style(RendererDescription("singleSymbol", SymbolDescription(layers=layers)))
    assert result.style["fill"] == "#4f9dde66"
    dropped = result.of_kind(NoteKind.DROPPED)
    assert len(dropped) == 1
    assert "LinePatternFill" in dropped[0].detail
    assert "PointPatternFill" in dropped[0].detail


def test_a_simple_layer_under_an_unreadable_one_is_still_found():
    layers = (
        SymbolLayerDescription(type_name="ShapeburstFill"),
        SymbolLayerDescription(type_name="SimpleFill", fill_color="79,157,222,102"),
    )
    result = capture_style(RendererDescription("singleSymbol", SymbolDescription(layers=layers)))
    assert result.style["fill"] == "#4f9dde66"
    assert "ShapeburstFill" in result.of_kind(NoteKind.DROPPED)[0].detail


def test_a_disabled_layer_is_not_reported_as_dropped():
    # It drew nothing, so nothing was lost, and a report listing it teaches the analyst to
    # ignore the report.
    layers = (
        SymbolLayerDescription(type_name="SimpleFill", fill_color="79,157,222,102"),
        SymbolLayerDescription(type_name="LinePatternFill", enabled=False),
    )
    result = capture_style(RendererDescription("singleSymbol", SymbolDescription(layers=layers)))
    assert result.of_kind(NoteKind.DROPPED) == ()


def test_the_kind_says_which_of_the_three_shapes_was_read():
    assert capture_style(_fill(fill_color="1,2,3,4")).kind == "fill"
    assert capture_style(_marker(size=6.0, size_unit="Pixel")).kind == "marker"
    line = SymbolLayerDescription(type_name="SimpleLine", stroke_color="1,2,3,4")
    assert (
        capture_style(RendererDescription("singleSymbol", SymbolDescription(layers=(line,)))).kind
        == "line"
    )


def test_a_line_layers_colour_is_a_stroke_and_not_a_fill():
    # QgsSimpleLineSymbolLayer.color() is the line's colour. Reading it as a fill leaves
    # every linear class with no visible line at all.
    line = SymbolLayerDescription(type_name="SimpleLine", stroke_color="232,197,71,204")
    result = capture_style(RendererDescription("singleSymbol", SymbolDescription(layers=(line,))))
    assert result.style == {"stroke": "#e8c547cc"}


def test_a_symbol_that_named_nothing_at_all_is_not_a_successful_capture():
    # Every class already has a style seeded, so an empty block proposes erasing a
    # deliberate choice -- and `captured` is the flag a caller branches on.
    result = capture_style(_fill())
    assert not result.captured
    assert result.style is None
    assert result.refusal.remedy


# ── what is ignored is reported ───────────────────────────────────────────────


def test_data_defined_overrides_are_ignored_and_named():
    result = capture_style(_fill(fill_color="1,2,3,4", data_defined=("fillColor",)))
    assert any("fillColor" in note.detail for note in result.of_kind(NoteKind.IGNORED))


def test_a_hatch_pattern_is_ignored_and_named():
    result = capture_style(_fill(fill_color="79,157,222,102", brush_style="horizontal"))
    assert result.style["fill"] == "#4f9dde66"
    assert any("horizontal" in note.detail for note in result.of_kind(NoteKind.IGNORED))


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"blend_mode": "Multiply"}, "Multiply"),
        ({"scale_visibility": True}, "zooms"),
        ({"labels_enabled": True}, "labels"),
    ],
)
def test_the_things_the_vocabulary_cannot_say_are_listed_rather_than_refused(kwargs, expected):
    # The analyst is going to notice that the web viewer looks plainer than their QGIS.
    # The report is the difference between a known trade and a bug.
    layer = SymbolLayerDescription(type_name="SimpleFill", fill_color="79,157,222,102")
    result = capture_style(
        RendererDescription("singleSymbol", SymbolDescription(layers=(layer,)), **kwargs)
    )
    assert result.captured
    assert any(expected in note.detail for note in result.of_kind(NoteKind.IGNORED))


def test_normal_blending_is_not_reported_as_a_loss():
    layer = SymbolLayerDescription(type_name="SimpleFill", fill_color="79,157,222,102")
    result = capture_style(
        RendererDescription("singleSymbol", SymbolDescription(layers=(layer,)), blend_mode="Normal")
    )
    assert result.notes == ()


def test_a_qt_builtin_dash_has_no_numbers_to_read_so_it_is_reported():
    # A pen style of "dash" with no custom vector is Qt's own pattern: there are no pixel
    # lengths in it to capture, and the viewer will draw the stroke solid.
    result = capture_style(_fill(fill_color="1,2,3,4", stroke_style="dash"))
    assert "dash" not in result.style
    assert any("solid" in note.detail for note in result.of_kind(NoteKind.IGNORED))


# ── the result has to be readable by a human ──────────────────────────────────


def test_the_summary_of_a_clean_capture_names_what_was_captured():
    summary = capture_style(_fill(fill_color="79,157,222,102")).summary()
    assert "fill" in summary


def test_the_summary_states_the_loss_and_not_only_the_win():
    result = capture_style(_fill(fill_color="79,157,222,102", brush_style="horizontal"))
    assert "horizontal" in result.summary()


def test_a_refusals_summary_carries_the_remedy_as_well_as_the_reason():
    # A refusal that does not name what to do instead is a dead end wearing an
    # explanation.
    result = capture_style(RendererDescription("categorizedSymbol", category_count=5))
    assert result.refusal.reason in result.summary()
    assert result.refusal.remedy in result.summary()


@pytest.mark.parametrize(
    "renderer",
    [
        RendererDescription("categorizedSymbol", category_count=5),
        RendererDescription("pointCluster"),
        RendererDescription("nullSymbol"),
        _fill(stroke_width=1.0, stroke_width_unit="MapUnit"),
        _fill(fill_color="not a colour"),
        _fill(),
    ],
)
def test_no_refusal_leaves_the_analyst_with_nothing_to_do(renderer):
    result = capture_style(renderer)
    assert not result.captured
    assert result.refusal.remedy.strip()


# ── the known non-empty diffs, recorded so they are decisions and not surprises ──


def test_the_forward_paths_invented_marker_outline_comes_back_as_a_real_key():
    """A style block with no `stroke_width` renders with QGIS's 0.5, and 0.5 is what a
    reader sees. The captured block therefore says something the stored one did not.

    Recorded rather than worked around: the capture is right about what the symbol holds,
    and the proposal step is where "this key was a default, not a choice" belongs.
    """
    style = {"fill": "#5ec8a0", "stroke": "#2f7f63", "radius": 3}
    captured = capture_style(_through_qgis("Point", style)).style
    assert captured == {**style, "stroke_width": 0.5}
