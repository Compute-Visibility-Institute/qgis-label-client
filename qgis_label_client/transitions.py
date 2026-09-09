"""Commit a track or valid-time view only after every layer accepts it."""

from dataclasses import dataclass

from qgis.core import QgsDataProvider
from qgis.PyQt.QtXml import QDomDocument

from . import layers
from .core.errors import LabelClientError
from .settings import DEFAULTS, PluginSettings


class PendingSettings(PluginSettings):
    """An in-memory settings snapshot; URI construction cannot persist a proposal."""

    def __init__(self, settings, changes):
        self.values = {key: settings.get(key) for key in DEFAULTS}
        self.values.update(changes)

    def get(self, key):
        return self.values[key]

    def set(self, key, value):
        self.values[key] = value


class TransitionError(LabelClientError):
    """The proposed view failed; the message describes any incomplete recovery."""


@dataclass
class LayerState:
    layer: object
    source: str
    provider: str
    style: object
    read_only: bool
    abstract: str
    track: object

    @classmethod
    def capture(cls, layer):
        style = QDomDocument()
        if layer.exportNamedStyle(style):
            raise TransitionError(
                f"Could not save the style of {layer.name()!r} before changing it."
            )
        return cls(
            layer,
            layer.source(),
            layer.providerType(),
            style,
            layer.readOnly(),
            layer.abstract(),
            layer.customProperty(layers.TRACK_PROPERTY, None),
        )

    def restore(self):
        layer = self.layer
        layer.setDataSource(
            self.source, layer.name(), self.provider, QgsDataProvider.ProviderOptions(), False
        )
        if not layer.isValid():
            raise TransitionError("Previous provider could not be reopened.")
        restored, _message = layer.importNamedStyle(self.style)
        if not restored:
            raise TransitionError("Previous layer style could not be restored.")
        if self.track is None:
            layer.removeCustomProperty(layers.TRACK_PROPERTY)
        else:
            layer.setCustomProperty(layers.TRACK_PROPERTY, self.track)
        layer.setReadOnly(self.read_only)
        layer.setAbstract(self.abstract)
        layer.triggerRepaint()

    def disable(self):
        """Remove uncertain data from the canvas and prevent edits until reloaded."""
        self.layer.setDataSource(
            "", self.layer.name(), self.provider, QgsDataProvider.ProviderOptions(), False
        )
        self.layer.setReadOnly(True)
        self.layer.setAbstract("View change and restore failed. Remove and reload this layer.")
        self.layer.triggerRepaint()


def transition(targets, settings, changes, registry, track):
    """Validate all candidates, swap sources, then persist the chosen view.

    The layer being changed joins the rollback list *before* mutation: even a provider
    which raises halfway through setDataSource may already have replaced the old source.
    """
    targets = list(targets)
    if any(layer.isModified() for layer in targets):
        raise TransitionError("Save or discard unsaved edits before changing the view.")
    pending = PendingSettings(settings, changes)
    previous = {key: settings.get(key) for key in changes}
    touched = []
    settings_started = False
    try:
        states = [LayerState.capture(layer) for layer in targets]
        for layer in targets:
            layers.validate_repoint(layer, pending, registry, track)
        for state in states:
            touched.append(state)
            layers.repoint_for(state.layer, pending, registry, track)
        settings_started = True
        for key, value in changes.items():
            settings.set(key, value)
    except Exception as exc:
        failed = []
        unsafe = []
        unrestored_settings = []
        for state in reversed(touched):
            try:
                state.restore()
            except Exception:  # noqa: BLE001 - each failed provider must be disabled
                failed.append(state.layer.name())
                try:
                    state.disable()
                except Exception:  # noqa: BLE001 - continue restoring the other layers
                    unsafe.append(state.layer.name())
        if settings_started:
            for key, value in previous.items():
                try:
                    settings.set(key, value)
                except Exception:  # noqa: BLE001 - attempt every original setting
                    unrestored_settings.append(key)
        if unsafe or unrestored_settings:
            details = []
            if unsafe:
                details.append("Layers could not be safely disabled: " + ", ".join(unsafe))
            if unrestored_settings:
                details.append("Settings could not be restored: " + ", ".join(unrestored_settings))
            raise TransitionError(
                "View recovery is incomplete. Remove the affected layers and reconnect before "
                "continuing. " + "; ".join(details)
            ) from exc
        if failed:
            raise TransitionError(
                "View change failed. These layers could not be restored and were disabled; "
                "remove and reload them: " + ", ".join(failed)
            ) from exc
        raise TransitionError("View change failed; the previous view was retained.") from exc
    return len(targets)
