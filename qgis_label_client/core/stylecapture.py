"""Reading an analyst's existing QGIS symbology back into the registry's ``style`` block.

The inverse of :mod:`.styling`, and it exists so that bootstrapping layers from somebody's
own QGIS can *propose* the symbology they already have, instead of making them re-choose
their own colours in the admin console afterwards.

Every conversion :mod:`.styling` documents as a hazard on the way out is a silent wrong
answer on the way back in, and all three fail the same way: the captured style looks
correct in the analyst's own QGIS and wrong in the browser. The person who made the
mistake is the one person who cannot see it.

* **widths and sizes are pixels here and millimetres in QGIS.** QGIS's default outline is
  0.26 mm, which is 1 pixel; captured as ``0.26`` it is a quarter of a pixel and the
  viewer draws nothing at all. The unit is read from the symbol and converted, and a unit
  with no fixed pixel width is refused rather than guessed at.
* **``radius`` is a radius; QGIS's marker ``size`` is a diameter.** Halved on the way in,
  because :mod:`.styling` doubles it on the way out -- and passing it through unchanged is
  what draws 872 cooling units on one campus at twice their size, "the difference between
  dots and a solid blanket".
* **colours are emitted as CSS ``#RRGGBBAA``**, never Qt's ``#AARRGGBB``. ``#4f9dde66``
  and ``#664f9dde`` both parse and only one is the colour the analyst chose. This is why
  :class:`SymbolLayerDescription` asks for colours as ``"r,g,b,a"``: it is the one form
  that cannot be read in the wrong order, and it is what QGIS symbol properties use.

UNITS ARRIVE IN QGIS'S SPELLING, NOT THIS MODULE'S

``QgsUnitTypes.encodeUnit()`` writes "MM", "Pixel", "MapUnit", "RenderMetersInMapUnits",
and :mod:`.styling` puts "Pixel" into every symbol it builds. Those are the strings a
reader actually has, so every spelling is normalised before anything is converted. An
earlier draft matched only this module's own tidier names and refused every real symbol
with it -- including the ones already in pixels, which need no conversion at all. Failing
closed made that survivable; it did not make it usable.

REFUSING IS THE FEATURE, NOT A LIMITATION

The registry holds one style per class, and QGIS does not. A layer categorised into five
symbols is telling you it should be five classes; collapsing it to one arbitrary symbol
throws that away and nobody finds out. So an attribute-driven renderer is refused, and so
is a width whose unit has no honest pixel value, and so is a colour string that does not
parse -- :func:`.styling.parse_color` falls back to transparent rather than raising, and a
fallback nobody notices proposes an invisible class as a success.

Things with no representation in the vocabulary at all -- data-defined overrides, blend
modes, scale-dependent visibility, labels, hatch patterns, a square or star marker shape,
the second and third layers of a stacked symbol -- are ignored rather than refused, and
every one of them is REPORTED. The analyst is going to notice that the web viewer looks
plainer than their QGIS; the report is the difference between that being a known trade and
a bug.

OPACITY IS THREE THINGS IN QGIS AND ONE THING HERE

QGIS has layer opacity, symbol opacity and the colour's own alpha, and applies the product
of the three. The style block has one alpha channel, so the three are MULTIPLIED into it:
the captured colour is what the analyst sees on screen, which is the thing they were
actually choosing. The cost is that a fully opaque fill on a 50% layer comes back as
``...80``-ish alpha and does not match the swatch in their colour picker, so any composed
opacity is reported. Composing is defensible; composing silently is not.

Nothing here imports QGIS. The input is a plain description of a renderer that a thin
QGIS-facing reader populates from a ``QgsVectorLayer`` -- the same split every other
``core`` module uses, and the reason these three conversions can be tested at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .styling import parse_color

#: The dpi at which the numbers in a style block are pixels.
#:
#: 96 is CSS's reference pixel, and the browser is the surface this vocabulary is written
#: for. QGIS renders the canvas at the screen's real dpi, so a width captured on a HiDPI
#: display is a few per cent out -- a different order of wrong from the 3.78x that passing
#: millimetres through unchanged would be, which is the mistake this constant prevents.
REFERENCE_DPI = 96.0

PIXELS_PER_MILLIMETRE = REFERENCE_DPI / 25.4

#: Pixels below a hundredth are not a thing a screen or a browser can draw, and unrounded
#: millimetre conversions produce seventeen digits of them. It is not only ugly: a
#: re-import is meant to propose nothing when nothing changed, and two captures of the
#: same symbol that differ in the last bit would propose a change every time.
_DECIMALS = 2

#: The only marker shape the vocabulary can describe: ``radius`` is MapLibre's
#: ``circle-radius``, and :mod:`.styling` hardcodes ``name="circle"`` on the way out.
CIRCLE = "circle"


class Unit(str, Enum):
    """The unit a QGIS symbol property is expressed in.

    The members are this module's names for the units; the strings a reader has to hand
    are QGIS's, so :func:`normalise_unit` maps the second onto the first. Comparing
    against these values directly would refuse everything real -- see the module header.
    """

    PIXELS = "pixel"
    MILLIMETRES = "millimetre"
    POINTS = "point"
    INCHES = "inch"
    #: A width in map units is a different number of pixels at every zoom. There is no
    #: honest conversion, so this is refused rather than approximated.
    MAP_UNITS = "map_unit"
    #: Metres on the ground, which is map units wearing a friendlier name.
    METRES_AT_SCALE = "metres_at_scale"
    #: A proportion of the symbol's own size.
    PERCENTAGE = "percentage"
    UNKNOWN = "unknown"


#: Every spelling of a unit that can reach this module, lowercased and with spaces and
#: hyphens folded to underscores, mapped to what it means.
#:
#: The QGIS spellings are ``QgsUnitTypes.encodeUnit()``'s and the ones its
#: ``decodeRenderUnit()`` accepts; "pixel" is here for :mod:`.styling`, which writes
#: "Pixel" into every ``*_unit`` property it builds, so a style that goes out through the
#: forward path and comes back through this one has to survive its own spelling.
_UNIT_ALIASES: dict[str, Unit] = {
    "px": Unit.PIXELS,
    "pixel": Unit.PIXELS,
    "pixels": Unit.PIXELS,
    "mm": Unit.MILLIMETRES,
    "millimeter": Unit.MILLIMETRES,
    "millimeters": Unit.MILLIMETRES,
    "millimetre": Unit.MILLIMETRES,
    "millimetres": Unit.MILLIMETRES,
    "pt": Unit.POINTS,
    "point": Unit.POINTS,
    "points": Unit.POINTS,
    "in": Unit.INCHES,
    "inch": Unit.INCHES,
    "inches": Unit.INCHES,
    "mapunit": Unit.MAP_UNITS,
    "mapunits": Unit.MAP_UNITS,
    "map_unit": Unit.MAP_UNITS,
    "map_units": Unit.MAP_UNITS,
    "mu": Unit.MAP_UNITS,
    "rendermetersinmapunits": Unit.METRES_AT_SCALE,
    "metersinmapunits": Unit.METRES_AT_SCALE,
    "meterinmapunits": Unit.METRES_AT_SCALE,
    "metres_at_scale": Unit.METRES_AT_SCALE,
    "percentage": Unit.PERCENTAGE,
    "percent": Unit.PERCENTAGE,
    "%": Unit.PERCENTAGE,
    "unknown": Unit.UNKNOWN,
    "": Unit.UNKNOWN,
}


def normalise_unit(unit: Unit | str) -> Unit:
    """What a unit spelling means, or :attr:`Unit.UNKNOWN` for one nobody has taught it.

    An unrecognised name resolves to ``UNKNOWN`` rather than to a guess, and ``UNKNOWN``
    is refused: a unit added to QGIS after this table was written is a unit whose pixel
    value this module does not know, and inventing one is how a stroke ends up 3.78 times
    too wide in a browser nobody has open yet.
    """
    if isinstance(unit, Unit):
        return unit
    name = str(unit).strip().lower().replace(" ", "_").replace("-", "_")
    return _UNIT_ALIASES.get(name, Unit.UNKNOWN)


#: How many pixels one of each convertible unit is worth at :data:`REFERENCE_DPI`. Keyed
#: by members, which is safe only because every lookup goes through
#: :func:`normalise_unit` first: ``Enum`` hashes by name, so a dict keyed by members
#: cannot be found with the bare string a member equals.
_PIXELS_PER: dict[Unit, float] = {
    Unit.PIXELS: 1.0,
    Unit.MILLIMETRES: PIXELS_PER_MILLIMETRE,
    Unit.POINTS: REFERENCE_DPI / 72.0,
    Unit.INCHES: REFERENCE_DPI,
}

#: ``QgsFeatureRenderer.type()`` for the one renderer shape that has a single symbol to
#: read. Everything else is either attribute-driven or draws something the vocabulary
#: cannot describe.
SINGLE_SYMBOL = "singleSymbol"

#: Renderers that style by attribute value. Refused with their own wording, because the
#: right answer is not "pick a symbol" -- it is "these are your classes".
ATTRIBUTE_RENDERERS = frozenset({"categorizedSymbol", "graduatedSymbol", "RuleRenderer"})

#: ``QgsSymbolLayer.layerType()`` values that reduce to one fill and one stroke, mapped to
#: the word :func:`.styling.symbol_kind` uses for the same idea. Anything absent -- a
#: gradient, an SVG fill, a marker line, a font marker, or a type added to QGIS after this
#: was written -- is dropped, which is the safe direction for an unrecognised name.
SIMPLE_LAYER_KINDS: dict[str, str] = {
    "SimpleFill": "fill",
    "SimpleLine": "line",
    "SimpleMarker": "marker",
}


class NoteKind(str, Enum):
    """Why something in the analyst's symbology is not in the captured style."""

    #: The vocabulary could have said it, but only once, and something else said it first.
    DROPPED = "dropped"
    #: The vocabulary has no way to say it at all.
    IGNORED = "ignored"
    #: It was carried across, but only by making an assumption worth stating.
    ASSUMED = "assumed"


