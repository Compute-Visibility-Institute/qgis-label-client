"""Translating a legacy shapefile's vocabulary onto the class registry's.

THE PROBLEM, EXACTLY

DBF truncates field names at ten characters, and the analyst abbreviated by hand on top
of that. The result is four columns across three layers that are two concepts:

    "No. Cooler"  (Compounds)         same concept, two truncations
    "No. Coolim"  (Bld_Datacenters)

    "No. transf"  (Compounds)         same concept, differing only in case
    "No. Transf"  (Substation)

Field names also carry spaces, periods and a colon (``Name:ch``), none of which survive a
trip through SQL or JSON unscathed.

WHY THE TARGET NAMES ARE NOT WRITTEN DOWN HERE

They are in the class registry, which the plugin fetches at runtime. A lookup table in
this file mapping ``"No. Cooler"`` to its canonical spelling would be a second copy of the
vocabulary, and the entire point of ``label_class.attr_schema`` is that there is one copy:
adding an attribute is an ``UPDATE`` on the server and needs no plugin release. A table
here would silently cancel that, and the repository's hygiene test enforces the rule.

So the matching is structural rather than tabular. Both sides are reduced to a *concept*
-- a set of four-character token stems plus a flag for "this name counts something" -- and
a source column is mapped onto whichever declared attribute it is a subset of:

    "No. Cooler"          -> stems {cool},        counts
    "No. Coolim"          -> stems {cool},        counts
    <the canonical name>  -> stems {cool, unit},  counts   -- both are subsets of it

Four characters is the stem length because that is what survives the abbreviations
actually present: ``Cooler``, ``Coolim`` and the canonical spelling all reduce to
``cool``; ``transf`` and its canonical spelling both reduce to ``tran``. The quantity flag
is what stops a column describing a *kind* of thing from landing on a column counting
them.

AMBIGUITY IS NEVER RESOLVED BY GUESSING

Two candidates scoring equally produce no mapping and a report of the tie, in both the
class guess and the field mapping. This is a one-way bootstrap into an empty system; a
silent wrong guess is far more expensive than a question in a dialog.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .names import AUTO, CHINESE, ENGLISH
from .registry import AttributeSpec, ClassRegistry, LabelClass

#: Characters of a token kept when reducing it to a stem. See the module docstring for
#: why four: it is the longest prefix shared by every abbreviation in the source data.
STEM_LENGTH = 4

#: Tokens that mark a name as counting something rather than describing it. Generic
#: English and notation, not domain vocabulary -- nothing here names a class or an
#: attribute, which is the property the hygiene test protects.
_QUANTITY_TOKENS = frozenset(
    {
        "no",
        "nos",
        "num",
        "nums",
        "number",
        "numbers",
        "count",
        "counts",
        "qty",
        "quantity",
        "total",
        "amount",
    }
)

#: Tokens that mark which language a name column holds.
_LANGUAGE_TOKENS = {
    "ch": CHINESE,
    "cn": CHINESE,
    "zh": CHINESE,
    "zho": CHINESE,
    "hans": CHINESE,
    "chinese": CHINESE,
    "en": ENGLISH,
    "eng": ENGLISH,
    "english": ENGLISH,
}

#: The single stem that identifies a column as holding a name rather than an attribute.
_NAME_STEM = "name"

# Insert a separator at camelCase and ACRONYMWord boundaries, so "CoolingUnits" and
# "Bld_Datacenters" split the same way as "cooling_unit_count" does.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")


def tokenise(name: str) -> tuple[str, ...]:
    """Split an identifier into lowercase word tokens.

    ``"No. Cooler"``, ``"Bld_Datacenters"``, ``"CoolingUnits"`` and ``"Name:ch"`` all come
    from the same person and the same decade and none of them share a convention, so the
    splitter has to handle punctuation, underscores and camel case at once.
    """
    spaced = _CAMEL_BOUNDARY.sub(" ", name or "")
    return tuple(token.lower() for token in _SEPARATORS.split(spaced) if token)


def stem(token: str) -> str:
    """Reduce one token to the prefix that survives the source's abbreviations."""
    return token[:STEM_LENGTH]


