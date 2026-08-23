"""The class registry: the plugin's single source of truth for label vocabulary.

WHY THE PLUGIN NEVER CONTAINS A LIST OF CLASSES OR ATTRIBUTES

The analyst's shapefiles had seven layers whose attributes were 0% populated, with the
same concept spelled two different ways in two layers because DBF truncates field names
at ten characters. The platform's answer was to stop treating attributes as columns:
``label_class`` holds a JSON Schema per class and ``label.attrs`` holds JSONB, so adding
``rack_count`` to datacenter buildings is one ``UPDATE`` and no migration.

That only holds if every client reads the registry instead of shipping its own copy. A
hardcoded list here would reintroduce exactly the drift the design removes -- the web UI
would show the new field on Monday and QGIS would show it whenever the plugin was next
released. So: no class names, no attribute names, no enum values in this file. Every one
of them arrives at runtime.

ACCEPTED SHAPES

The backend is expected to serve ``{"classes": [...], "fields": {...}}``. Two other
shapes are accepted without complaint, because both are plausible ways a deployment
might expose the same table and neither is worth a support ticket:

* a bare JSON array of class objects;
* an OAPIF/GeoJSON ``FeatureCollection`` whose features carry the class rows in
  ``properties``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .errors import RegistryError
from .fields import DEFAULT_FIELDS, CoreFields

#: Geometry type strings used by ``label_class.geom_type``.
_ANY_GEOMETRY = "Any"


@dataclass(frozen=True)
class AttributeSpec:
    """One attribute as the class's JSON Schema describes it.

    A thin read-only view over the schema fragment. It exists so the form builder can
    ask questions ("is this an enum?") without every call site learning JSON Schema, and
    it keeps the raw fragment around because the schema subset the server understands
    may grow past what this plugin renders.
    """

    name: str
    schema: Mapping[str, Any] = field(default_factory=dict)

    @property
    def type(self) -> str | None:
        value = self.schema.get("type")
        return value if isinstance(value, str) else None

    @property
    def description(self) -> str | None:
        value = self.schema.get("description")
        return value if isinstance(value, str) else None

    @property
    def enum(self) -> tuple[Any, ...] | None:
        value = self.schema.get("enum")
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return tuple(value)
        return None

    @property
    def minimum(self) -> float | None:
        value = self.schema.get("minimum")
        return (
            float(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None
        )

    @property
    def maximum(self) -> float | None:
        value = self.schema.get("maximum")
        return (
            float(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None
        )

    def summary(self) -> str:
        """One line describing the attribute, for form help text."""
        bits: list[str] = [self.name]
        if self.type:
            bits.append(f"({self.type})")
        if self.enum:
            bits.append("one of " + ", ".join(str(v) for v in self.enum))
        elif self.minimum is not None or self.maximum is not None:
            lo = "" if self.minimum is None else f"{self.minimum:g}"
            hi = "" if self.maximum is None else f"{self.maximum:g}"
            bits.append(f"range {lo}..{hi}")
        if self.description:
            bits.append("- " + self.description)
        return " ".join(bits)


@dataclass(frozen=True)
class LabelClass:
    """One row of ``label_class``."""

    class_id: str
    geom_type: str
    label_en: str
    label_zh: str | None = None
    description: str | None = None
    attr_schema: Mapping[str, Any] = field(default_factory=dict)
    form: Mapping[str, Any] = field(default_factory=dict)
    style: Mapping[str, Any] = field(default_factory=dict)
    sort_order: int = 100
    active: bool = True

    @property
    def display_name(self) -> str:
        """Label for menus and the categorized renderer.

        Both languages when both exist. The Chinese name is not decoration: the source
        data is Chinese infrastructure and 82.6% of compounds have only a Chinese name.
        """
        if self.label_zh and self.label_zh != self.label_en:
            return f"{self.label_en} ({self.label_zh})"
        return self.label_en

    @property
    def accepts_any_geometry(self) -> bool:
        return self.geom_type == _ANY_GEOMETRY

    def attribute_names(self) -> list[str]:
        """Declared attributes, in the order the class wants them shown.

        ``form.order`` wins where it is present -- it is the shared presentation hint
        that keeps QGIS and the web viewer from drifting -- and anything declared in the
        schema but missing from ``order`` is appended alphabetically rather than
        dropped, so a newly added attribute shows up before anyone edits ``form``.
        """
        properties = self.attr_schema.get("properties")
        declared = list(properties) if isinstance(properties, Mapping) else []
        order = self.form.get("order")
        ordered: list[str] = []
        if isinstance(order, Sequence) and not isinstance(order, (str, bytes)):
            ordered = [str(name) for name in order if str(name) in declared]
        remainder = sorted(name for name in declared if name not in ordered)
        return ordered + remainder

    def attribute(self, name: str) -> AttributeSpec:
        """Schema fragment for one attribute (empty if the class does not declare it)."""
        properties = self.attr_schema.get("properties")
        fragment = properties.get(name) if isinstance(properties, Mapping) else None
        return AttributeSpec(name=name, schema=fragment if isinstance(fragment, Mapping) else {})

    def attributes(self) -> list[AttributeSpec]:
        return [self.attribute(name) for name in self.attribute_names()]

    @property
    def open_vocabulary(self) -> bool:
        """True when the class accepts attributes it has not declared.

        The seed data sets ``additionalProperties: true`` everywhere on purpose --
        capture first, formalise later -- so the form help has to say so rather than
        implying the declared list is exhaustive.
        """
        return self.attr_schema.get("additionalProperties", True) is not False

    def widget_hint(self, name: str) -> str | None:
        """Widget the class asks for, if any (``form.widgets``)."""
        widgets = self.form.get("widgets")
        if isinstance(widgets, Mapping):
            hint = widgets.get(name)
            if isinstance(hint, str):
                return hint
        return None

    def help_text(self) -> str:
        """Human-readable summary of the class's attribute vocabulary.

        Shown next to the raw ``attrs`` field. QGIS renders a JSONB column as one value;
        rather than build a JSON editor -- a subsystem, not a thin client -- the plugin
        puts the class's own schema in front of the person typing.
        """
        lines = [f"{self.display_name} - attributes declared by the class registry:"]
        specs = self.attributes()
        if specs:
            lines += ["  " + spec.summary() for spec in specs]
        else:
            lines.append("  (none declared yet)")
        if self.open_vocabulary:
            lines.append("Additional attributes are accepted; the server validates on write.")
        else:
            lines.append("This class rejects undeclared attributes.")
        return "\n".join(lines)


@dataclass(frozen=True)
class ClassRegistry:
    """The whole vocabulary, plus any server overrides for core field names."""

    classes: tuple[LabelClass, ...] = ()
    fields: CoreFields = DEFAULT_FIELDS
    source_url: str | None = None

    def __len__(self) -> int:
        return len(self.classes)

    def __iter__(self):
        return iter(self.classes)

    def get(self, class_id: str) -> LabelClass | None:
        for cls in self.classes:
            if cls.class_id == class_id:
                return cls
        return None

    def active(self) -> tuple[LabelClass, ...]:
        """Classes that still accept new labels.

        Retired classes stay in the registry because historical labels reference them --
        dropping them from the renderer would make old features invisible rather than
        merely uneditable.
        """
        return tuple(cls for cls in self.classes if cls.active)

    def value_map(self) -> list[tuple[str, str]]:
        """``(display, stored)`` pairs for a ``class_id`` picker, registry-ordered."""
        return [(cls.display_name, cls.class_id) for cls in self.active()]


def _coerce_class(raw: Mapping[str, Any]) -> LabelClass:
    class_id = raw.get("class_id") or raw.get("id")
    if not isinstance(class_id, str) or not class_id:
        raise RegistryError(f"class entry has no class_id: {raw!r}")
    geom_type = raw.get("geom_type") or raw.get("geometry_type") or _ANY_GEOMETRY
    label_en = raw.get("label_en") or raw.get("label") or class_id
    sort_order = raw.get("sort_order", 100)
    return LabelClass(
        class_id=class_id,
        geom_type=str(geom_type),
        label_en=str(label_en),
        label_zh=raw.get("label_zh") or None,
        description=raw.get("description") or None,
        attr_schema=_as_mapping(raw.get("attr_schema")),
        form=_as_mapping(raw.get("form")),
        style=_as_mapping(raw.get("style")),
        sort_order=int(sort_order) if isinstance(sort_order, (int, float)) else 100,
        # Absent means active: a registry that omits the flag is describing live classes.
        active=bool(raw.get("active", True)),
    )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _extract_entries(document: Any) -> tuple[Iterable[Any], Mapping[str, Any]]:
    """Pull the class list and any ``fields`` block out of the accepted shapes."""
    if isinstance(document, list):
        return document, {}
    if not isinstance(document, Mapping):
        raise RegistryError(
            f"Class registry must be a JSON object or array, got {type(document).__name__}."
        )
    if isinstance(document.get("classes"), list):
        return document["classes"], _as_mapping(document.get("fields"))
    if isinstance(document.get("features"), list):
        # GeoJSON / OAPIF shape: the row is in `properties`.
        return (
            [
                feature.get("properties", {})
                for feature in document["features"]
                if isinstance(feature, Mapping)
            ],
            _as_mapping(document.get("fields")),
        )
    raise RegistryError(
        "Class registry has no 'classes' array. Expected {'classes': [...]}, a bare "
        "array, or a GeoJSON FeatureCollection."
    )


def parse_registry(document: Any, source_url: str | None = None) -> ClassRegistry:
    """Parse a class-registry document into a :class:`ClassRegistry`.

    Ordering follows ``sort_order`` then ``class_id`` so the panel, the renderer legend
    and the web UI agree, which is the entire point of the field existing.
    """
    entries, field_overrides = _extract_entries(document)
    classes = [_coerce_class(entry) for entry in entries if isinstance(entry, Mapping)]
    if not classes:
        raise RegistryError("Class registry is empty; the backend has no label classes.")
    classes.sort(key=lambda cls: (cls.sort_order, cls.class_id))
    return ClassRegistry(
        classes=tuple(classes),
        fields=DEFAULT_FIELDS.merged(field_overrides),
        source_url=source_url,
    )