@dataclass(frozen=True)
class Note:
    """One thing the analyst will see in QGIS and not in the browser."""

    kind: NoteKind
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True)
class Refusal:
    """Why no style was captured, and what to do about it.

    ``remedy`` is a required field rather than an optional extra sentence. A capture that
    refuses without naming the alternative leaves the analyst with a dialog that says no
    and nothing to click, and the alternative is never obvious: "publish it as separate
    classes" is not something you deduce from "this renderer is categorised".
    """

    reason: str
    remedy: str

    @property
    def message(self) -> str:
        return f"{self.reason} {self.remedy}"

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class SymbolLayerDescription:
    """One QGIS symbol layer, described without importing QGIS to describe it.

    This is the contract the later ``QgsVectorLayer`` reader has to satisfy, and the one
    ambiguity worth spelling out is which QGIS accessor feeds which field -- a line's
    ``color()`` is a STROKE here, not a fill, and reading it as a fill would leave every
    linear class with no visible line at all::

        SimpleFill    fill_color=color()  stroke_color=strokeColor()  stroke_width=strokeWidth()
        SimpleLine    --                  stroke_color=color()        stroke_width=width()
        SimpleMarker  fill_color=color()  stroke_color=strokeColor()  stroke_width=strokeWidth()
                      size=size(), which is a DIAMETER

    Colours are ``"r,g,b,a"`` strings -- ``f"{c.red()},{c.green()},{c.blue()},{c.alpha()}"``
    from a ``QColor``. **Do not pass ``QColor.name(HexArgb)``**: it is ``#AARRGGBB``, it
    looks exactly like a CSS colour, and it parses without complaint into the wrong one.
    ``#RGB``, ``#RRGGBB`` and ``#RRGGBBAA`` are accepted too, for a caller that already
    has one; anything else is refused rather than quietly captured as transparent.

    Units are whatever ``QgsUnitTypes.encodeUnit()`` returns -- "MM", "Pixel", "MapUnit".
    They default to millimetres because that is what QGIS gives a new symbol layer, so a
    reader that mirrors QGIS's own defaults is right by mirroring them.
    """

    #: ``QgsSymbolLayer.layerType()``, verbatim. Kept as QGIS spells it so that an
    #: unrecognised one can be named in the report instead of silently becoming "other".
    type_name: str
    fill_color: str | None = None
    stroke_color: str | None = None
    stroke_width: float | None = None
    stroke_width_unit: Unit | str = Unit.MILLIMETRES
    #: Marker DIAMETER, in `size_unit`. Halved into ``radius`` on capture.
    size: float | None = None
    size_unit: Unit | str = Unit.MILLIMETRES
    #: ``QgsSimpleMarkerSymbolLayer.name()``: "circle", "square", "triangle", "star", ...
    #: The vocabulary has a radius and no shape, so anything else is drawn as a circle in
    #: the browser -- captured, and reported, because a square that becomes a circle is
    #: exactly the failure the analyst cannot see from inside their own QGIS.
    marker_shape: str = CIRCLE
    #: Qt brush style: ``"solid"``, ``"no"``, or the name of a hatch pattern.
    brush_style: str = "solid"
    #: Qt pen style: ``"solid"``, ``"no"``, ``"dash"``, ``"dot"``, ...
    stroke_style: str = "solid"
    #: ``customDashVector()``, and empty unless ``useCustomDashPattern()`` is actually on.
    #: A pen style of ``"dash"`` with no vector is Qt's built-in pattern, which has no
    #: numbers to read.
    dash_pattern: tuple[float, ...] = ()
    dash_pattern_unit: Unit | str = Unit.MILLIMETRES
    #: A disabled layer draws nothing, so dropping it loses nothing and is not reported.
    enabled: bool = True
    #: Names of properties carrying an active data-defined override.
    data_defined: tuple[str, ...] = ()


