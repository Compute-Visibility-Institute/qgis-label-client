"""Wiring :mod:`.core.validtime` into QGIS: the layer tree, the field default, settings.

The rules live in :mod:`.core.validtime` and are tested without a running QGIS. This file
is the half that cannot be: it reads the layer tree, writes ``QgsSettings``, and installs
the default that makes a new polygon arrive with its ``valid_from`` already right.

WHY A FIELD DEFAULT AND NOT A CUSTOM SAVE DIALOG

The obvious build is a dialog that intercepts the save and asks for a date. It is the
wrong one. QGIS already has a place where a new feature's attributes are proposed and
edited -- the attribute form -- and analysts already know it. Replacing it with a plugin
dialog means reimplementing field validation, the class picker and the JSONB attribute
widgets, and it means the plugin has to intercept a gesture (save) that QGIS offers about
six ways to trigger.

A ``QgsDefaultValue`` needs none of that. It is evaluated when the form opens, so the
analyst sees the proposed date in the ordinary place, in the ordinary widget, and can type
over it exactly as they would any other field. The plugin's job shrinks to answering one
question -- what should this say -- which is precisely the pure function next door.

THE FUNCTION IS EVALUATED PER FEATURE, WHICH IS THE POINT

``cvi_valid_from()`` resolves against the CURRENT layer tree every time the form opens, so
bringing another scene to the top changes the next polygon's default without anything
having to invalidate a cache. That is also why it must stay cheap: no network, no database.
Everything it reads was stamped onto the raster layers during the signed-URL refresh.
"""

from __future__ import annotations

from datetime import datetime

from qgis.core import (
    Qgis,
    QgsDefaultValue,
    QgsExpression,
    QgsProject,
    QgsSettings,
    QgsVectorLayer,
)

from .core.assets import ASSET_KEY_PROPERTY, CAPTURED_AT_PROPERTY
from .core.fields import DEFAULT_FIELDS, CoreFields
from .core.validtime import (
    CaptureRef,
    Memory,
    RasterStack,
    Resolution,
    from_payload,
    remember,
    resolve,
    revert_override,
    to_payload,
)
from .log import log, log_warning

#: Settings key for the remembered decisions. Under the plugin's own group.
MEMORY_KEY = "cvi-label-client/valid_time_memory"

#: The expression a new feature's ``valid_from`` defaults to.
DEFAULT_EXPRESSION = "cvi_valid_from()"


def current_stack(project: QgsProject | None = None) -> RasterStack:
    """The dated imagery actually on screen, topmost first.

    VISIBILITY IS PART OF THE QUESTION. A raster that is loaded but unchecked is not being
    traced from, and letting it vote would make the default follow a scene nobody is
    looking at -- the exact failure this whole mechanism exists to prevent, arrived at by
    a different route.

    ``layerOrder()`` rather than the tree's own order, because a project using the custom
    layer order panel draws in an order that does not match the legend. What the analyst
    traces is what is drawn on top, so drawing order is the one that counts.
    """
    project = project or QgsProject.instance()
    root = project.layerTreeRoot()

    visible: dict[str, bool] = {}
    names: dict[str, str] = {}
    for node in root.findLayers():
        layer = node.layer()
        if layer is not None:
            visible[layer.id()] = bool(node.isVisible())
            names[layer.id()] = layer.name()

    refs: list[CaptureRef] = []
    for layer in root.layerOrder():
        if layer is None or layer.type() != Qgis.LayerType.Raster:
            continue
        if not visible.get(layer.id(), False):
            continue
        refs.append(
            CaptureRef(
                layer_id=layer.id(),
                layer_name=names.get(layer.id(), layer.name()),
                captured_at=_captured_at(layer),
                capture_id=str(layer.customProperty(ASSET_KEY_PROPERTY, "")) or None,
            )
        )
    return RasterStack.of(refs)