@dataclass(frozen=True)
class Concept:
    """What a column or class name is *about*, reduced so two spellings can be compared."""

    #: Stems of the content-bearing tokens, quantity markers removed.
    stems: frozenset[str] = frozenset()
    #: True when the name counts something ("No.", "count").
    counts: bool = False
    #: Language this name column declares, if it declares one.
    language: str | None = None

    def __bool__(self) -> bool:
        return bool(self.stems)


def concept_of(name: str) -> Concept:
    """Reduce an identifier to a comparable :class:`Concept`."""
    tokens = tokenise(name)
    counts = any(token in _QUANTITY_TOKENS for token in tokens)
    language: str | None = None
    content: list[str] = []
    for token in tokens:
        if token in _QUANTITY_TOKENS:
            continue
        if token in _LANGUAGE_TOKENS:
            # Recorded, then dropped: "Name:ch" and "Name_en" are the same concept in two
            # languages, and leaving the marker in would stop them matching each other.
            language = _LANGUAGE_TOKENS[token]
            continue
        content.append(stem(token))
    return Concept(stems=frozenset(content), counts=counts, language=language)


# ---------------------------------------------------------------------------
# Which class is this layer?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassGuess:
    """A heuristic answer to "which registry class is this layer?", and its confidence."""

    class_id: str | None = None
    score: int = 0
    #: Class ids that scored identically. Non-empty means the guess was withheld.
    tied_with: tuple[str, ...] = ()

    @property
    def confident(self) -> bool:
        return self.class_id is not None

    def describe(self) -> str:
        if self.tied_with:
            return "Ambiguous: matches " + ", ".join(self.tied_with) + " equally well. Choose one."
        if not self.class_id:
            return "No class name resembles this layer. Choose one."
        return f"Guessed from the layer name (matched {self.score} word(s))."


def _class_stems(label_class: LabelClass) -> frozenset[str]:
    """Everything the registry says this class is called, reduced to stems."""
    return concept_of(label_class.class_id).stems | concept_of(label_class.label_en).stems


def guess_class(layer_name: str, registry: ClassRegistry) -> ClassGuess:
    """Guess which class a layer holds, from its name and the live registry.

    Only *active* classes are candidates: the database refuses new labels on a retired
    class, so offering one would produce a guess that cannot be published.

    A tie is reported rather than broken. ``mapLayers()`` order and registry order are
    both arbitrary from the user's point of view, so breaking a tie by either would make
    the guess depend on something nobody can see.
    """
    layer = concept_of(layer_name)
    scored: list[tuple[tuple[int, int], str]] = []
    for label_class in registry.active():
        candidate = _class_stems(label_class)
        overlap = layer.stems & candidate
        # A Chinese layer name cannot tokenise into ASCII stems at all, so match the
        # registry's own Chinese label as a substring instead of losing the signal.
        zh_hit = bool(label_class.label_zh and label_class.label_zh in (layer_name or ""))
        score = len(overlap) + (1 if zh_hit else 0)
        if not score:
            continue
        # Tie-break on specificity: between two classes matching one word each, the one
        # with fewer unmatched words of its own is the closer description.
        scored.append(((score, -len(candidate - overlap)), label_class.class_id))

    if not scored:
        return ClassGuess()
    scored.sort(key=lambda entry: entry[0], reverse=True)
    best = scored[0][0]
    winners = [class_id for rank, class_id in scored if rank == best]
    if len(winners) > 1:
        return ClassGuess(score=best[0], tied_with=tuple(sorted(winners)))
    return ClassGuess(class_id=winners[0], score=best[0])


# ---------------------------------------------------------------------------
# Where does this column go?
# ---------------------------------------------------------------------------


class FieldRole(str, Enum):
    """What a source column becomes on the platform side."""

    #: Into ``label.names``, under the language key in :attr:`FieldMapping.target`.
    NAME = "name"
    #: Into ``label.attrs``, under the attribute key in :attr:`FieldMapping.target`.
    ATTRIBUTE = "attribute"
    #: Nothing in the class registry resembles it. Reported, never published.
    UNMAPPED = "unmapped"


