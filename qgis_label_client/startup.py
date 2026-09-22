"""Connection prompts and safe refreshes of already-loaded native layers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from qgis.core import Qgis
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import QAction, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from . import layers


@dataclass
class RefreshResult:
    refreshed: list[str] = field(default_factory=list)
    editing: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def refresh_connected_layers(
    backend_url: str, *, track: str | None = None, collection_ids: set[str] | None = None
) -> RefreshResult:
    """Invalidate clean live providers' caches without touching any edit buffer.

    The next canvas/table request fetches fresh server data using the layer's existing
    track and time/canvas filters. This does not eagerly download the whole database.
    Historical views stay pinned and another backend is never touched.
    """
    result = RefreshResult()
    for layer in layers.live_layers():
        if not layers.belongs_to_backend(layer, backend_url):
            continue
        if track is not None and layers.track_of(layer) != track:
            continue
        if collection_ids is not None and layers.collection_of(layer) not in collection_ids:
            continue
        if layer.isModified() or layer.customProperty("cvi/pending_state", ""):
            result.editing.append(layer.name())
            continue
        try:
            if layer.isEditable():
                # commitChanges(False) keeps editing enabled after a successful push.
                # Invalidate only provider data; leave the empty edit buffer attached.
                layer.dataProvider().reloadData()
                layer.updateExtents()
            else:
                layer.reload()
            if layer.providerType() == layers.CLASS_PROVIDER:
                layers.check_class_refresh(layer)
                layer.updateFields()
                layers.configure_class_columns(layer, layers.class_layer_metadata(layer))
            layer.triggerRepaint()
            result.refreshed.append(layer.name())
        except Exception as exc:  # noqa: BLE001 - one failed provider must not block others
            result.failed.append(f"{layer.name()}: {exc}")
    return result


class ConnectionDialog(QDialog):
    """Use the plugin's existing sign-in lifecycle, never a second OAuth flow."""

    def __init__(self, owner):
        super().__init__(owner.plugin.iface.mainWindow())
        self.setWindowTitle("CVI — sign in and connect")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        instructions = QLabel(
            "Sign in with your work Google account, then Connect to refresh the "
            "labels loaded in this project. Existing local edits are kept.",
            self,
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.sign_out = QPushButton("Sign out", self)
        self.sign_in = QPushButton("Sign in with Google", self)
        self.connect = QPushButton("Connect", self)
        self.sign_out.clicked.connect(owner.sign_out)
        self.sign_in.clicked.connect(owner.sign_in)
        self.connect.clicked.connect(owner.connect)
        for button in (self.sign_out, self.sign_in, self.connect):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        note = QLabel(
            "Change Show connection prompt on startup in the CVI panel's Backend section.",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        close = QPushButton("Continue in QGIS", self)
        close.clicked.connect(self.close)
        layout.addWidget(close)


class StartupConnection:
    def __init__(self, plugin):
        self.plugin = plugin
        self.dialog = None
        self.timer = None
        self.start_timer = None
        self.connected = False
        self.connection_note = ""
        self.closed = False

    def install(self, menu_name):
        self.closed = False
        show = QAction("Sign in and connect…", self.plugin.iface.mainWindow())
        show.triggered.connect(self.show)
        self.plugin.iface.addPluginToMenu(menu_name, show)
        self.plugin.teardown.add(
            "menu: connection prompt",
            lambda: self.plugin.iface.removePluginMenu(menu_name, show),
        )
        self.plugin.dock.startupPromptChanged.connect(self.set_enabled)
        self.start_timer = QTimer(self.plugin.dock)
        self.start_timer.setSingleShot(True)
        self.start_timer.timeout.connect(self.show_if_enabled)
        self.start_timer.start(0)

    def set_enabled(self, enabled):
        self.plugin.settings.set("show_startup_connection", bool(enabled))

    def show_if_enabled(self):
        if self.plugin.settings.get("show_startup_connection"):
            self.show()

    def show(self):
        if self.closed or self.plugin.dock is None:
            return
        if self.dialog is None:
            self.dialog = ConnectionDialog(self)
            self.timer = QTimer(self.dialog)
            self.timer.timeout.connect(self.update_status)
            self.dialog.finished.connect(lambda *_, timer=self.timer: timer.stop())
        self.update_status()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        # Reflect existing browser/renewal tasks without adding any network polling.
        self.timer.start(500)

    def update_status(self):
        if self.dialog is None:
            return
        settings = self.plugin.settings
        signed_in = bool(settings.authcfg)
        signing_in = self.plugin.signin is not None
        connecting = self.plugin._registry_pending
        busy = self.plugin.activities.state.busy
        if not signed_in:
            self.connected = False
            self.connection_note = ""
            message = "Not signed in. Sign in with Google, then Connect."
        elif settings.oauth_expires_at and settings.oauth_expires_at <= time.time():
            message = "Your sign-in has expired. Connect can renew it, or sign in again."
        elif self.connected and self.plugin.registry is not None:
            message = self.connection_note or "Connected. Your loaded layers are ready."
        else:
            message = f"Signed in as {settings.oauth_email or 'your account'}. Click Connect."
        if signing_in:
            message = "Finish signing in in your browser, then return here and Connect."
        elif connecting:
            message = "Connecting and checking server access…"
        elif busy:
            message = "Waiting for the current operation to finish…"
        self.dialog.status.setText(message)
        self.dialog.sign_in.setEnabled(not signing_in and not connecting and not busy)
        self.dialog.sign_out.setEnabled(signed_in or signing_in)
        self.dialog.connect.setEnabled(signed_in and not signing_in and not connecting and not busy)

    def sign_in(self):
        self.connected = False
        self.plugin.sign_in()
        self.update_status()

    def sign_out(self):
        self.connected = False
        self.plugin.sign_out()
        self.update_status()

    def connect(self):
        self.connected = False
        self.plugin.connect_backend()
        self.update_status()

    def refresh_layers(self):
        """Called after Connect and after any explicit pending-edit upload handling."""
        result = refresh_connected_layers(self.plugin.settings.api_base_url)
        self.connected = True
        self.connection_note = (
            f"Connected. Refreshed {len(result.refreshed)} loaded layer(s) from the server."
        )
        if result.editing:
            self.connection_note += (
                " Kept local edits unchanged: " + ", ".join(result.editing) + "."
            )
        if result.failed:
            self.connection_note += " Some layers could not refresh; see the QGIS message bar."
            self.plugin._message("; ".join(result.failed), Qgis.MessageLevel.Warning)
        self.update_status()
        if self.dialog is not None and not result.failed:
            # Accept only after Connect and the layer refresh complete. The
            # dialog's finished signal also stops its status-update timer.
            self.dialog.accept()
        return result

    def close(self):
        self.closed = True
        for timer in (self.start_timer, self.timer):
            if timer is not None:
                timer.stop()
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None
        self.timer = None
        self.start_timer = None
