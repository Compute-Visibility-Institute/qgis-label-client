"""Reading the layer tree: which imagery the analyst is actually tracing.

The rules are tested in ``test_validtime.py`` without QGIS. What is tested here is the
one thing that file cannot reach — turning a project's layer tree into the stack those
rules run against — because getting *this* wrong produces a default that is confidently
derived from a scene nobody is looking at.
"""

from __future__ import annotations

from datetime import datetime, timezone

from qgis.core import Qgis

from qgis_label_client.core.assets import ASSET_KEY_PROPERTY, CAPTURED_AT_PROPERTY
from qgis_label_client.validtime import current_stack

APRIL = datetime(2026, 4, 21, 3, 40, 14, tzinfo=timezone.utc)
OCTOBER = datetime(2026, 10, 3, 3, 12, 0, tzinfo=timezone.utc)


class FakeLayer:
    def __init__(self, layer_id, name, captured_at=None, raster=True):
        self._id = layer_id
        self._name = name
        self._type = Qgis.LayerType.Raster if raster else Qgis.LayerType.Vector
        self.properties = {}
        if captured_at is not None:
            self.properties[CAPTURED_AT_PROPERTY] = captured_at.isoformat()
            self.properties[ASSET_KEY_PROPERTY] = f"{layer_id}:visual"

    def id(self):
        return self._id

    def name(self):
        return self._name

    def type(self):
        return self._type

    def customProperty(self, key, default=""):  # noqa: N802 - Qt naming
        return self.properties.get(key, default)


class FakeNode:
    def __init__(self, layer, visible=True):
        self._layer = layer
        self._visible = visible

    def layer(self):
        return self._layer

    def isVisible(self):  # noqa: N802 - Qt naming
        return self._visible


class FakeRoot:
    def __init__(self, nodes):
        self._nodes = nodes

    def findLayers(self):  # noqa: N802 - Qt naming
        return self._nodes

    def layerOrder(self):  # noqa: N802 - Qt naming
        return [node.layer() for node in self._nodes]


class FakeProject:
    def __init__(self, nodes):
        self._root = FakeRoot(nodes)

    def layerTreeRoot(self):  # noqa: N802 - Qt naming
        return self._root


def test_the_topmost_visible_raster_supplies_the_date() -> None:
    project = FakeProject(
        [
            FakeNode(FakeLayer("october", "WV03 03OCT26", OCTOBER)),
            FakeNode(FakeLayer("april", "WV03 26APR21", APRIL)),
        ]
    )
    assert current_stack(project).top_dated().captured_at == OCTOBER


def test_an_unchecked_layer_does_not_vote() -> None:
    """A raster that is loaded but not drawn is not being traced from.

    Letting it win would make the default follow a scene nobody is looking at — the
    same failure the whole mechanism exists to prevent, reached by another route.
    """
    project = FakeProject(
        [
            FakeNode(FakeLayer("october", "WV03 03OCT26", OCTOBER), visible=False),
            FakeNode(FakeLayer("april", "WV03 26APR21", APRIL)),
        ]
    )
    assert current_stack(project).top_dated().captured_at == APRIL


def test_vector_layers_are_not_imagery() -> None:
    project = FakeProject(
        [
            FakeNode(FakeLayer("labels", "Labels", raster=False)),
            FakeNode(FakeLayer("april", "WV03 26APR21", APRIL)),
        ]
    )
    stack = current_stack(project)
    assert [ref.layer_id for ref in stack.layers] == ["april"]


def test_an_undated_raster_is_kept_but_carries_no_date() -> None:
    """It is still part of what is on screen, so it belongs in the fingerprint.

    Dropping it would make toggling a basemap invisible to stickiness, which is right,
    but it would also make two genuinely different stacks fingerprint identically.
    """
    project = FakeProject(
        [
            FakeNode(FakeLayer("basemap", "OpenStreetMap")),
            FakeNode(FakeLayer("april", "WV03 26APR21", APRIL)),
        ]
    )
    stack = current_stack(project)
    assert len(stack.layers) == 2
    assert stack.top_dated().layer_id == "april"