@dataclass(frozen=True)
class FieldMapping:
    """One source column and where its values go."""

    source: str
    role: FieldRole = FieldRole.UNMAPPED
    #: Attribute key for :attr:`FieldRole.ATTRIBUTE`, language key for
    #: :attr:`FieldRole.NAME`, ``None`` when unmapped.
    target: str | None = None
    #: Declared attributes that matched equally well. Non-empty means the mapping was
    #: withheld rather than guessed.
    tied_with: tuple[str, ...] = ()

    def describe(self) -> str:
        if self.tied_with:
            return f"{self.source}: ambiguous between " + ", ".join(self.tied_with)
        if self.role is FieldRole.UNMAPPED:
            return f"{self.source}: nothing in the class schema resembles it"
        return f"{self.source} -> {self.role.value} {self.target}"


def _normalised(name: str) -> str:
    """A form in which ``No. transf`` and ``No. Transf`` are the same string."""
    return "_".join(tokenise(name))


def _declared(label_class: LabelClass) -> tuple[str, ...]:
    return tuple(label_class.attribute_names())


def _attribute_match(source: str, label_class: LabelClass) -> FieldMapping | None:
    """Match one column against the attributes the class actually declares."""
    declared = _declared(label_class)
    if not declared:
        return None

    # Exact first, punctuation and case ignored. A layer already using the canonical
    # spelling must never be routed through the fuzzy path.
    wanted = _normalised(source)
    for name in declared:
        if _normalised(name) == wanted:
            return FieldMapping(source=source, role=FieldRole.ATTRIBUTE, target=name)

    concept = concept_of(source)
    if not concept:
        return None

    scored: list[tuple[tuple[int, int], str]] = []
    for name in declared:
        target = concept_of(name)
        if not target or concept.counts != target.counts:
            continue
        if not concept.stems <= target.stems:
            # One-way subset: an abbreviation is allowed to say less than the canonical
            # name, never more. "Cooler Location" must not land on a count of coolers.
            continue
        scored.append(((len(concept.stems), -len(target.stems - concept.stems)), name))

    if not scored:
        return None
    scored.sort(key=lambda entry: entry[0], reverse=True)
    best = scored[0][0]
    winners = [name for rank, name in scored if rank == best]
    if len(winners) > 1:
        return FieldMapping(source=source, tied_with=tuple(sorted(winners)))
    return FieldMapping(source=source, role=FieldRole.ATTRIBUTE, target=winners[0])


def _name_match(source: str) -> FieldMapping | None:
    """Match one column against ``label.names``.

    The test is strict: the column's content, once any language marker is removed, must be
    *exactly* the name stem. ``Name``, ``Name:ch`` and ``Name_en`` qualify; a column called
    ``<something>_name`` does not, because it names an attribute of the feature rather than
    the feature itself.
    """
    concept = concept_of(source)
    if concept.stems != {_NAME_STEM}:
        return None
    return FieldMapping(source=source, role=FieldRole.NAME, target=concept.language or AUTO)


def map_field(source: str, label_class: LabelClass) -> FieldMapping:
    """Decide where one source column's values go.

    The class registry is consulted before the name heuristic, so a class that declares an
    attribute called ``name`` gets it as an attribute. The registry is authoritative about
    its own vocabulary; the heuristic only fills the space it leaves.
    """
    declared = {_normalised(name) for name in _declared(label_class)}
    if _normalised(source) not in declared:
        as_name = _name_match(source)
        if as_name is not None:
            return as_name
    return _attribute_match(source, label_class) or FieldMapping(source=source)


def map_fields(sources: Iterable[str], label_class: LabelClass) -> tuple[FieldMapping, ...]:
    """Map every column of one layer."""
    return tuple(map_field(source, label_class) for source in sources)


