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

    # --- history track ---------------------------------------------------------
    # The THIRD axis, and as much a part of the stable core as label_id is: valid time
    # says when a thing was true, transaction time says when we believed it, and this
    # says which "we". Server-assigned from app.writable_track_id() and immutable -- a
    # label cannot be moved between tracks, because its label_id is what every
    # label_history row is keyed on and moving it would merge two audit chains.
    #
    # Readable rather than hidden, for exactly the reason label_id is: a client that
    # cannot see the value cannot notice when it is wrong.
    track_id: str = "track_id"

    # --- flexible metadata containers -----------------------------------------
    # The containers are fixed; their contents are not. Nothing below indexes into them.
    attrs: str = "attrs"
    names: str = "names"
    name_en: str = "name_en"
    name_zh: str = "name_zh"

    # --- valid time ------------------------------------------------------------
    valid_from: str = "valid_from"
    valid_to: str = "valid_to"

    # --- geometry family -------------------------------------------------------
    # Present ONLY on the collections that mix geometry types (the read-only
    # current/as-of/history views). Its presence is how the plugin recognises a
    # mixed collection at runtime, rather than by knowing their names -- which
    # would put deployment vocabulary back into this repository.
    #
    # Point | LineString | Polygon, collapsing the Multi variants, matching
    # label_class.geom_type families and core.routing.geometry_family().
    geom_family: str = "geom_family"

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

    # --- v_label_asof: the transaction-time view --------------------------------
    # The historical-view collection (:mod:`.recorded`) mirrors the live label view and
    # adds these. They are named here, not in .recorded, because a deployment renames a
    # view column by sending a `fields` block -- the same escape hatch every other column
    # above has, and a historical layer is exactly as much a deployment's own shape.
    #
    # asof_id is the OAPIF feature id of that collection and IS a property there, unlike
    # the live collection's surrogate id: as-of rows come from the live table UNION the
    # history table, two id spaces that would collide, so identity is `label_id` plus the
    # start of the valid range.
    asof_id: str = "asof_id"
    # Transaction-time bounds. TEXT on the wire rather than timestamps, and that is a
    # correctness requirement rather than a formatting choice -- see .recorded and
    # docs/read-path.md: QGIS's Part 1 filter compiler decides whether to emit `datetime=`
    # from the FIELD'S TYPE, so a subset filter on a DateTime-typed transaction-time column
    # would compile to `datetime=` and be applied by the server to VALID time. Wrong axis,
    # silently, with a plausible result.
    belief_from: str = "belief_from"
    belief_to: str = "belief_to"
    # True where the belief has since ended -- deleted OR corrected. What the historical
    # layer's styling keys on.
    superseded: str = "superseded"
    # The instant the view actually resolved at, echoed on every row. The canary: see
    # .recorded.canary_filter.
    recorded_at: str = "recorded_at"

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