def test_an_unparseable_stamp_is_ignored_rather_than_raised() -> None:
    """This runs on the path that installs the form default.

    Raising costs the analyst the default entirely — worse than falling through to the
    layer below, which is merely less specific.
    """
    broken = FakeLayer("broken", "Corrupt stamp")
    broken.properties[CAPTURED_AT_PROPERTY] = "not-a-date"
    project = FakeProject([FakeNode(broken), FakeNode(FakeLayer("april", "A", APRIL))])
    assert current_stack(project).top_dated().captured_at == APRIL


def test_an_empty_project_is_not_an_error() -> None:
    assert current_stack(FakeProject([])).layers == ()


# ── the reference that stops QGIS segfaulting on unload ──────────────────────


def test_the_registered_function_is_held_by_the_module() -> None:
    """QgsExpression.registerFunction stores a RAW POINTER and takes no reference.

    Written as `registerFunction(_valid_from_function())` this reads correctly and
    crashes QGIS: the object is collected the moment the call returns and QGIS is left
    pointing at freed memory. Unticking the plugin dereferences it and takes the whole
    application down — no traceback, because the interpreter is gone before it could
    write one. Observed on 3.44.13.

    So the assertion is about object lifetime, not about behaviour, and it is the only
    thing standing between a refactor and a hard crash that no other test can catch.
    """
    from qgis_label_client import validtime

    validtime.unregister_functions()
    assert validtime._registered_function is None

    validtime.register_functions()
    assert validtime._registered_function is not None, (
        "nothing holds the function; QGIS will dereference freed memory on unload"
    )


def test_unregistering_drops_the_reference_only_after_qgis_has_let_go() -> None:
    """Clearing the name first would recreate the dangling pointer, in the one code path
    most likely to be running during interpreter teardown."""
    from qgis_label_client import validtime

    validtime.register_functions()
    validtime.unregister_functions()
    assert validtime._registered_function is None


def test_registering_twice_does_not_replace_a_live_registration() -> None:
    """A plugin reload calls initGui again; swapping the object under a live pointer is
    the same crash by another route."""
    from qgis_label_client import validtime

    validtime.unregister_functions()
    validtime.register_functions()
    first = validtime._registered_function
    validtime.register_functions()
    assert validtime._registered_function is first


# ── the conversion that stops the form silently showing NULL ─────────────────


def test_the_proposal_is_converted_to_a_qdatetime() -> None:
    """A Python datetime reaches a DateTime field as NULL, with no error anywhere.

    Observed on QGIS 3.44.13. Every diagnostic said the feature worked:

        field type:   DateTime (QVariant 16)
        evaluates to: datetime.datetime(2026, 4, 21, 3, 40, 14, tzinfo=utc)
        eval error:   (none)

    and the attribute form showed NULL. sip does not map a Python datetime onto
    QDateTime on the way out of an expression function, so createFeature gets a type it
    cannot store and discards it rather than raising.

    The assertion is about the returned TYPE, not the value, because the value was
    already right and that is exactly what made this hard to see.
    """
    from qgis.PyQt.QtCore import QDateTime

    from qgis_label_client.validtime import _as_qdatetime

    out = _as_qdatetime(APRIL)
    assert isinstance(out, QDateTime), (
        "a Python datetime is silently dropped by a DateTime field; QGIS needs a QDateTime"
    )


def test_the_conversion_normalises_to_utc_before_taking_components() -> None:
    """An aware datetime in another zone must not be read off in local wall-clock time.

    A string round trip, or reading .hour off a non-UTC value, would put the deployment's
    locale between the instant the sensor recorded and the instant stored -- the exact
    class of error valid time exists to remove.

    Asserted on what QDate/QTime were CONSTRUCTED with rather than on what the stub
    returns: checking the return value would be checking the stub, not this code.
    """
    from datetime import timedelta, timezone as tz

    from qgis_label_client.validtime import _as_qdatetime

    # 05:40:14+02:00 is the same instant as 03:40:14Z. Read naively it is the wrong hour.
    other_zone = APRIL.astimezone(tz(timedelta(hours=2)))
    assert other_zone.hour == 5, "fixture must actually differ in wall-clock terms"

    out = _as_qdatetime(other_zone)
    qdate, qtime = out.stub_args[0], out.stub_args[1]
    assert qdate.stub_args == (2026, 4, 21)
    assert qtime.stub_args == (3, 40, 14), "components must come from the UTC instant"