def name_columns(sources: Iterable[str]) -> tuple[str, ...]:
    """Columns holding a name, decided without reference to any class.

    Needed before a class has been chosen: the damaged-name scan runs in the preview, and
    whether a column is called ``Name:ch`` does not depend on what the features are. A
    class that later claims one of these as an attribute only means the scan counted
    something that will not be published as a name -- a harmless over-report, where the
    reverse would be a silent under-report of destroyed data.
    """
    return tuple(source for source in sources if _name_match(source) is not None)


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def is_blank(value: Any) -> bool:
    """True for a value that records nothing.

    ``None``, the empty string and whitespace are all "nobody filled this in", and the key
    is omitted rather than written. Writing ``{"<count>": null}`` would claim we looked and
    found nothing; omitting the key says nobody recorded it. In a dataset where only four
    columns have any data at all, that distinction is most of the content.

    Zero is *not* blank. A recorded count of zero is a fact.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (Sequence, Mapping)) and not isinstance(value, (str, bytes)):
        return len(value) == 0
    return False


def coerce(value: Any, spec: AttributeSpec) -> tuple[Any, str | None]:
    """Coerce one value to the JSON type the class registry declares for it.

    Returns ``(value, None)`` or ``(None, reason)``. A DBF column is typed by the file, not
    by the schema, so an integer attribute routinely arrives as ``3.0`` or ``"3"``.

    An undeclared attribute is passed through untouched: every seeded class sets
    ``additionalProperties: true`` -- capture first, formalise later -- and refusing
    unforeseen attributes is exactly the friction that left the source data 0% populated.
    """
    want = spec.type
    if want is None:
        return value, None
    try:
        if want == "integer":
            as_float = float(value)
            if as_float != int(as_float):
                return None, f"{value!r} is not a whole number"
            return int(as_float), None
        if want == "number":
            return float(value), None
        if want == "string":
            return str(value).strip(), None
        if want == "boolean":
            return bool(value), None
    except (TypeError, ValueError):
        return None, f"{value!r} cannot be read as {want}"
    return value, None


def schema_problem(value: Any, spec: AttributeSpec) -> str | None:
    """Check the value keywords the server's schema validator will check.

    The server stays the single source of truth; this is not a second one. But it *raises*,
    and a rejected feature in the middle of a 1,246-feature bootstrap is a failure the user
    has to attribute to a row by hand. Catching the value-level keywords here turns that
    into one named warning against one named feature before anything is sent.

    ``required`` is deliberately not checked: a missing attribute is a fact about the source
    data, and dropping a whole feature over one would be worse than letting the server say
    so.
    """
    enum = spec.enum
    if enum is not None and value not in enum:
        return f"{value!r} is not one of {', '.join(str(item) for item in enum)}"

    # bool is a subclass of int, and a boolean is never what minimum/maximum mean.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if spec.minimum is not None and value < spec.minimum:
            return f"{value!r} is below the declared minimum {spec.minimum:g}"
        if spec.maximum is not None and value > spec.maximum:
            return f"{value!r} is above the declared maximum {spec.maximum:g}"
    return None


@dataclass(frozen=True)
class AttributeResult:
    """The ``attrs`` object for one feature, and everything that did not make it in."""

    attrs: Mapping[str, Any]
    issues: tuple[str, ...] = ()


def build_attrs(
    values: Mapping[str, Any],
    mappings: Iterable[FieldMapping],
    label_class: LabelClass,
) -> AttributeResult:
    """Build ``label.attrs`` for one feature.

    Blank values are omitted before anything else happens, which is also why an unmapped
    column that is empty in every row -- ``id``, and most of this dataset -- never produces
    a warning. There is nothing to warn about.
    """
    attrs: dict[str, Any] = {}
    issues: list[str] = []

    for mapping in mappings:
        if mapping.role is FieldRole.NAME:
            continue
        raw = values.get(mapping.source)
        if is_blank(raw):
            continue
        if mapping.role is not FieldRole.ATTRIBUTE or not mapping.target:
            issues.append(f"{mapping.describe()}; its value {raw!r} was not published")
            continue

        spec = label_class.attribute(mapping.target)
        coerced, problem = coerce(raw, spec)
        if problem:
            issues.append(f"{mapping.target}: {problem}")
            continue
        problem = schema_problem(coerced, spec)
        if problem:
            issues.append(f"{mapping.target}: {problem}")
            continue
        if is_blank(coerced):
            # Coercion can empty a value: a string field holding only spaces becomes "".
            continue
        attrs[mapping.target] = coerced

    return AttributeResult(attrs=attrs, issues=tuple(issues))


def name_entries(
    values: Mapping[str, Any],
    mappings: Iterable[FieldMapping],
) -> tuple[tuple[str, Any], ...]:
    """``(language, raw value)`` pairs for :func:`.names.build_names`."""
    return tuple(
        (mapping.target or AUTO, values.get(mapping.source))
        for mapping in mappings
        if mapping.role is FieldRole.NAME
    )
