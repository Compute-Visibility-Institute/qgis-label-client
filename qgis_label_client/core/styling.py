"""Translating the registry's ``style`` block into QGIS symbol properties.

The same ``label_class.style`` JSONB drives the QGIS renderer and the web viewer. That is
the point of it living in the database: the two surfaces cannot drift, and changing a
class colour is a row update rather than two releases.

Its vocabulary is web-shaped -- ``fill``, ``stroke``, ``stroke_width``, ``radius``,
``dash`` -- so the translation is here, and two conversions in it are deliberate:

* **widths and sizes are rendered in pixels, not millimetres.** QGIS defaults to
  millimetres; the style block's numbers were written for a browser. A ``stroke_width``
  of 2 is a hairline in a browser and a fat band at 2 mm on a map, and the whole reason
  the block is shared is that the two views should look alike.
* **``radius`` becomes a diameter.** MapLibre's ``circle-radius`` is a radius; QGIS's
  marker ``size`` is a diameter. Passing the number through unchanged draws cooling units
  at twice their intended size, which on a campus with 872 of them is the difference
  between dots and a solid blanket.

Colours are parsed here rather than handed to Qt because the block uses CSS
``#RRGGBBAA`` and Qt's hex-with-alpha form is ``#AARRGGBB``. The two are silently
compatible-looking and completely different.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .expressions import identifier, literal
from .fields import DEFAULT_FIELDS, CoreFields

#: QGIS parses "r,g,b,a" unambiguously in every symbol-layer property.
_TRANSPARENT = "0,0,0,0"

_DEFAULT_STROKE = "#e0e0e0"
_DEFAULT_FILL = "#00000000"
_DEFAULT_WIDTH = 1.0
_DEFAULT_RADIUS = 3.0

#: Stroke colour for a belief that has since ended -- deleted, or corrected into something
#: else. An alert colour rather than a class colour, because "we no longer think this" is
#: not a property of the class and reading it as one would be the whole point missed.
SUPERSEDED_STROKE = "#a4243b"

#: Opacity of a whole historical layer. One call on the layer rather than a per-symbol
#: alpha: it survives any renderer, reads as "ghost" at any zoom, and cannot be undone by
#: a class whose own style block sets an opaque fill.
HISTORICAL_OPACITY = 0.55


def _try_parse_color(text: str) -> tuple[int, int, int, int] | None:
    """Parse one colour string, or return ``None`` if it is not one."""
    text = text.strip()
    if not text:
        return None

    if text.startswith("#"):
        digits = text[1:]
        if len(digits) in (3, 4):  # shorthand: each digit doubles
            digits = "".join(ch * 2 for ch in digits)
        if len(digits) not in (6, 8):
            return None
        try:
            channels = [int(digits[i : i + 2], 16) for i in range(0, len(digits), 2)]
        except ValueError:
            return None
        if len(channels) == 3:
            channels.append(255)
        return (channels[0], channels[1], channels[2], channels[3])

    parts = [part.strip() for part in text.split(",")]
    if len(parts) not in (3, 4):
        return None
    try:
        channels = [max(0, min(255, int(float(part)))) for part in parts]
    except ValueError:
        return None
    if len(channels) == 3:
        channels.append(255)
    return (channels[0], channels[1], channels[2], channels[3])


def parse_color(value: Any, default: str = _DEFAULT_FILL) -> tuple[int, int, int, int]:
    """Parse a CSS colour into ``(r, g, b, a)`` with a 0-255 alpha.

    Accepts ``#RGB``, ``#RGBA``, ``#RRGGBB``, ``#RRGGBBAA`` and an ``"r,g,b"`` /
    ``"r,g,b,a"`` string. Anything unrecognised falls back to `default`, and then to
    fully transparent, rather than raising: a typo in one class's colour should make that
    class look wrong, not stop the whole registry from loading.
    """
    if isinstance(value, str):
        parsed = _try_parse_color(value)
        if parsed is not None:
            return parsed
    return _try_parse_color(default) or (0, 0, 0, 0)


def qgis_color(value: Any, default: str = _DEFAULT_FILL) -> str:
    """Render a style colour as the ``"r,g,b,a"`` string QGIS symbol properties take."""
    r, g, b, a = parse_color(value, default)
    return f"{r},{g},{b},{a}"


def _number(style: Mapping[str, Any], key: str, default: float) -> float:
    value = style.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _dash_pattern(style: Mapping[str, Any]) -> str | None:
    """Render ``dash: [6, 3]`` as QGIS's ``"6;3"`` custom-dash string."""
    dash = style.get("dash")
    if not isinstance(dash, Sequence) or isinstance(dash, (str, bytes)) or not dash:
        return None
    try:
        return ";".join(f"{float(part):g}" for part in dash)
    except (TypeError, ValueError):
        return None


def _is_opaque_enough_to_draw(colour: str) -> bool:
    return not colour.endswith(",0")