@dataclass(frozen=True)
class SymbolDescription:
    """A QGIS symbol: its layers, bottom-most first, and its own opacity."""

    layers: tuple[SymbolLayerDescription, ...] = ()
    #: ``QgsSymbol.opacity()``, 0-1.
    opacity: float = 1.0
    #: ``QgsSymbol.type()`` as a word, carried only so a refusal can name what could not
    #: be read ("this fill symbol is built from GradientFill").
    symbol_type: str = ""


@dataclass(frozen=True)
class RendererDescription:
    """A QGIS layer's renderer, reduced to what the style vocabulary can react to."""

    #: ``QgsFeatureRenderer.type()``, verbatim.
    renderer_type: str
    #: The single symbol, where there is one. ``None`` for every renderer that has more
    #: than one or none at all.
    symbol: SymbolDescription | None = None
    #: ``QgsVectorLayer.opacity()``, 0-1.
    layer_opacity: float = 1.0
    #: How many symbols an attribute-driven renderer has, and the attribute it splits on.
    #: Both are for the refusal's wording: "styled into 5 symbols" is the sentence that
    #: makes "these are five classes" land.
    category_count: int = 0
    category_field: str = ""
    #: Anything other than normal compositing, named as QGIS names it.
    blend_mode: str = ""
    scale_visibility: bool = False
    labels_enabled: bool = False


