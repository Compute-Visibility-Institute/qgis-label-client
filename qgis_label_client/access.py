"""Apply caller permissions to native layers without changing their edit buffers."""

import json

from . import layers

READ_ONLY_REASON = "Read-only access: your account cannot save labels to this backend."
ACCESS_PROPERTY = "cvi_label_client/caller_read_only"


class LayerAccess:
    """Persist ownership with the layer so saved reader projects reopen for writers."""

    def apply(self, targets, writable):
        editing = []
        for layer in targets:
            try:
                state = json.loads(layer.customProperty(ACCESS_PROPERTY, ""))
            except (ValueError, TypeError):
                state = None
            if not (
                isinstance(state, dict)
                and state.get("version") == 1
                and isinstance(state.get("abstract"), str)
            ):
                state = None
            if layers.recorded_at_of(layer):
                if state is not None:
                    layer.removeCustomProperty(ACCESS_PROPERTY)
                continue
            if writable is False:
                if layer.isEditable():
                    editing.append(layer.name())
                    continue
                if state is None:
                    if layer.readOnly():
                        continue
                    layer.setCustomProperty(
                        ACCESS_PROPERTY,
                        json.dumps({"version": 1, "abstract": layer.abstract()}),
                    )
                layer.setReadOnly(True)
                layer.setAbstract(READ_ONLY_REASON)
            elif state is not None:
                # Unknown identity must not leave a writer locked out. Restore only
                # our own flag; provider capabilities still govern actual editing.
                if not layer.isEditable():
                    layer.setReadOnly(False)
                layer.removeCustomProperty(ACCESS_PROPERTY)
                if layer.abstract() == READ_ONLY_REASON:
                    layer.setAbstract(state["abstract"])
        return editing