def fill_symbol_properties(style: Mapping[str, Any], historical: bool = False) -> dict[str, str]:
    """Simple-fill properties for a polygon class."""
    fill = qgis_color(style.get("fill"), _DEFAULT_FILL)
    stroke = qgis_color(style.get("stroke"), _DEFAULT_STROKE)
    properties: dict[str, str] = {
        "color": fill,
        # A fully transparent fill is how the seed says "outline only" for the compound
        # and administrative classes. Draw no fill at all rather than an invisible one:
        # an invisible fill still makes the whole polygon clickable, which on a 3428 km
        # national layer means every click selects a compound.
        "style": "solid" if _is_opaque_enough_to_draw(fill) else "no",
        "outline_color": stroke,
        "outline_width": f"{_number(style, 'stroke_width', _DEFAULT_WIDTH):g}",
        "outline_width_unit": "Pixel",
        "outline_style": "solid",
        "joinstyle": "miter",
    }
    dash = _dash_pattern(style)
    if dash:
        properties["outline_style"] = "dash"
        properties["use_custom_dash"] = "1"
        properties["customdash"] = dash
        properties["customdash_unit"] = "Pixel"
    if historical:
        properties["outline_style"] = "dash"
    return properties


def line_symbol_properties(style: Mapping[str, Any], historical: bool = False) -> dict[str, str]:
    """Simple-line properties for a linear class."""
    properties: dict[str, str] = {
        "line_color": qgis_color(style.get("stroke"), _DEFAULT_STROKE),
        "line_width": f"{_number(style, 'stroke_width', _DEFAULT_WIDTH):g}",
        "line_width_unit": "Pixel",
        "line_style": "solid",
        "capstyle": "round",
    }
    dash = _dash_pattern(style)
    if dash:
        properties["line_style"] = "dash"
        properties["use_custom_dash"] = "1"
        properties["customdash"] = dash
        properties["customdash_unit"] = "Pixel"
    if historical:
        properties["line_style"] = "dash"
    return properties


def marker_symbol_properties(style: Mapping[str, Any], historical: bool = False) -> dict[str, str]:
    """Simple-marker properties for a point class."""
    radius = _number(style, "radius", _DEFAULT_RADIUS)
    properties = {
        "name": "circle",
        "color": qgis_color(style.get("fill"), _DEFAULT_STROKE),
        "outline_color": qgis_color(style.get("stroke"), _DEFAULT_STROKE),
        "outline_width": f"{_number(style, 'stroke_width', 0.5):g}",
        "outline_width_unit": "Pixel",
        # radius -> diameter. See the module docstring.
        "size": f"{radius * 2:g}",
        "size_unit": "Pixel",
    }
    if historical:
        properties["outline_style"] = "dash"
    return properties


#: Which symbol builder a class's ``geom_type`` needs. Keyed on the values
#: 003_class.sql allows; anything else -- including 'Any' -- gets the fill symbol, which
#: is the least surprising default for a mixed-geometry collection.
GEOMETRY_KIND: dict[str, str] = {
    "Point": "marker",
    "MultiPoint": "marker",
    "LineString": "line",
    "MultiLineString": "line",
    "Polygon": "fill",
    "MultiPolygon": "fill",
}


def symbol_kind(geom_type: str) -> str:
    return GEOMETRY_KIND.get(geom_type, "fill")


def symbol_properties(
    geom_type: str, style: Mapping[str, Any], historical: bool = False
) -> dict[str, str]:
    """Symbol properties appropriate to the class's geometry type.

    `historical` dashes the stroke, and nothing else. A dashed boundary is the strongest
    "not editable" convention in GIS and it costs one property; the class colours stay
    exactly as they are, because a historical layer is still a labels layer and the colours
    are how people read it. Changing them would make the two layers harder to compare,
    which is the entire reason both are open at once.
    """
    kind = symbol_kind(geom_type)
    if kind == "marker":
        return marker_symbol_properties(style, historical)
    if kind == "line":
        return line_symbol_properties(style, historical)
    return fill_symbol_properties(style, historical)


def superseded_stroke_expression(
    style: Mapping[str, Any], fields: CoreFields = DEFAULT_FIELDS
) -> str:
    """A QGIS expression colouring a stroke by whether the belief has since ended.

    Applied as a DATA-DEFINED PROPERTY on the ordinary categorized symbols rather than by
    rebuilding the class categories as sub-rules of a rule-based renderer. Two reasons, and
    the first is the one that decides it: data-defined properties are plain QGIS symbology,
    so they survive the ``exportNamedStyle``/``importNamedStyle`` round trip
    :func:`..layers.repoint_layer` performs on every re-point -- a track switch or an as-of
    change would otherwise silently strip the distinction. The second is that a rule-based
    renderer would mean rebuilding every category as a pair of rules, which is a great deal
    more code for the same three visual states.

    The class colour is passed through unchanged, so a class that is retired or restyled on
    the server keeps looking like itself here.
    """
    stroke = qgis_color(style.get("stroke"), _DEFAULT_STROKE)
    alert = qgis_color(SUPERSEDED_STROKE, _DEFAULT_STROKE)
    return f"if({identifier(fields.superseded)}, {literal(alert)}, {literal(stroke)})"
