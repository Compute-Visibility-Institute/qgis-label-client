"""Choosing a label's ``valid_from``.

The defect under test is not a crash. It is a well-formed, plausible, false timestamp: a
compound traced from an April scene recorded as a claim about an afternoon in August.
Nothing rejects it, so the only defence is that the default is right by construction.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from qgis_label_client.core.validtime import (
    MEMORY_LIMIT,
    RECENT_LIMIT,
    CaptureRef,
    Memory,
    RasterStack,
    Source,
    recently_used,
    remember,
    resolve,
    revert_override,
)

APRIL = datetime(2026, 4, 21, 3, 40, 14, tzinfo=timezone.utc)
OCTOBER = datetime(2026, 10, 3, 3, 12, 0, tzinfo=timezone.utc)
MARCH = datetime(2026, 3, 1, tzinfo=timezone.utc)


def april_layer(layer_id: str = "wv03-april") -> CaptureRef:
    return CaptureRef(layer_id, "WV03 26APR21", APRIL, capture_id="cap-april")


def october_layer(layer_id: str = "wv03-october") -> CaptureRef:
    return CaptureRef(layer_id, "WV03 03OCT26", OCTOBER, capture_id="cap-october")


def basemap(layer_id: str = "osm") -> CaptureRef:
    """A layer with no acquisition time. Ordinary, and must not vote."""
    return CaptureRef(layer_id, "OpenStreetMap")


# ---------------------------------------------------------------------------
# Deriving from the scene
# ---------------------------------------------------------------------------


def test_default_comes_from_the_topmost_dated_raster() -> None:
    resolved = resolve(RasterStack.of([april_layer()]))
    assert resolved.value == APRIL
    assert resolved.source is Source.CAPTURE


def test_a_basemap_above_the_imagery_does_not_win() -> None:
    """It has no date to offer, so it must be looked past rather than treated as absent."""
    resolved = resolve(RasterStack.of([basemap(), april_layer()]))
    assert resolved.value == APRIL


def test_the_top_scene_wins_when_two_are_loaded() -> None:
    resolved = resolve(RasterStack.of([october_layer(), april_layer()]))
    assert resolved.value == OCTOBER


def test_nothing_dated_yields_no_value_rather_than_now() -> None:
    """The whole point. A guess that looks like an answer is the defect, not the fix."""
    resolved = resolve(RasterStack.of([basemap()]))
    assert resolved.value is None
    assert resolved.source is Source.UNKNOWN
    assert "by hand" in resolved.describe()


def test_an_empty_canvas_is_not_an_error() -> None:
    assert resolve(RasterStack()).source is Source.UNKNOWN


# ---------------------------------------------------------------------------
# Stickiness
# ---------------------------------------------------------------------------


def test_the_second_polygon_on_the_same_imagery_inherits_the_first() -> None:
    stack = RasterStack.of([april_layer()])
    memory = remember(None, resolve(stack), APRIL)
    assert resolve(stack, memory).source is Source.STICKY


def test_bringing_another_scene_to_the_top_abandons_the_sticky_value() -> None:
    """The safety argument for stickiness: it can only apply to the imagery it was chosen for."""
    april = RasterStack.of([april_layer()])
    memory = remember(None, resolve(april), MARCH)

    october = RasterStack.of([october_layer(), april_layer()])
    resolved = resolve(october, memory)
    assert resolved.value == OCTOBER
    assert resolved.source is Source.CAPTURE


def test_reordering_the_same_two_layers_changes_the_stack() -> None:
    """Same layers loaded, different one being traced. Order is part of identity."""
    top_april = RasterStack.of([april_layer(), october_layer()])
    top_october = RasterStack.of([october_layer(), april_layer()])
    assert top_april.fingerprint() != top_october.fingerprint()


def test_a_resigned_url_does_not_discard_the_analysts_choices() -> None:
    """The plugin re-points every raster at a fresh URL at session start.

    A fingerprint built from layer sources would go stale every morning and silently
    throw away every remembered decision. Identity is the layer and its instant.
    """
    before = RasterStack.of([april_layer()])
    memory = remember(None, resolve(before), MARCH)
    after = RasterStack.of([CaptureRef("wv03-april", "WV03 26APR21 (refreshed)", APRIL)])
    assert resolve(after, memory).value == MARCH


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


def test_typing_a_different_date_is_remembered_as_an_override() -> None:
    stack = RasterStack.of([april_layer()])
    memory = remember(None, resolve(stack), MARCH)

    resolved = resolve(stack, memory)
    assert resolved.source is Source.OVERRIDE
    assert resolved.value == MARCH
    assert resolved.derived == APRIL


def test_the_override_description_names_both_numbers() -> None:
    """A reviewer has to see that a human decided this, and what they decided against."""
    stack = RasterStack.of([april_layer()])
    memory = remember(None, resolve(stack), MARCH)
    described = resolve(stack, memory).describe()
    assert "1 Mar 2026" in described
    assert "21 Apr 2026" in described


def test_accepting_the_proposed_date_is_not_an_override() -> None:
    """Otherwise every label carries a permanent "you set this" over the default value."""
    stack = RasterStack.of([april_layer()])
    memory = remember(None, resolve(stack), APRIL)
    assert resolve(stack, memory).source is Source.STICKY


def test_the_same_instant_spelled_naively_is_not_an_override() -> None:
    """A form widget hands back a naive datetime; the backend sends an aware one."""
    stack = RasterStack.of([april_layer()])
    memory = remember(None, resolve(stack), APRIL.replace(tzinfo=None))
    assert resolve(stack, memory).source is Source.STICKY


def test_switching_away_and_back_restores_the_override() -> None:
    """The analyst's reasoning about a scene does not expire because they looked elsewhere."""
    april = RasterStack.of([april_layer()])
    october = RasterStack.of([october_layer()])

    memory = remember(None, resolve(april), MARCH)
    memory = remember(memory, resolve(october, memory), OCTOBER)

    assert resolve(april, memory).value == MARCH


