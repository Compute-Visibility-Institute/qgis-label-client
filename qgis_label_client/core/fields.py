"""Property names for the *stable core* of the label schema.

READ THIS BEFORE ASSUMING THIS FILE VIOLATES THE NO-HARDCODED-ATTRIBUTES RULE.

Label attributes are never hardcoded anywhere in this plugin. They live in ``attrs`` and
``names`` as JSONB, governed by the JSON Schema each class publishes in the class
registry, and every attribute name the plugin ever renders is read from that registry at
runtime. Adding an attribute is a row update on the server and requires no plugin
release.

What is named here is the other half of the schema's deliberate split: identity, class,
valid time and provenance, which are real columns with real constraints precisely
because correctness depends on them. ``label_id`` is not an attribute -- it is the
server-assigned immutable UUID that the entire bitemporal model hangs off, and the
single hardest defect the platform exists to fix. Treating it as fluid would be the
mistake, not treating it as fixed.

Even so, none of these are compiled in. A deployment that renames a view column sends a
``fields`` block in its class-registry document and :meth:`CoreFields.merged` picks it
up. The values below are defaults, not assumptions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from dataclasses import fields as dataclass_fields


@dataclass(frozen=True)
class CoreFields:
    """Names of the stable columns as the API exposes them over OAPIF."""

    # --- identity -------------------------------------------------------------
    # The OAPIF scalar feature id (``label.id``, a surrogate bigint) is carried by the
    # GeoJSON ``id`` member, not by a property, so it is not named here. label_id is the
    # identity; they are never interchangeable.
    label_id: str = "label_id"
    class_id: str = "class_id"

    # --- flexible metadata containers -----------------------------------------
    # The containers are fixed; their contents are not. Nothing below indexes into them.
    attrs: str = "attrs"
    names: str = "names"
    name_en: str = "name_en"
    name_zh: str = "name_zh"

    # --- valid time ------------------------------------------------------------
    valid_from: str = "valid_from"
    valid_to: str = "valid_to"

    # --- provenance ------------------------------------------------------------
    capture_id: str = "capture_id"
    updated_by: str = "updated_by"
    updated_at: str = "updated_at"

    # --- label_history / v_label_audit ----------------------------------------
    history_id: str = "history_id"
    operation: str = "operation"
    changed: str = "changed"
    actor: str = "actor"
    reason: str = "reason"
    recorded_from: str = "recorded_from"
    recorded_to: str = "recorded_to"

    # --- labeled_extent ---------------------------------------------------------
    extent_id: str = "extent_id"
    completeness: str = "completeness"
    caveat: str = "caveat"
    surveyed_by: str = "surveyed_by"

    def merged(self, overrides: Mapping[str, object] | None) -> CoreFields:
        """Return a copy with any server-supplied overrides applied.

        Unknown keys are ignored rather than raising: a newer backend adding a field
        name this plugin version does not know about must not stop an annotator working.
        """
        if not overrides:
            return self
        known = {f.name for f in dataclass_fields(self)}
        applied = {
            key: str(value)
            for key, value in overrides.items()
            if key in known and isinstance(value, str) and value
        }
        return replace(self, **applied) if applied else self


DEFAULT_FIELDS = CoreFields()

#: Values of ``labeled_extent.completeness`` (see db/migrations/007_labeled_extent.sql).
#: Only ``exhaustive`` licenses treating unlabeled ground inside the polygon as negative.
COMPLETENESS_EXHAUSTIVE = "exhaustive"
COMPLETENESS_PARTIAL = "partial"
COMPLETENESS_UNKNOWN = "unknown"