def _captured_at(layer: object) -> datetime | None:
    """Read the instant stamped during the signed-URL refresh.

    Returns ``None`` for anything unparseable rather than raising: this runs on the path
    that installs a form default, and an exception here costs the analyst the default
    entirely, which is strictly worse than falling back to deriving from a lower layer.
    """
    raw = layer.customProperty(CAPTURED_AT_PROPERTY, "")  # type: ignore[attr-defined]
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        log_warning(
            f"Raster layer {layer.name()!r} carries an unreadable "  # type: ignore[attr-defined]
            f"{CAPTURED_AT_PROPERTY} ({raw!r}); ignoring it for valid-time defaults."
        )
        return None


def load_memory(settings: QgsSettings | None = None) -> Memory:
    """Remembered decisions, or an empty memory if the profile has none or a broken one."""
    store = settings or QgsSettings()
    return from_payload(store.value(MEMORY_KEY, None))


def save_memory(memory: Memory, settings: QgsSettings | None = None) -> None:
    store = settings or QgsSettings()
    store.setValue(MEMORY_KEY, to_payload(memory))


def propose(project: QgsProject | None = None) -> Resolution:
    """What the next polygon's ``valid_from`` should say, and why."""
    return resolve(current_stack(project), load_memory())


def record_choice(chosen: datetime | None, project: QgsProject | None = None) -> Memory:
    """Remember what was actually saved, so the next polygon inherits it.

    Called after a successful commit rather than when the form closes: a date the analyst
    typed into a feature they then rolled back is not a decision they made about anything.
    """
    stack = current_stack(project)
    memory = remember(load_memory(), resolve(stack, load_memory()), chosen)
    save_memory(memory)
    return memory


def revert(project: QgsProject | None = None) -> Resolution:
    """Drop the override for the current imagery and re-derive from the scene."""
    stack = current_stack(project)
    memory, resolution = revert_override(load_memory(), stack)
    save_memory(memory)
    log(f"Valid-time override reverted: {resolution.describe()}")
    return resolution


def install_default(
    layer: QgsVectorLayer, fields: CoreFields = DEFAULT_FIELDS
) -> bool:
    """Make new features arrive with ``valid_from`` already proposed.

    ``applyOnUpdate=False`` is load-bearing. With it true the expression would re-evaluate
    on every edit, so correcting a polygon's geometry six months later would silently
    re-stamp its valid time to whatever imagery happens to be open then -- turning a
    geometry fix into a false claim about when the building existed. The default is for
    features being CREATED and nothing else.
    """
    index = layer.fields().indexOf(fields.valid_from)
    if index < 0:
        # Not an error. A deployment that has not applied 013_valid_time.sql serves a
        # collection without the column, and the plugin should keep working against it
        # rather than refusing to add the layer.
        log_warning(
            f"Layer {layer.name()!r} has no {fields.valid_from!r} field; "
            "valid-time defaults are unavailable against this backend."
        )
        return False
    layer.setDefaultValueDefinition(index, QgsDefaultValue(DEFAULT_EXPRESSION, False))
    return True


def register_functions() -> None:
    """Register ``cvi_valid_from()`` so the field default can call it.

    Registered once per session and unregistered by :func:`unregister_functions` on plugin
    unload -- a stale function bound to a torn-down plugin is the classic way a QGIS plugin
    leaves a project unopenable after a reload.
    """
    if QgsExpression.isFunctionName(_FUNCTION_NAME):
        return
    QgsExpression.registerFunction(_valid_from_function())


def unregister_functions() -> None:
    if QgsExpression.isFunctionName(_FUNCTION_NAME):
        QgsExpression.unregisterFunction(_FUNCTION_NAME)


_FUNCTION_NAME = "cvi_valid_from"


def _valid_from_function():  # pragma: no cover - requires a running QGIS
    from qgis.core import qgsfunction

    @qgsfunction(args=0, group="CVI", register=False, usesGeometry=False)
    def cvi_valid_from(values, feature, parent):  # noqa: ARG001
        """When the label being drawn was true on the ground.

        Defaults to the acquisition time of the topmost visible imagery, carries the
        previous choice forward while that imagery is unchanged, and honours an override
        the analyst made for this scene. Returns NULL when nothing on top is dated, so
        the form insists rather than inventing a date.
        """
        resolution = propose()
        return resolution.value if resolution.value is not None else None

    return cvi_valid_from
