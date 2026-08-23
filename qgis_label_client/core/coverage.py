"""Coverage QA: which labels sit outside any declared exhaustive survey extent.

WHY THIS IS A QA BUTTON AND NOT A VALIDATION ERROR

In the source data 872 cooling units are labeled on a single 1.0 x 0.8 km campus, three
of 190 compounds. The other 187 have cooling units on the ground and none in the data.
That is sensible human triage and a catastrophe for a detector: every unlabeled cooling
unit at those sites becomes *supervised background*, and the model is taught that cooling
units are not cooling units.

``labeled_extent`` records where someone actually swept, per class, per date. Chip
sampling draws only from inside ``completeness = 'exhaustive'`` extents; everything else
is **unknown, never negative**. This module answers the question that makes that usable
day to day -- "have I drawn labels on ground I never declared as swept?" -- so the answer
arrives while the analyst still remembers, rather than a year later when it cannot be
reconstructed at all.

A label outside every extent is **not an error**. It is a label on unsurveyed ground,
which is a normal and correct thing to have. The finding is that the *extent* is missing,
not that the label is wrong, and the wording throughout says so.

Geometry is not done here. The caller supplies an intersection predicate, which keeps the
class/completeness reasoning -- the part that is easy to get subtly wrong -- testable
without QGIS.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .fields import (
    COMPLETENESS_EXHAUSTIVE,
    COMPLETENESS_PARTIAL,
    DEFAULT_FIELDS,
    CoreFields,
)


class Coverage(str, Enum):
    """How a single label relates to the declared survey extents for its class."""

    #: Inside an exhaustive extent. Safe for the export pipeline to treat surrounding
    #: unlabeled ground as negative.
    EXHAUSTIVE = "exhaustive"
    #: Inside only a partial extent. The sweep was qualified (see its caveat), so the
    #: surrounding ground is still unknown.
    PARTIAL = "partial"
    #: Outside every extent for this class. Unknown ground -- not a mistake.
    UNSURVEYED = "unsurveyed"


@dataclass(frozen=True)
class ExtentRef:
    """The subset of a ``labeled_extent`` feature this module reasons about."""

    extent_id: str
    class_id: str
    completeness: str
    caveat: str | None = None

    @property
    def is_exhaustive(self) -> bool:
        return self.completeness == COMPLETENESS_EXHAUSTIVE

    @property
    def is_partial(self) -> bool:
        return self.completeness == COMPLETENESS_PARTIAL


@dataclass(frozen=True)
class LabelRef:
    """The subset of a label feature this module reasons about."""

    feature_id: object
    label_id: str | None
    class_id: str


@dataclass(frozen=True)
class Finding:
    """One label and its coverage verdict."""

    label: LabelRef
    coverage: Coverage
    #: Extents the label intersects, whatever their completeness.
    extent_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoverageReport:
    """Everything the QA panel needs to say something true and specific."""

    findings: tuple[Finding, ...]
    #: Classes with no exhaustive extent declared anywhere at all. Distinct from a label
    #: merely falling outside one, and a much stronger signal: nobody has ever recorded a
    #: sweep for this class.
    classes_without_extents: tuple[str, ...] = ()

    def by_coverage(self, coverage: Coverage) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.coverage is coverage)

    @property
    def unsurveyed(self) -> tuple[Finding, ...]:
        return self.by_coverage(Coverage.UNSURVEYED)

    @property
    def partial_only(self) -> tuple[Finding, ...]:
        return self.by_coverage(Coverage.PARTIAL)

    @property
    def clean(self) -> bool:
        return not self.unsurveyed and not self.partial_only and not self.classes_without_extents

    def summary(self) -> str:
        """A sentence that states the consequence, not just the count."""
        if not self.findings and not self.classes_without_extents:
            return "No labels checked."
        if self.clean:
            return (
                f"All {len(self.findings)} labels fall inside an exhaustive survey extent "
                "for their class."
            )
        bits: list[str] = []
        if self.unsurveyed:
            bits.append(
                f"{len(self.unsurveyed)} on ground never declared as surveyed for their class"
            )
        if self.partial_only:
            bits.append(f"{len(self.partial_only)} inside only a partial extent (see its caveat)")
        if self.classes_without_extents:
            bits.append(
                "no exhaustive extent exists at all for: " + ", ".join(self.classes_without_extents)
            )
        return (
            f"{len(self.findings)} labels checked - "
            + "; ".join(bits)
            + ". That ground is UNKNOWN to the export pipeline, not negative; declare a "
            "labeled_extent to make it usable as training data."
        )


def applicable_extents(class_id: str, extents: Iterable[ExtentRef]) -> list[ExtentRef]:
    """Extents that can say anything about labels of `class_id`.

    Coverage is per class. An exhaustive sweep for ``compound`` says nothing whatsoever
    about ``cooling_unit`` on the same ground -- that conflation is the original defect
    restated -- so the class filter comes before any geometry test.
    """
    return [extent for extent in extents if extent.class_id == class_id]


def classify(
    label: LabelRef,
    extents: Sequence[ExtentRef],
    intersects: Callable[[LabelRef, ExtentRef], bool],
) -> Finding:
    """Classify one label against the extents declared for its class."""
    candidates = applicable_extents(label.class_id, extents)
    hits = [extent for extent in candidates if intersects(label, extent)]
    if any(extent.is_exhaustive for extent in hits):
        coverage = Coverage.EXHAUSTIVE
    elif any(extent.is_partial for extent in hits):
        coverage = Coverage.PARTIAL
    else:
        # Includes hits on 'unknown' extents, which 007_labeled_extent.sql defines as a
        # backfilled guess to be treated as unsurveyed.
        coverage = Coverage.UNSURVEYED
    return Finding(label=label, coverage=coverage, extent_ids=tuple(e.extent_id for e in hits))


def build_report(
    labels: Iterable[LabelRef],
    extents: Iterable[ExtentRef],
    intersects: Callable[[LabelRef, ExtentRef], bool],
) -> CoverageReport:
    """Classify every label and summarise."""
    extent_list = list(extents)
    label_list = list(labels)
    findings = tuple(classify(label, extent_list, intersects) for label in label_list)

    classes_seen = {label.class_id for label in label_list}
    classes_with_exhaustive = {e.class_id for e in extent_list if e.is_exhaustive}
    missing = tuple(sorted(classes_seen - classes_with_exhaustive))
    return CoverageReport(findings=findings, classes_without_extents=missing)


def extent_from_properties(
    properties: Mapping[str, Any],
    fields: CoreFields = DEFAULT_FIELDS,
    fallback_id: object = None,
) -> ExtentRef | None:
    """Build an :class:`ExtentRef` from an OAPIF feature's properties.

    Returns ``None`` when the feature has no class, because an extent without a class
    cannot license anything and silently defaulting it would be the dangerous choice.
    """
    class_id = properties.get(fields.class_id)
    if not isinstance(class_id, str) or not class_id:
        return None
    extent_id = properties.get(fields.extent_id) or fallback_id
    completeness = properties.get(fields.completeness)
    caveat = properties.get(fields.caveat)
    return ExtentRef(
        extent_id=str(extent_id) if extent_id is not None else "",
        class_id=class_id,
        # An extent that does not say how complete it is gets the safest reading, not
        # the most convenient one.
        completeness=str(completeness) if isinstance(completeness, str) else "unknown",
        caveat=caveat if isinstance(caveat, str) and caveat else None,
    )


def label_from_properties(
    properties: Mapping[str, Any],
    fields: CoreFields = DEFAULT_FIELDS,
    feature_id: object = None,
) -> LabelRef | None:
    """Build a :class:`LabelRef` from an OAPIF feature's properties."""
    class_id = properties.get(fields.class_id)
    if not isinstance(class_id, str) or not class_id:
        return None
    label_id = properties.get(fields.label_id)
    return LabelRef(
        feature_id=feature_id,
        label_id=str(label_id) if label_id else None,
        class_id=class_id,
    )
