"""Reading a real QGIS renderer through :func:`layers.capture_layer_style`.

``core/stylecapture.py`` is tested in isolation, with no QGIS import in sight, and
``tests/test_stylecapture.py``'s round trip already proves that module correctly inverts
``core/styling.py`` -- the millimetres, the radius/diameter, and the colour byte order --
given a plain description. What is NOT proven there is that this reader BUILDS that
description correctly from real QGIS objects: that a fill's colour comes from
``fillColor()`` and a line's from ``color()``, that a marker's ``size()`` reaches the
description as a diameter untouched, that a stroke width's unit is read from the same
accessor its number came from.

So every Fake below duck-types exactly the accessors ``layers.py``'s reader calls for one
symbol-layer type, and no others -- calling ``fillColor()`` on a ``SimpleLine`` Fake
raises ``AttributeError``, exactly as it would against the real binding, instead of
returning a plausible value that would hide a wrong-accessor bug. Enum-typed fields
(brush style, pen style, the data-defined property keys) are handed real ``Qt``/
``QgsSymbolLayer`` enum members pulled independently of the reader's own by-name
resolvers, so a broken resolver actually fails these tests rather than being tested
against itself.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from qgis.core import QgsSymbolLayer
from qgis.PyQt.QtCore import Qt

from qgis_label_client import layers as layer_tools
from qgis_label_client.core import stylecapture, styling
from qgis_label_client.core.stylecapture import NoteKind

# ── enum sentinels, resolved independently of the reader's own by-name lookups ─────────

NO_BRUSH = Qt.BrushStyle.NoBrush
SOLID_BRUSH = Qt.BrushStyle.SolidPattern
HATCH_BRUSH = Qt.BrushStyle.HorPattern

NO_PEN = Qt.PenStyle.NoPen
SOLID_PEN = Qt.PenStyle.SolidLine
DASH_PEN = Qt.PenStyle.DashLine

_PROPERTY_KEYS = {
    "FillColor": QgsSymbolLayer.Property.FillColor,
    "StrokeColor": QgsSymbolLayer.Property.StrokeColor,
    "StrokeWidth": QgsSymbolLayer.Property.StrokeWidth,
    "Size": QgsSymbolLayer.Property.Size,
}


# ── test doubles ─────────────────────────────────────────────────────────────


class FakeQColor:
    """``QColor``'s four channel accessors, and nothing else -- see stylecapture.py's
    module docstring for why ``red()``/``green()``/``blue()``/``alpha()`` are the only
    four methods the reader is allowed to call on a colour.
    """

    def __init__(self, r: int, g: int, b: int, a: int = 255) -> None:
        self._r, self._g, self._b, self._a = r, g, b, a

    def red(self) -> int:
        return self._r

    def green(self) -> int:
        return self._g

    def blue(self) -> int:
        return self._b

    def alpha(self) -> int:
        return self._a


class FakeDataDefinedProperties:
    """Stands in for ``QgsPropertyCollection``: ``isActive(key)`` and nothing else.

    `active_names` is resolved against ``QgsSymbolLayer.Property`` directly, not through
    ``layers._symbol_layer_property_key`` -- using the reader's own resolver to build the
    fixture would make a broken resolver invisible to every test that depends on it.
    """

    def __init__(self, active_names: tuple[str, ...] = ()) -> None:
        self._active = {_PROPERTY_KEYS[name] for name in active_names}

    def isActive(self, key) -> bool:  # noqa: N802 - Qt naming
        return key in self._active


#: Which accessors _describe_symbol_layer may call for each simple layer type. Anything
#: not listed here is not attached to the Fake at all -- see FakeSymbolLayer.
_ALLOWED_METHODS: dict[str, frozenset[str]] = {
    "SimpleFill": frozenset(
        {
            "fillColor",
            "strokeColor",
            "strokeWidth",
            "strokeWidthUnit",
            "strokeStyle",
            "brushStyle",
            "useCustomDashPattern",
            "customDashVector",
            "customDashPatternUnit",
        }
    ),
    "SimpleLine": frozenset(
        {
            "color",
            "width",
            "widthUnit",
            "penStyle",
            "useCustomDashPattern",
            "customDashVector",
            "customDashPatternUnit",
        }
    ),
    "SimpleMarker": frozenset(
        {
            "fillColor",
            "strokeColor",
            "strokeWidth",
            "strokeWidthUnit",
            "strokeStyle",
            "brushStyle",
            "size",
            "sizeUnit",
            "shape",
        }
    ),
}


class FakeSymbolLayer:
    """Duck-types exactly the accessors ``_describe_symbol_layer`` may call for `type_name`.

    Only the methods real QGIS actually declares on that layer type are attached: a reader
    bug that calls ``fillColor()`` on a ``SimpleLine`` (which has none -- ``color()`` IS
    the line's one colour) raises ``AttributeError`` here exactly as it would against the
    real binding, instead of returning a plausible value that hides the bug. This is what
    makes the colour-format and wrong-accessor traps mutation-testable at all.
    """

    def __init__(self, type_name: str, **overrides: object) -> None:
        self.type_name = type_name
        self._enabled = overrides.pop("enabled", True)
        self._active_data_defined = overrides.pop("activeDataDefined", ())
        self._values: dict[str, object] = {
            "fillColor": FakeQColor(0, 0, 0, 255),
            "strokeColor": FakeQColor(0, 0, 0, 255),
            "color": FakeQColor(0, 0, 0, 255),
            "strokeWidth": 1.0,
            "width": 1.0,
            "strokeWidthUnit": "Pixel",
            "widthUnit": "Pixel",
            "strokeStyle": SOLID_PEN,
            "penStyle": SOLID_PEN,
            "brushStyle": SOLID_BRUSH,
            "size": 6.0,
            "sizeUnit": "Pixel",
            "shape": "circle",
            "useCustomDashPattern": False,
            "customDashVector": [],
            "customDashPatternUnit": "Pixel",
        }
        for key, value in overrides.items():
            if key not in self._values:
                raise TypeError(f"FakeSymbolLayer got an unexpected override {key!r}")
            self._values[key] = value

    def layerType(self) -> str:  # noqa: N802 - Qt naming
        return self.type_name

    def enabled(self) -> bool:
        return self._enabled

    def dataDefinedProperties(self) -> FakeDataDefinedProperties:  # noqa: N802
        return FakeDataDefinedProperties(self._active_data_defined)

    def __getattr__(self, name: str):
        allowed = _ALLOWED_METHODS.get(self.type_name, frozenset())
        if name in allowed:
            value = self._values[name]
            return lambda: value
        raise AttributeError(
            f"{self.type_name!r} test double has no {name}() -- real QGIS does not either"
        )


class FakeSymbol:
    def __init__(self, layers, opacity: float = 1.0, type_: str = "Fill") -> None:
        self._layers = tuple(layers)
        self._opacity = opacity
        self._type = type_

    def symbolLayers(self):  # noqa: N802 - Qt naming
        return list(self._layers)

    def opacity(self) -> float:
        return self._opacity

    def type(self) -> str:
        return self._type

    def symbolTypeToString(self, type_) -> str:  # noqa: N802 - Qt naming
        return self._type


class FakeSingleSymbolRenderer:
    def __init__(self, symbol: FakeSymbol) -> None:
        self._symbol = symbol

    def type(self) -> str:
        return "singleSymbol"

    def symbol(self) -> FakeSymbol:
        return self._symbol


class FakeCategorizedRenderer:
    def __init__(self, field: str, count: int) -> None:
        self._field = field
        self._categories = [object()] * count

    def type(self) -> str:
        return "categorizedSymbol"

    def classAttribute(self) -> str:  # noqa: N802 - Qt naming
        return self._field

    def categories(self):
        return list(self._categories)


class FakeGraduatedRenderer:
    """Shaped like ``QgsGraduatedSymbolRenderer``, which shares ``classAttribute()`` with
    the categorized renderer but counts its classes through ``ranges()`` instead of
    ``categories()`` -- the real API has no ``categories()`` on this class at all, so a
    reader that only ever tried ``categories`` would silently count zero.
    """

    def __init__(self, field: str, count: int) -> None:
        self._field = field
        self._ranges = [object()] * count

    def type(self) -> str:
        return "graduatedSymbol"

    def classAttribute(self) -> str:  # noqa: N802 - Qt naming
        return self._field

    def ranges(self):
        return list(self._ranges)


class FakeVectorLayer:
    def __init__(
        self,
        renderer,
        opacity: float = 1.0,
        blend_mode: object = None,
        scale_visibility: bool = False,
        labels_enabled: bool = False,
    ) -> None:
        self._renderer = renderer
        self._opacity = opacity
        # Defaults to a mode that reads as "Normal", so a test that does not care about
        # blending does not pick up a spurious "blend mode was ignored" note.
        self._blend_mode = blend_mode if blend_mode is not None else SimpleNamespace(name="Normal")
        self._scale_visibility = scale_visibility
        self._labels_enabled = labels_enabled

    def renderer(self):
        return self._renderer

    def opacity(self) -> float:
        return self._opacity

    def blendMode(self):  # noqa: N802 - Qt naming
        return self._blend_mode

    def hasScaleBasedVisibility(self) -> bool:  # noqa: N802
        return self._scale_visibility

    def labelsEnabled(self) -> bool:  # noqa: N802
        return self._labels_enabled


def _layer_with(*layers: FakeSymbolLayer, **kwargs) -> FakeVectorLayer:
    return FakeVectorLayer(FakeSingleSymbolRenderer(FakeSymbol(layers)), **kwargs)


@pytest.fixture(autouse=True)
def _stub_stable_encoders(monkeypatch):
    """Stand in for QGIS's own stable string encoders: ``encodeUnit``, ``encodeShape`` and
    ``getBlendModeEnum``.

    The design behind this reader confirms none of the three need by-name resolution
    across QGIS versions -- what the reader has to get right is threading their RESULT
    through correctly, not re-implementing QGIS's own string serialization (which these
    stubs, absent a real QGIS on this machine, are not equipped to do faithfully anyway).
    Each stand-in hands back exactly what it is given, so a Fake's raw unit/shape/mode
    value IS the string ``capture_style`` has to see -- which is what every test below
    actually exercises.
    """
    monkeypatch.setattr(layer_tools, "QgsUnitTypes", SimpleNamespace(encodeUnit=lambda u: u))
    monkeypatch.setattr(
        layer_tools, "QgsSimpleMarkerSymbolLayerBase", SimpleNamespace(encodeShape=lambda s: s)
    )
    monkeypatch.setattr(
        layer_tools, "QgsPainting", SimpleNamespace(getBlendModeEnum=lambda mode: mode)
    )


# ── describe_renderer answers only what a bare renderer can ────────────────────


def test_describe_renderer_reads_a_single_symbol_renderer():
    layer = FakeSymbolLayer("SimpleFill", fillColor=FakeQColor(1, 2, 3, 4))
    description = layer_tools.describe_renderer(FakeSingleSymbolRenderer(FakeSymbol([layer])))
    assert description.renderer_type == "singleSymbol"
    assert description.symbol.layers[0].type_name == "SimpleFill"
    # layer_opacity, blend_mode, scale_visibility, labels_enabled: a renderer alone cannot
    # answer any of them, so they stay at RendererDescription's own defaults here.
    assert description.layer_opacity == 1.0
    assert description.blend_mode == ""
    assert description.labels_enabled is False


def test_describe_renderer_names_the_category_count_and_field():
    description = layer_tools.describe_renderer(FakeCategorizedRenderer("kind", 5))
    assert description.renderer_type == "categorizedSymbol"
    assert description.category_field == "kind"
    assert description.category_count == 5
    assert description.symbol is None


def test_describe_renderer_counts_a_graduated_renderers_ranges_not_just_categories():
    # QgsGraduatedSymbolRenderer has no categories() at all -- ranges() is the real
    # accessor for its class list. A reader that only tried "categories" would come back
    # 0 here and stylecapture.py would refuse with the generic "several symbols" wording
    # instead of naming the actual count.
    description = layer_tools.describe_renderer(FakeGraduatedRenderer("population", 4))
    assert description.renderer_type == "graduatedSymbol"
    assert description.category_field == "population"
    assert description.category_count == 4


def test_capture_layer_style_refuses_a_layer_with_no_renderer_at_all():
    result = layer_tools.capture_layer_style(FakeVectorLayer(renderer=None))
    assert not result.captured
    assert result.refusal is stylecapture.NO_SYMBOL


def test_capture_layer_style_layers_map_layer_properties_onto_the_description():
    """The split this reader is built around: opacity, blend mode, scale-visibility and
    labels are QgsMapLayer properties, not renderer ones, and only capture_layer_style
    reads the layer at all.
    """
    layer = FakeSymbolLayer("SimpleFill", fillColor=FakeQColor(79, 157, 222, 255))
    fake_layer = _layer_with(
        layer,
        blend_mode=SimpleNamespace(name="Multiply"),
        scale_visibility=True,
        labels_enabled=True,
    )
    result = layer_tools.capture_layer_style(fake_layer)
    assert result.captured
    ignored = " ".join(note.detail for note in result.of_kind(NoteKind.IGNORED))
    assert "Multiply" in ignored
    assert "zooms" in ignored
    assert "labels" in ignored


# ── case 1: a translucent fill with a non-default stroke width, in pixels ──────


def test_a_translucent_fill_and_a_pixel_stroke_width_round_trip():
    layer = FakeSymbolLayer(
        "SimpleFill",
        fillColor=FakeQColor(79, 157, 222, 102),
        strokeColor=FakeQColor(79, 157, 222, 255),
        strokeWidth=2.5,
        strokeWidthUnit="Pixel",
    )
    result = layer_tools.capture_layer_style(_layer_with(layer))
    assert result.captured
    assert result.style == {"fill": "#4f9dde66", "stroke": "#4f9dde", "stroke_width": 2.5}

    # Closes the loop: re-render the captured block through the already-tested styling
    # module and check the reconstructed numbers match what the Fake actually held.
    rerendered = styling.fill_symbol_properties(result.style)
    assert rerendered["color"] == "79,157,222,102"
    assert rerendered["outline_color"] == "79,157,222,255"
    assert float(rerendered["outline_width"]) == 2.5


# ── cases 2 and 3: marker size is a diameter, halved into a radius ─────────────


def test_a_marker_with_an_integer_diameter_halves_cleanly_into_a_radius():
    layer = FakeSymbolLayer("SimpleMarker", size=6.0, sizeUnit="Pixel")
    result = layer_tools.capture_layer_style(_layer_with(layer))
    assert result.style["radius"] == 3
    rerendered = styling.marker_symbol_properties(result.style)
    assert float(rerendered["size"]) == 6.0


def test_a_marker_with_an_odd_diameter_halves_to_a_half_pixel_radius():
    layer = FakeSymbolLayer("SimpleMarker", size=7.0, sizeUnit="Pixel")
    result = layer_tools.capture_layer_style(_layer_with(layer))
    assert result.style["radius"] == 3.5
    rerendered = styling.marker_symbol_properties(result.style)
    assert float(rerendered["size"]) == 7.0


def test_a_marker_sized_in_map_units_is_refused_end_to_end_through_the_reader():
    # There is no fixed pixel width for a map-unit size -- it is a different number of
    # pixels at every zoom. core/stylecapture.py already refuses Unit.MAP_UNITS at the
    # pure-function level; this proves the READER actually reports "MapUnit" (QGIS's own
    # spelling, per _stub_stable_encoders standing in for encodeUnit) as that unit in the
    # first place, rather than defaulting an unfamiliar value to millimetres and silently
    # capturing a wrong-and-different-at-every-zoom size as if it were fixed.
    layer = FakeSymbolLayer("SimpleMarker", size=6.0, sizeUnit="MapUnit")
    result = layer_tools.capture_layer_style(_layer_with(layer))
    assert not result.captured
    assert "map units" in result.refusal.message


def test_a_stroke_width_in_map_units_is_refused_end_to_end_through_the_reader():
    layer = FakeSymbolLayer(
        "SimpleFill",
        fillColor=FakeQColor(79, 157, 222, 255),
        strokeWidth=1.0,
        strokeWidthUnit="MapUnit",
    )
    result = layer_tools.capture_layer_style(_layer_with(layer))
    assert not result.captured
    assert "map units" in result.refusal.message


# ── case 4: a line with a custom dash pattern in millimetres ───────────────────


def test_a_line_with_a_custom_dash_pattern_in_millimetres_converts_to_pixels():
    layer = FakeSymbolLayer(
        "SimpleLine",
        color=FakeQColor(232, 197, 71, 255),
        width=2.0,
        widthUnit="MM",
        penStyle=DASH_PEN,
        useCustomDashPattern=True,
        customDashVector=[2.0, 1.0],
        customDashPatternUnit="MM",
    )
    result = layer_tools.capture_layer_style(_layer_with(layer))
    assert result.style["stroke_width"] == 7.56
    assert result.style["dash"] == [7.56, 3.78]

    rerendered = styling.line_symbol_properties(result.style)
    assert rerendered["customdash"] == "7.56;3.78"
    assert float(rerendered["line_width"]) == 7.56


# ── case 5: an active data-defined override is reported, not silently dropped ──


def test_an_active_data_defined_stroke_colour_override_is_captured_and_reported():
    layer = FakeSymbolLayer(
        "SimpleFill",
        fillColor=FakeQColor(79, 157, 222, 255),
        activeDataDefined=("StrokeColor",),
    )
    result = layer_tools.capture_layer_style(_layer_with(layer))
    assert result.captured
    assert any("StrokeColor" in note.detail for note in result.of_kind(NoteKind.IGNORED))


def test_all_four_data_defined_names_the_vocabulary_could_lose_are_recognised():
    layer = FakeSymbolLayer(
        "SimpleMarker",
        fillColor=FakeQColor(1, 2, 3, 4),
        activeDataDefined=("FillColor", "StrokeColor", "StrokeWidth", "Size"),
    )
    result = layer_tools.capture_layer_style(_layer_with(layer))
    detail = " ".join(note.detail for note in result.of_kind(NoteKind.IGNORED))
    for name in ("FillColor", "StrokeColor", "StrokeWidth", "Size"):
        assert name in detail


def test_a_data_defined_override_that_is_set_but_not_active_is_not_reported():
    # isActive(), not merely present -- an override that is defined but switched off draws
    # nothing different from the plain colour, and reporting it would be a false alarm the
    # analyst cannot do anything about.
    layer = FakeSymbolLayer("SimpleFill", fillColor=FakeQColor(79, 157, 222, 255))
    result = layer_tools.capture_layer_style(_layer_with(layer))
    assert result.of_kind(NoteKind.IGNORED) == ()


# ── case 6: a multi-layer symbol, order preserved through the reader itself ────


def test_a_second_unrecognised_layer_is_read_and_reported_as_dropped():
    first = FakeSymbolLayer("SimpleFill", fillColor=FakeQColor(79, 157, 222, 102))
    second = FakeSymbolLayer("LinePatternFill")
    result = layer_tools.capture_layer_style(_layer_with(first, second))
    assert result.style["fill"] == "#4f9dde66"
    dropped = result.of_kind(NoteKind.DROPPED)
    assert len(dropped) == 1
    assert "LinePatternFill" in dropped[0].detail


def test_a_simple_layer_found_under_an_unrecognised_one_is_still_captured():
    # Proves the READER preserves symbolLayers()'s order, not just that the pure function
    # can find a simple layer wherever it sits -- see _describe_symbol's docstring.
    first = FakeSymbolLayer("ShapeburstFill")
    second = FakeSymbolLayer("SimpleFill", fillColor=FakeQColor(79, 157, 222, 102))
    result = layer_tools.capture_layer_style(_layer_with(first, second))
    assert result.style["fill"] == "#4f9dde66"
    assert "ShapeburstFill" in result.of_kind(NoteKind.DROPPED)[0].detail


def test_a_disabled_layer_is_read_but_not_reported_as_a_loss():
    off = FakeSymbolLayer("SimpleFill", fillColor=FakeQColor(1, 2, 3, 4), enabled=False)
    on = FakeSymbolLayer("SimpleFill", fillColor=FakeQColor(79, 157, 222, 102))
    result = layer_tools.capture_layer_style(_layer_with(off, on))
    assert result.style["fill"] == "#4f9dde66"
    assert result.of_kind(NoteKind.DROPPED) == ()


# ── a line's colour is a stroke, never a fill (the accessor-table trap) ────────


def test_a_line_layers_colour_comes_from_color_not_a_fill_accessor():
    # SimpleLine has one colour, not a fill and a stroke -- color() IS the stroke. The
    # Fake only attaches color()/width()/widthUnit() for a SimpleLine (see
    # _ALLOWED_METHODS), so a reader that mistakenly reached for fillColor()/
    # strokeColor() here would raise AttributeError rather than reading a plausible value.
    layer = FakeSymbolLayer("SimpleLine", color=FakeQColor(232, 197, 71, 204))
    result = layer_tools.capture_layer_style(_layer_with(layer))
    assert result.style == {"stroke": "#e8c547cc", "stroke_width": 1}


# ── brush style, pen style and marker shape resolve through real enum members ──


def test_a_no_brush_polygon_keeps_its_colour_with_the_alpha_zeroed():
    layer = FakeSymbolLayer(
        "SimpleFill", fillColor=FakeQColor(79, 157, 222, 200), brushStyle=NO_BRUSH
    )
    result = layer_tools.capture_layer_style(_layer_with(layer))
    assert result.style["fill"] == "#4f9dde00"


def test_a_hatch_brush_style_is_captured_and_reported_as_a_pattern():
    layer = FakeSymbolLayer(
        "SimpleFill", fillColor=FakeQColor(79, 157, 222, 102), brushStyle=HATCH_BRUSH
    )
    result = layer_tools.capture_layer_style(_layer_with(layer))
    assert result.style["fill"] == "#4f9dde66"
    assert any("ignored" in note.detail for note in result.of_kind(NoteKind.IGNORED))


def test_a_no_pen_stroke_is_recorded_as_an_explicit_no_outline():
    layer = FakeSymbolLayer(
        "SimpleFill", fillColor=FakeQColor(79, 157, 222, 102), strokeStyle=NO_PEN
    )
    result = layer_tools.capture_layer_style(_layer_with(layer))
    assert result.style["stroke"] == "#00000000"


def test_a_non_circle_marker_shape_is_captured_and_reported():
    layer = FakeSymbolLayer("SimpleMarker", size=6.0, sizeUnit="Pixel", shape="square")
    result = layer_tools.capture_layer_style(_layer_with(layer))
    assert result.captured
    assert any("square" in note.detail for note in result.of_kind(NoteKind.IGNORED))