def test_reverting_an_override_goes_back_to_the_scene() -> None:
    stack = RasterStack.of([april_layer()])
    memory = remember(None, resolve(stack), MARCH)
    assert resolve(stack, memory).can_revert

    memory, resolved = revert_override(memory, stack)
    assert resolved.value == APRIL
    assert resolved.source is Source.CAPTURE


def test_reverting_with_no_dated_imagery_says_so_rather_than_pretending() -> None:
    """"Attempts to auto find it" -- and it can fail. A button that silently does nothing is worse."""
    stack = RasterStack.of([basemap()])
    memory = remember(None, resolve(stack), MARCH)
    _, resolved = revert_override(memory, stack)
    assert resolved.value is None
    assert resolved.source is Source.UNKNOWN


def test_an_override_over_undated_imagery_offers_no_revert() -> None:
    stack = RasterStack.of([basemap()])
    memory = remember(None, resolve(stack), MARCH)
    assert resolve(stack, memory).can_revert is False


def test_reverting_keeps_the_value_in_the_recently_used_list() -> None:
    """Undoing one label's date is not a statement that the instant was never useful."""
    stack = RasterStack.of([april_layer()])
    memory = remember(None, resolve(stack), MARCH)
    memory, _ = revert_override(memory, stack)
    assert MARCH in recently_used(memory)


# ---------------------------------------------------------------------------
# Recently used, and bounds
# ---------------------------------------------------------------------------


def test_recently_used_is_newest_first_and_deduplicated() -> None:
    stack = RasterStack.of([april_layer()])
    memory = None
    for value in (MARCH, APRIL, MARCH):
        memory = remember(memory, resolve(stack, memory), value)
    assert recently_used(memory)[0] == MARCH
    assert list(recently_used(memory)).count(MARCH) == 1


def test_recently_used_is_bounded() -> None:
    stack = RasterStack.of([april_layer()])
    memory = None
    for day in range(RECENT_LIMIT + 5):
        memory = remember(memory, resolve(stack, memory), MARCH + timedelta(days=day))
    assert len(recently_used(memory)) == RECENT_LIMIT


def test_per_stack_memory_is_bounded() -> None:
    """Persisted, so an unbounded map keyed on layer ids grows for the life of a profile."""
    memory = None
    for index in range(MEMORY_LIMIT + 5):
        stack = RasterStack.of([CaptureRef(f"layer-{index}", "scene", APRIL)])
        memory = remember(memory, resolve(stack, memory), MARCH)
    assert len(memory.by_stack) == MEMORY_LIMIT


def test_saving_nothing_changes_nothing() -> None:
    stack = RasterStack.of([april_layer()])
    memory = Memory()
    assert remember(memory, resolve(stack), None) is memory


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_memory_survives_a_round_trip_through_settings() -> None:
    from qgis_label_client.core.validtime import from_payload, to_payload

    stack = RasterStack.of([april_layer()])
    memory = remember(None, resolve(stack), MARCH)

    restored = from_payload(to_payload(memory))
    assert resolve(stack, restored).value == MARCH
    assert resolve(stack, restored).source is Source.OVERRIDE


def test_a_corrupt_settings_entry_degrades_to_no_memory() -> None:
    """Read at startup on the path that installs the field default.

    Raising here would leave the analyst with no default at all — strictly worse than
    the forgotten stickiness that discarding one entry costs.
    """
    from qgis_label_client.core.validtime import from_payload

    assert from_payload("not a mapping") == Memory()
    assert from_payload({"by_stack": [{"stack": "s"}], "recent": ["nonsense"]}) == Memory()


def test_settings_payload_is_json_able_primitives_only() -> None:
    """QgsSettings round-trips through an INI file on some platforms."""
    import json

    from qgis_label_client.core.validtime import to_payload

    stack = RasterStack.of([april_layer()])
    memory = remember(None, resolve(stack), MARCH)
    json.dumps(to_payload(memory))  # must not raise