@dataclass(frozen=True)
class CaptureResult:
    """A captured style and everything that did not survive the capture.

    Both halves, always. A caller that can only learn "it worked" cannot show the analyst
    what changed, and the whole point of proposing a style rather than applying one is
    that a human gets to look at it first.
    """

    style: dict[str, Any] | None = None
    #: ``"fill"``, ``"line"`` or ``"marker"`` -- the same word :func:`.styling.symbol_kind`
    #: returns for a class's ``geom_type``, so a caller pairing this with a class can see
    #: whether the two agree without re-deriving it from which keys are present.
    kind: str = ""
    refusal: Refusal | None = None
    notes: tuple[Note, ...] = ()

    @property
    def captured(self) -> bool:
        return self.style is not None

    def of_kind(self, kind: NoteKind) -> tuple[Note, ...]:
        return tuple(note for note in self.notes if note.kind is kind)

    def summary(self) -> str:
        """A sentence for the publish report, stating the loss and not only the win."""
        if self.refusal is not None:
            return self.refusal.message
        if self.style is None:
            return "Nothing to capture."
        keys = ", ".join(sorted(self.style))
        if not self.notes:
            return f"Captured a {self.kind} style ({keys})."
        return f"Captured a {self.kind} style ({keys}). " + " ".join(
            note.detail for note in self.notes
        )


class _CaptureRefusedError(Exception):
    """Raised where a refusal is discovered mid-capture. Carries the one to return."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.message)
        self.refusal = refusal


def _unit_name(unit: Unit | str) -> str:
    """The spelling the caller used, for a refusal that has to quote it back."""
    return unit.value if isinstance(unit, Unit) else str(unit)


def _tidy(value: float, converted: bool) -> float | int:
    """Round a converted measurement; leave one that needed no conversion untouched.

    An integral width writes as ``2`` rather than ``2.0``, which is how the seeded blocks
    in 010_classes.sql are written and what a capture has to match to be recognised as
    proposing no change.

    Rounding is for the conversions, whose seventeen digits differ in the last bit between
    two captures of the same symbol. A width already in pixels is the number the analyst
    typed, and rounding it is not tidying: 0.004 becomes 0, and a zero-width stroke is the
    "viewer draws nothing at all" this module exists to prevent.
    """
    if converted:
        value = round(value, _DECIMALS)
    return int(value) if value == int(value) else value


def _refuse_unit(what: str, resolved: Unit, spelling: str) -> Refusal:
    """Name the specific reason this unit has no pixel value, not a generic one.

    "map units are a different number of pixels at every zoom" is the sentence that stops
    the analyst re-trying the same symbol; "unrecognised unit" would send them looking for
    a bug in the reader.
    """
    if resolved in (Unit.MAP_UNITS, Unit.METRES_AT_SCALE):
        reason = (
            f"The {what} is measured in map units ('{spelling}'), which is a different "
            "number of pixels at every zoom, and the style block's numbers are pixels."
        )
    elif resolved is Unit.PERCENTAGE:
        reason = (
            f"The {what} is a percentage ('{spelling}') of the symbol's own size, which "
            "is a different number of pixels every time the symbol is drawn."
        )
    else:
        reason = (
            f"The {what} is measured in '{spelling}', which is not a unit this reader "
            "knows a pixel value for."
        )
    return Refusal(
        reason=reason,
        remedy=(
            "Set the symbol's unit to pixels, millimetres, points or inches -- there is "
            "no honest conversion from this one -- or leave the class style as it is."
        ),
    )


def _to_pixels(value: float, unit: Unit | str, what: str) -> tuple[float, bool]:
    """Convert one symbol measurement to pixels, or refuse the whole capture.

    Reports whether a conversion actually happened, because a number that arrived in
    pixels is the analyst's own number and :func:`_tidy` must not touch it.
    """
    resolved = normalise_unit(unit)
    factor = _PIXELS_PER.get(resolved)
    if factor is None:
        raise _CaptureRefusedError(_refuse_unit(what, resolved, _unit_name(unit)))
    return value * factor, factor != 1.0


def _pixels(value: float, unit: Unit | str, what: str, converted: set[Unit]) -> float | int:
    """One measurement in pixels, recording the unit it came from if it was converted."""
    pixels, was_converted = _to_pixels(value, unit, what)
    if was_converted:
        converted.add(normalise_unit(unit))
    return _tidy(pixels, was_converted)


def is_parseable_color(value: Any) -> bool:
    """Whether :func:`.styling.parse_color` will read `value`, or fall back instead.

    ``parse_color`` is documented to fall back rather than raise, on purpose: one class
    with a typo in its colour should look wrong, not stop the registry loading. That is
    the right rule in the forward direction and the wrong one here, where the fallback is
    fully transparent black and a silent one would propose an invisible class as a
    success. Asking twice with two different fallbacks is the only way to tell from
    outside: an answer that depends on the fallback means the fallback is what came back.
    """
    return parse_color(value, "0,0,0,0") == parse_color(value, "255,255,255,255")


def css_color(value: Any, opacity: float = 1.0) -> str:
    """Render a colour as CSS ``#RRGGBBAA``, the byte order the vocabulary uses.

    `opacity` multiplies the alpha, which is how the layer's and the symbol's opacity get
    into the one channel available here.

    A fully opaque colour is written as six digits rather than eight. That is how the
    seeded styles are written, and a capture that proposes ``#4f9ddeff`` where the row
    already holds ``#4f9dde`` proposes a change that is not a change -- which
    ``018_class_registry.sql`` would faithfully record in ``class_history`` as one.
    """
    red, green, blue, alpha = parse_color(value)
    alpha = max(0, min(255, round(alpha * opacity)))
    if alpha >= 255:
        return f"#{red:02x}{green:02x}{blue:02x}"
    return f"#{red:02x}{green:02x}{blue:02x}{alpha:02x}"


def _colour(value: Any, opacity: float, what: str) -> str:
    """A captured colour, or a refusal naming the string that was not one."""
    if not is_parseable_color(value):
        raise _CaptureRefusedError(
            Refusal(
                reason=(
                    f"The {what} arrived as {value!r}, which is not a colour this reader "
                    "can read, and guessing at it would propose a class nobody can see."
                ),
                remedy=(
                    'Pass a QColor as "r,g,b,a" -- QColor.name(HexArgb) is #AARRGGBB and '
                    "is not it -- or a CSS #RGB, #RRGGBB or #RRGGBBAA string."
                ),
            )
        )
    return css_color(value, opacity)


def _first_simple_layer(
    layers: tuple[SymbolLayerDescription, ...],
) -> tuple[SymbolLayerDescription | None, list[SymbolLayerDescription]]:
    """The first layer this vocabulary can express, and the enabled ones it displaces."""
    drawn = [layer for layer in layers if layer.enabled]
    chosen: SymbolLayerDescription | None = None
    dropped: list[SymbolLayerDescription] = []
    for layer in drawn:
        if chosen is None and layer.type_name in SIMPLE_LAYER_KINDS:
            chosen = layer
        else:
            dropped.append(layer)
    return chosen, dropped


#: Refusal for a renderer that has no symbol to read at all -- a null renderer, or one the
#: QGIS-facing reader could not make sense of.
NO_SYMBOL = Refusal(
    reason="This layer's renderer offered no symbol to read.",
    remedy="Give the layer a single simple fill, line or marker symbol.",
)

#: Refusal for a symbol layer that named no colour, width or size. An empty style block is
#: not a capture: every class already has a style seeded in 010_classes.sql, so proposing
#: ``{}`` proposes deleting a deliberate choice, and a caller branching on ``captured``
#: would read that as a success.
NOTHING_TO_CAPTURE = Refusal(
    reason=(
        "This symbol layer carried no colour, width or size, so there is nothing to "
        "propose and an empty style block would propose erasing the one the class has."
    ),
    remedy="Give the symbol a fill or stroke colour, or leave the class style as it is.",
)


def _refuse_renderer(renderer: RendererDescription) -> Refusal | None:
    """The renderer-level refusals, which are about the registry's shape, not QGIS's."""
    if renderer.renderer_type in ATTRIBUTE_RENDERERS:
        by_what = (
            f"by the attribute '{renderer.category_field}'"
            if renderer.category_field
            else "by attribute value"
        )
        how_many = (
            f"{renderer.category_count} symbols" if renderer.category_count else "several symbols"
        )
        return Refusal(
            reason=(
                f"This layer is styled {by_what} into {how_many}, and a class holds one "
                "style. Styling by attribute value is what the registry expresses as "
                "class membership, so collapsing it to a single symbol would throw away "
                "the split the layer is already drawing."
            ),
            remedy=(
                "Publish it as separate classes -- the split it draws is the split the "
                "registry wants -- or set the layer to a single symbol first."
            ),
        )
    if renderer.renderer_type != SINGLE_SYMBOL:
        named = renderer.renderer_type or "unnamed"
        return Refusal(
            reason=(
                f"This layer uses the '{named}' renderer, which does not reduce to one "
                "fill and one stroke."
            ),
            remedy="Set the layer to a single symbol to capture a style from it.",
        )
    if renderer.symbol is None or not renderer.symbol.layers:
        return NO_SYMBOL
    return None


def _refuse_symbol_layers(symbol: SymbolDescription) -> Refusal:
    """Refuse a symbol whose every layer draws something with no equivalent here."""
    kinds = ", ".join(dict.fromkeys(layer.type_name for layer in symbol.layers if layer.enabled))
    described = f"this {symbol.symbol_type} symbol" if symbol.symbol_type else "this symbol"
    built = f"It is built from {kinds}." if kinds else "Every layer in it is turned off."
    return Refusal(
        reason=(
            f"Nothing in {described} is a simple fill, line or marker, and one fill, one "
            f"stroke and one radius is the whole vocabulary. {built}"
        ),
        remedy=(
            "Add a simple fill, line or marker layer to the symbol, or set the class "
            "style in the class console, where the vocabulary is not the constraint."
        ),
    )


def _ignored_notes(
    layer: SymbolLayerDescription, kind: str, renderer: RendererDescription, opacity: float
) -> list[Note]:
    """Everything about this capture that the analyst is entitled to be told."""
    notes: list[Note] = []
    if opacity < 1.0:
        notes.append(
            Note(
                NoteKind.ASSUMED,
                f"Layer opacity {renderer.layer_opacity:g} and symbol opacity "
                f"{(renderer.symbol.opacity if renderer.symbol else 1.0):g} were composed "
                "into the captured colours' alpha, which is the one place the web viewer "
                "has to put them -- so the captured colours are what you see on screen, "
                "not what the colour picker shows.",
            )
        )
    if kind == "marker" and layer.marker_shape and layer.marker_shape != CIRCLE:
        notes.append(
            Note(
                NoteKind.IGNORED,
                f"The '{layer.marker_shape}' marker shape was ignored: the vocabulary has "
                "a radius and no shape, so the web viewer will draw a circle where your "
                "QGIS draws this.",
            )
        )
    if layer.data_defined:
        notes.append(
            Note(
                NoteKind.IGNORED,
                f"Data-defined overrides on {', '.join(layer.data_defined)} were ignored: "
                "a class style is one fixed colour and width for every feature in the "
                "class.",
            )
        )
    if layer.brush_style not in ("solid", "no"):
        notes.append(
            Note(
                NoteKind.IGNORED,
                f"The '{layer.brush_style}' fill pattern was ignored; the viewer draws a "
                "solid fill in the captured colour.",
            )
        )
    if renderer.blend_mode and renderer.blend_mode.lower() != "normal":
        notes.append(
            Note(
                NoteKind.IGNORED,
                f"The '{renderer.blend_mode}' blend mode was ignored; the viewer "
                "composites normally.",
            )
        )
    if renderer.scale_visibility:
        notes.append(
            Note(
                NoteKind.IGNORED,
                "Scale-dependent visibility was ignored; a class style has no way to say "
                "at which zooms it applies.",
            )
        )
    if renderer.labels_enabled:
        notes.append(
            Note(
                NoteKind.IGNORED,
                "This layer's labels were ignored; the style block has no text vocabulary at all.",
            )
        )
    return notes


def _stroke_into(
    style: dict[str, Any], layer: SymbolLayerDescription, opacity: float, converted: set[Unit]
) -> None:
    """The stroke colour and width, which every kind of symbol layer has."""
    if layer.stroke_style == "no":
        # An explicit "no outline", not a missing one. Saying so beats omitting the key
        # and letting the forward path's default paint an outline nobody asked for.
        style["stroke"] = css_color("#00000000")
        return
    if layer.stroke_color is not None:
        style["stroke"] = _colour(layer.stroke_color, opacity, "stroke colour")
    if layer.stroke_width is not None:
        style["stroke_width"] = _pixels(
            layer.stroke_width, layer.stroke_width_unit, "stroke width", converted
        )


def _dash_into(
    style: dict[str, Any],
    layer: SymbolLayerDescription,
    kind: str,
    converted: set[Unit],
    notes: list[Note],
) -> None:
    """The dash array, which fills and lines have in this vocabulary and markers do not."""
    if kind == "marker":
        if layer.dash_pattern or layer.stroke_style not in ("solid", "no"):
            notes.append(
                Note(
                    NoteKind.IGNORED,
                    "The marker outline's dash pattern was ignored: a point class has a "
                    "fill, a stroke and a radius, and no way to say the stroke is broken.",
                )
            )
        return
    if layer.dash_pattern:
        style["dash"] = [
            _pixels(part, layer.dash_pattern_unit, "dash pattern", converted)
            for part in layer.dash_pattern
        ]
        return
    if layer.stroke_style not in ("solid", "no"):
        notes.append(
            Note(
                NoteKind.IGNORED,
                f"The '{layer.stroke_style}' stroke style is one of Qt's built-in "
                "patterns and has no pixel lengths to read, so the viewer will draw this "
                "stroke solid. Turn on a custom dash pattern to carry it across.",
            )
        )


def capture_style(renderer: RendererDescription) -> CaptureResult:
    """Capture one QGIS renderer as a ``label_class.style`` block, or refuse it.

    The result is a proposal. Nothing here decides whether it should be applied: a class
    style is vocabulary, changing it changes what every existing label of that class
    MEANS, and ``018_class_registry.sql`` requires an attributed ``admin`` write for
    exactly that reason. This function's job is to be right about the numbers and honest
    about the losses.
    """
    refusal = _refuse_renderer(renderer)
    symbol = renderer.symbol
    if refusal is not None or symbol is None:
        return CaptureResult(refusal=refusal or NO_SYMBOL)

    chosen, dropped = _first_simple_layer(symbol.layers)
    if chosen is None:
        return CaptureResult(refusal=_refuse_symbol_layers(symbol))

    kind = SIMPLE_LAYER_KINDS[chosen.type_name]
    opacity = max(0.0, min(1.0, renderer.layer_opacity)) * max(0.0, min(1.0, symbol.opacity))
    notes = _ignored_notes(chosen, kind, renderer, opacity)
    if dropped:
        kinds = ", ".join(dict.fromkeys(layer.type_name for layer in dropped))
        notes.append(
            Note(
                NoteKind.DROPPED,
                f"This symbol stacks {len(dropped) + 1} drawn layers and the vocabulary "
                f"has one fill and one stroke, so {chosen.type_name} was captured and "
                f"{kinds} dropped.",
            )
        )

    style: dict[str, Any] = {}
    converted: set[Unit] = set()
    try:
        if kind in ("fill", "marker"):
            # A brush of "no" is how a polygon class says "outline only", and how the
            # forward path renders a fully transparent fill. Reading it as anything else
            # would make every click on a 3428 km national layer select a compound.
            #
            # The colour is kept and its alpha zeroed rather than replaced with black: an
            # analyst who set a transparent version of their class colour gets that
            # colour back, and a re-import of an unchanged symbol proposes nothing, where
            # collapsing it to #00000000 would write a class_history row for a change
            # that renders identically.
            if chosen.brush_style == "no":
                style["fill"] = (
                    _colour(chosen.fill_color, 0.0, "fill colour")
                    if chosen.fill_color is not None
                    else css_color("#00000000")
                )
            elif chosen.fill_color is not None:
                style["fill"] = _colour(chosen.fill_color, opacity, "fill colour")
        _stroke_into(style, chosen, opacity, converted)
        if kind == "marker" and chosen.size is not None:
            # size -> radius. See the module docstring; this halving is the one that stays
            # invisible until somebody opens the campus with 872 of them on it.
            diameter, was_converted = _to_pixels(chosen.size, chosen.size_unit, "marker size")
            if was_converted:
                converted.add(normalise_unit(chosen.size_unit))
            style["radius"] = _tidy(diameter / 2, was_converted)
        _dash_into(style, chosen, kind, converted, notes)
    except _CaptureRefusedError as refused:
        return CaptureResult(refusal=refused.refusal)

    if not style:
        return CaptureResult(refusal=NOTHING_TO_CAPTURE)

    if converted:
        names = ", ".join(sorted(unit.value for unit in converted))
        notes.append(
            Note(
                NoteKind.ASSUMED,
                f"Widths and sizes in {names} were converted to pixels at "
                f"{REFERENCE_DPI:g} dpi, so the numbers in the style block are not the "
                "numbers in the symbol dialog.",
            )
        )
    return CaptureResult(style=style, kind=kind, notes=tuple(notes))
