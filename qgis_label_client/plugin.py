"""The plugin object: ``initGui`` attaches, ``unload`` detaches, everything else wires.

THE ONLY CONTRACT QGIS IMPOSES

``initGui()`` and ``unload()``. QGIS cleans up nothing, so every attachment made in the
first must be undone by the second. The mechanical test is to reload five times with
Plugin Reloader and count toolbar buttons; five buttons means ``unload()`` is wrong.

That failure mode is prevented structurally here rather than by care: nothing is attached
without registering its detach on :class:`~.core.teardown.Teardown` in the same
statement, and ``unload()`` is one call plus a task shutdown. There is no list of removals
to keep in sync with a list of additions, because there is only one list.

THREADING

Network work goes through :class:`~.tasks.TaskRunner`. The ``_fetch_*`` methods below run
on a worker thread and touch nothing but the network client; the ``_on_*`` methods are
their main-thread completions and are the only place layers, widgets and ``iface`` are
allowed to appear.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from qgis.core import Qgis, QgsFeedback, QgsProject
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAction,
    QApplication,
    QDialog,
    QInputDialog,
    QMessageBox,
)

from . import auth, client, imagery, network, oauth_flow, qa
from . import layers as layer_tools
from . import publish as publish_tools
from .core import oauth, recorded
from .core.asof import AsOfMechanism, describe
from .core.collections import Collection
from .core.errors import LabelClientError
from .core.publish import PublishPlan, PublishReport
from .core.registry import ClassRegistry
from .core.teardown import Teardown
from .core.tracks import Track
from .core.tracks import resolve as resolve_track
from .dockwidget import LabelClientDock
from .historydialog import HistoryDialog
from .log import log, log_error, log_warning
from .publishdialog import PublishDialog, PublishReportDialog
from .settings import PluginSettings
from .tasks import TaskRunner
from .validtime import register_functions, unregister_functions

MENU_NAME = "&CVI Label Client"
PLUGIN_DIR = os.path.dirname(__file__)

#: How many recorded beliefs one history request asks for. A cap rather than paging
#: because the dialog is a read, not a browser -- but a silent cap would present a
#: truncated history as a complete one, so :meth:`LabelClientPlugin._on_history` says
#: when it has been hit.
HISTORY_LIMIT = 200


def _fetch_tracks_or_none(
    url: str, tracks_path: str, authcfg: str, feedback: QgsFeedback
) -> list[Track]:
    """Fetch the track list, treating a missing endpoint as "no tracks". Worker thread.

    A backend that predates history tracks has no ``/v1/tracks`` and answers 404. Letting
    that fail the whole Connect would make this plugin version unusable against it, over a
    feature the deployment does not have. Answering "no tracks" instead is the honest
    state: the panel shows an empty list, :meth:`LabelClientPlugin._require_track` refuses
    every write, and reads fall back to whatever the backend serves -- which, on a backend
    with no tracks, is everything, correctly.

    A *broken* response is not swallowed. :func:`~.core.tracks.parse_tracks` raises on a
    document that is not a track list, and that reaches the user, because a track list the
    plugin could not read must never look like a deployment with no tracks.
    """
    try:
        return client.fetch_tracks(url, tracks_path, authcfg, feedback)
    except LabelClientError as exc:
        if getattr(exc, "status", None) == 404:
            log_warning(
                f"No history-track endpoint at {tracks_path!r} (404). Treating this "
                "deployment as having no tracks; writes will be refused."
            )
            return []
        raise


class LabelClientPlugin:
    """Entry point QGIS instantiates once per load."""

    def __init__(self, iface) -> None:
        self.iface = iface
        self.settings = PluginSettings()
        self.tasks = TaskRunner()
        self.teardown = Teardown()

        self.dock: LabelClientDock | None = None
        self.registry: ClassRegistry | None = None
        self.collections: list[Collection] = []
        # Every history track the backend offers. Empty until Connect: the plugin has no
        # opinion about what tracks exist, exactly as it has none about what classes do.
        self.tracks: list[Track] = []
        # A publish in flight. Guarded rather than merely discouraged: the menu entry and
        # the panel button both reach it, and two concurrent runs would double the data
        # with nothing able to tell afterwards which copy is which.
        self.publishing = False

        # --- sign-in state ---------------------------------------------------
        #
        # A Google ID token lives about an hour and an editing session is longer, so the
        # plugin renews it. Three mechanisms, because no one of them covers every case:
        #
        #   the TIMER   renews at exp-5min and is the only one the analyst never notices;
        #   the LAZY    check runs before every request this plugin issues, and covers the
        #               laptop that slept through the timer;
        #   the 401     handler is the safety net, and it is a net rather than a fix: the
        #               request that failed was made by QGIS's own OAPIF provider, which
        #               this plugin cannot make retry. See _repair_after_401.
        self.refresh_timer: Any = None
        # The browser sign-in in flight. Held because a QObject with no Python reference is
        # collected mid-flow, and the flow is exactly one event loop turn long.
        self.signin: oauth_flow.GoogleSignIn | None = None
        self._refreshing = False
        # Actions parked behind a renewal, resumed once it lands. A list rather than one
        # slot because the panel and the menu can both fire while a renewal is in flight,
        # and dropping the second one silently is "the button does nothing".
        self._deferred: list[Callable[[], None]] = []
        # Set while a deferred action is being replayed, so an action that re-checks
        # freshness on the way in cannot defer itself a second time and loop.
        self._resuming = False
        # True when the renewal was provoked by a 401 rather than by the clock, which
        # changes the message: a repaired credential does not un-fail the request that
        # already failed, and the analyst has to reload the layer.
        self._repairing = False

    # ------------------------------------------------------------------ lifecycle

    def initGui(self) -> None:  # noqa: N802 - name fixed by the QGIS plugin contract
        icon = QIcon(os.path.join(PLUGIN_DIR, "icons", "cvi.svg"))

        # Before any layer is built: layers.build_label_layer installs a field default
        # that calls cvi_valid_from(), and QGIS will not evaluate a default naming a
        # function it does not know. Torn down through the same registry as everything
        # else -- an expression function still bound after a plugin reload is the classic
        # way a project becomes unopenable.
        register_functions()
        self.teardown.add("valid-time expression", unregister_functions)

        self.dock = LabelClientDock(self.iface.mainWindow())
        # Scoped enum: both spellings work on Qt5, only this one works on Qt6.
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.teardown.add("dock widget", self._detach_dock)

        self.panel_action = QAction(icon, "CVI Label Client panel", self.iface.mainWindow())
        self.panel_action.setCheckable(True)
        self.panel_action.setChecked(True)
        self.panel_action.toggled.connect(self._toggle_dock)
        self.iface.addToolBarIcon(self.panel_action)
        self.teardown.add("toolbar icon", lambda: self.iface.removeToolBarIcon(self.panel_action))
        self.iface.addPluginToMenu(MENU_NAME, self.panel_action)
        self.teardown.add(
            "menu: panel", lambda: self.iface.removePluginMenu(MENU_NAME, self.panel_action)
        )

        self.refresh_action = QAction(icon, "Refresh imagery URLs", self.iface.mainWindow())
        self.refresh_action.triggered.connect(self.refresh_imagery)
        self.iface.addPluginToMenu(MENU_NAME, self.refresh_action)
        self.teardown.add(
            "menu: refresh imagery",
            lambda: self.iface.removePluginMenu(MENU_NAME, self.refresh_action),
        )

        self.recorded_action = QAction(
            icon, "Historical view (transaction time)…", self.iface.mainWindow()
        )
        self.recorded_action.setToolTip(
            "Add a read-only layer showing the labels as the team believed them at a "
            "chosen instant, including labels deleted since."
        )
        self.recorded_action.triggered.connect(self.request_recorded_view)
        self.iface.addPluginToMenu(MENU_NAME, self.recorded_action)
        self.teardown.add(
            "menu: historical view",
            lambda: self.iface.removePluginMenu(MENU_NAME, self.recorded_action),
        )

        self.publish_action = QAction(icon, "Publish local layers…", self.iface.mainWindow())
        self.publish_action.setToolTip(
            "Send the vector layers open in this project to the backend as new labels."
        )
        self.publish_action.triggered.connect(self.publish_local_layers)
        self.iface.addPluginToMenu(MENU_NAME, self.publish_action)
        self.teardown.add(
            "menu: publish local layers",
            lambda: self.iface.removePluginMenu(MENU_NAME, self.publish_action),
        )

        self._connect_dock_signals()
        self._restore_settings()
        # Last, because it reads the settings this just restored. A profile whose token
        # expired while QGIS was closed arms a zero-delay timer and renews itself before
        # the analyst touches anything.
        self._arm_refresh_timer()
        log("Plugin loaded.")

    def unload(self) -> None:
        """Detach everything. QGIS cleans up nothing."""
        # Tasks first: a request that completes after the dock is gone would call into a
        # destroyed widget, which is a crash rather than a warning.
        self.tasks.shutdown()
        # Not on the teardown registry, because neither of these is an attachment to QGIS:
        # they are plugin-owned objects, the same category as the task runner above, and
        # the registry exists for things QGIS would otherwise keep after unload. The timer
        # is parented to the dock and dies with it; stopping it here means it cannot fire
        # once more between unload and that deletion.
        if self.refresh_timer is not None:
            self.refresh_timer.stop()
            self.refresh_timer = None
        self._abandon_sign_in()
        self._deferred.clear()
        for failure in self.teardown.run():
            log_error(f"Teardown step {failure.label!r} failed: {failure.error}")
        self.dock = None
        self.registry = None
        self.collections = []
        self.tracks = []
        self.publishing = False
        self._refreshing = False
        log("Plugin unloaded.")

    def _detach_dock(self) -> None:
        if self.dock is None:
            return
        self.iface.removeDockWidget(self.dock)
        # deleteLater rather than a bare drop: Qt owns the C++ object and the dock is
        # still parented to the main window until the event loop runs.
        self.dock.deleteLater()

    def _toggle_dock(self, visible: bool) -> None:
        if self.dock is not None:
            self.dock.setVisible(visible)

    def _connect_dock_signals(self) -> None:
        """Wire the view to the controller.

        No matching disconnects are registered: the connections die with the dock, which
        :meth:`_detach_dock` destroys. Registering them anyway would be teardown that
        runs against a deleted object.
        """
        if self.dock is None:
            return
        # Keep the toolbar toggle honest about the panel's actual state. Closing the dock
        # with its own X does not change the action, so without this the button stays
        # checked over a hidden panel and the next click unchecks it instead of
        # reopening it -- "the button does nothing", twice.
        self.dock.visibilityChanged.connect(self.panel_action.setChecked)

        self.dock.connectRequested.connect(self.connect_backend)
        self.dock.signInRequested.connect(self.sign_in)
        self.dock.signOutRequested.connect(self.sign_out)
        self.dock.copyAddressRequested.connect(self.copy_address)
        self.dock.loadLayersRequested.connect(self.load_collections)
        self.dock.refreshImageryRequested.connect(self.refresh_imagery)
        self.dock.asOfApplied.connect(self.apply_as_of)
        self.dock.recordedViewRequested.connect(self.open_recorded_view)
        self.dock.historyRequested.connect(self.show_history)
        self.dock.coverageRequested.connect(self.check_coverage)
        self.dock.publishRequested.connect(self.publish_local_layers)
        self.dock.trackChanged.connect(self.set_track)

    def _restore_settings(self) -> None:
        if self.dock is None:
            return
        self.dock.set_api_url(self.settings.api_base_url)
        self.dock.set_as_of(self.settings.as_of)
        self.dock.set_as_of_mechanism(self.settings.as_of_mechanism.value)
        # The picker's opening value only; the control stays disarmed. A remembered instant
        # is a convenience, not a claim that a historical layer is in play.
        self.dock.set_recorded_default(self.settings.recorded_at)
        self.dock.set_recorded_bounds()
        self._refresh_auth_label()
        self._refresh_axes()

    def _refresh_axes(self, moment: str = "") -> None:
        """Keep the two-axis line true, after anything that moves either axis.

        Both are named even when one of them is off, and that is load-bearing rather than
        tidy: each control on its own reads as "the" time control, and a person who has
        only met one of them will assume the other axis is not in play.
        """
        if self.dock is None:
            return
        self.dock.set_axes(
            recorded.describe_axes(
                moment or self.dock.recorded_at(),
                self.settings.as_of,
                self.settings.as_of_mechanism,
            )
        )

    # ------------------------------------------------------------------- helpers

    def _message(self, text: str, level: Qgis.MessageLevel = Qgis.MessageLevel.Info) -> None:
        self.iface.messageBar().pushMessage("CVI Label Client", text, level, 8)

    def _report(self, text: str) -> None:
        """Say a failure out loud, without trying to repair it.

        Split out of :meth:`_fail` for one caller: the renewal's own failure handler. A
        renewal that failed with a 401 routed through _fail would provoke another renewal,
        which would fail the same way -- a loop of background tasks with a red message bar
        per turn, from a single expired credential.
        """
        log_error(text)
        self._message(text, Qgis.MessageLevel.Critical)
        if self.dock is not None:
            self.dock.set_busy(False)
            self.dock.set_status(text)

    def _fail(self, text: str) -> None:
        self._report(text)
        self._repair_after_401(text)

    def _repair_after_401(self, text: str) -> None:
        """Renew a rejected credential, once, on the way past a failure.

        THE SAFETY NET, NOT THE MECHANISM. The timer and the pre-flight check are what
        keep the token fresh; this only catches the case they both missed -- a clock that
        jumped, a token revoked early, a renewal that failed and was retried by hand.

        It reads the status out of the message rather than out of an exception because
        ``QgsTask`` cannot carry an exception across the thread boundary: the runner
        formats it to a string on the worker thread, which is the only thing that survives
        (see :mod:`.tasks`). Matching on the rendered "HTTP 401" that
        :func:`.network._describe_status` writes is the seam that exists.

        Note what it deliberately does NOT do: replay the request. Most 401s here come
        from QGIS's own OAPIF provider, whose requests no plugin code is in the path of,
        so the honest repair is a fresh credential plus a sentence telling the analyst to
        reload the layer.
        """
        if "HTTP 401" not in text:
            return
        if self._refreshing or self._repairing:
            return
        if not self.settings.oauth_expires_at:
            # A hand-pasted token or no credential at all. There is nothing to renew, and
            # "renewing your sign-in" would be a lie on top of a failure.
            return
        self._repairing = True
        log_warning("A request was refused with 401; renewing the Google sign-in.")
        self._refresh_credential(quiet=True)

    def _refresh_auth_label(self) -> None:
        if self.dock is None:
            return
        stored = self.settings.authcfg_by_track
        try:
            summary = auth.summarise(self.settings.authcfg)
        except LabelClientError as exc:
            self.dock.set_auth_status(str(exc))
            return
        if summary is None:
            self.dock.set_auth_status("Not signed in. Anonymous requests.")
            return
        # How many tracks the credential covers, because a rotation that reached only some
        # of them leaves the rest 401ing in a way that reads as an outage.
        extra = len([name for name in stored if name])
        suffix = f" (+{extra} track-specific)" if extra else ""
        expires_at = self.settings.oauth_expires_at
        if expires_at:
            # Who and until when, first, because those are the two questions asked of this
            # label. The config id and method follow for a support conversation.
            headline = oauth.describe_session(self.settings.oauth_email, expires_at, time.time())
            self.dock.set_auth_status(f"{headline}. {summary.describe()}{suffix}")
            return
        self.dock.set_auth_status(summary.describe() + suffix)

    # ---------------------------------------------------------------- tracks

    def current_track(self) -> Track | None:
        """The history track this session is working in, or ``None``.

        ``None`` has two causes and they are deliberately not distinguished here, because
        the answer is the same either way -- do not write: either nothing has connected
        yet, or the stored setting names a track the backend does not offer. The second is
        resolved to ``None`` rather than to the deployment default on purpose: silently
        answering a request for one dataset from another is the contamination failure in
        reverse, and the annotator would conclude their track was empty.
        """
        return resolve_track(self.tracks, self.settings.track)

    def _track_name(self) -> str:
        """The track name to send as ``X-Track``, or ``""`` for "the deployment default".

        Empty is a legitimate value on a *read* -- it is what the API does with any request
        that names no track -- so this is not the same question as :meth:`current_track`
        returning ``None``. Writes go through :meth:`_require_track`, which is.
        """
        track = self.current_track()
        return track.name if track else ""

    def _require_track(self, action: str) -> Track | None:
        """The track, or ``None`` after saying why there is not one. For writes only."""
        track = self.current_track()
        if track is None:
            stored = self.settings.track
            if stored and self.tracks:
                self._fail(
                    f"Cannot {action}: this profile is set to history track {stored!r}, "
                    "which the backend does not offer. It may have been renamed, or this "
                    "credential may not be permitted to use it. Pick one from the panel."
                )
            else:
                self._fail(f"Cannot {action}: no history track is selected. Connect first.")
            return None
        if track.archived:
            self._fail(f"Cannot {action}: {track.warning()}")
            return None
        return track

    def set_track(self, name: str) -> None:
        """Switch the session to another history track, re-pointing every loaded layer.

        REFUSED WHILE ANY PLUGIN LAYER HAS UNSAVED EDITS, and that refusal is the whole
        reason this is a controller method rather than a setting write. Switching tracks
        re-points every layer, and ``setDataSource`` on a layer with a dirty edit buffer
        discards those edits with no prompt and no undo. Ten minutes of drawing would
        vanish because somebody changed a combo box.
        """
        if self.dock is None:
            return
        if name == self.settings.track:
            return
        dirty = layer_tools.dirty_layers()
        if dirty:
            self.dock.set_tracks(self.tracks, self.settings.track)
            self._message(
                "Save or discard your edits first: "
                + ", ".join(sorted(layer.name() for layer in dirty))
                + " have unsaved changes, and switching tracks re-points every layer, "
                "which would discard them without asking.",
                Qgis.MessageLevel.Warning,
            )
            return

        self.settings.set("track", name)
        track = self.current_track()
        repointed = 0
        for layer in layer_tools.plugin_layers():
            layer_tools.repoint_for(layer, self.settings, self.registry, track)
            repointed += 1
            self._warn_on_track_mismatch(layer, track)

        self._refresh_track_banner()
        # The floor is a property of the track, not of the deployment: a track created last
        # week cannot answer a question about last year, and the picker should say so
        # before somebody asks.
        self._refresh_recorded_bounds()
        self.dock.set_status(
            f"History track: {name or '(deployment default)'} - {repointed} layer(s) re-pointed."
        )
        log(f"Track switched to {name!r}; {repointed} layer(s) re-pointed.")

    def _refresh_recorded_bounds(self) -> None:
        """Point the historical picker at what the selected track can actually answer."""
        if self.dock is None:
            return
        track = self.current_track()
        self.dock.set_recorded_bounds(
            track.earliest_recorded if track else "", track.name if track else ""
        )

    def _refresh_track_banner(self) -> None:
        """Keep the panel's "you are here" line true. Called after anything that moves it."""
        if self.dock is None:
            return
        track = self.current_track()
        if track is None:
            stored = self.settings.track
            if stored:
                self.dock.set_track_banner(
                    f"<b>Track {stored!r} is not available.</b> Nothing will be written "
                    "until you pick a track this backend offers."
                )
            else:
                self.dock.set_track_banner(
                    "<b>No track selected.</b> Reads use the deployment default; writes "
                    "are refused until you choose one."
                )
            return
        warning = track.warning()
        strays = layer_tools.layers_on_other_tracks(track)
        stray_note = (
            "<br/><b>Loaded from another track:</b> "
            + ", ".join(
                sorted(f"{layer.name()} ({layer_tools.track_of(layer)})" for layer in strays)
            )
            + ". Those layers still talk to the track they were loaded from - the track is "
            "in each layer's own data source - so edits there land in that dataset."
            if strays
            else ""
        )
        self.dock.set_track_banner(
            f"<b>Working in: {track.describe()}</b>"
            + (f"<br/><b>{warning}</b>" if warning else "")
            + stray_note
        )

    def _warn_on_track_mismatch(self, layer, track: Track | None) -> None:
        """The cheap half of the canary: read one feature and compare.

        The filter in :func:`~.layers.apply_canaries` makes a propagation failure show
        up as an empty layer. This makes it show up as a sentence, which is the difference
        between "the backend is down" and "you are looking at the wrong dataset".
        """
        try:
            message = layer_tools.track_mismatch(layer, self.registry, track)
        except Exception as exc:  # noqa: BLE001 - a check must never break the thing it checks
            log_warning(f"Could not verify the track of {layer.name()!r}: {exc}")
            return
        if message:
            log_error(f"{layer.name()}: {message}")
            self._message(f"{layer.name()}: {message}", Qgis.MessageLevel.Critical)

    def _persist_url(self) -> str:
        """Save whatever is currently in the URL field and return it.

        Called before every network action rather than only on Connect: the toolbar and
        menu entries can fire without the panel focused, and reading a stale setting while
        a different URL sits in the field is a confusing five minutes.
        """
        if self.dock is not None:
            self.settings.set("api_base_url", self.dock.api_url())
        return self.settings.api_base_url

    # ---------------------------------------------------------------- connection

    def sign_in(self) -> None:
        """Sign in with Google in the system browser, and store the resulting ID token.

        WHY A BROWSER AND NOT A PASSWORD BOX

        The backend authenticates people by verifying a Google ID token: a signed
        assertion about a verified address, which nothing on this side can forge and which
        this plugin therefore never has to hold a password to obtain. The credential that
        reaches ``QgsAuthManager`` is the same shape as before -- ``Authorization: Bearer
        <token>`` plus ``X-Track`` in an ``APIHeader`` config -- so every downstream
        mechanism, including the one channel that reaches QGIS's own OAPIF requests, is
        untouched. Only where the token comes from has changed.

        The master password is asked for BEFORE the browser opens. Doing it afterwards
        would mean an analyst who dismisses the prompt has consented at Google, has a live
        refresh token in memory, and nowhere to put it.
        """
        if self.signin is not None:
            self._message(
                "A sign-in is already waiting for the browser. Finish it there, or wait "
                f"{oauth.CALLBACK_TIMEOUT_SECONDS} seconds for it to time out.",
                Qgis.MessageLevel.Warning,
            )
            return
        try:
            if not auth.master_password_ready():
                self._fail(
                    "The QGIS authentication database was not unlocked, so there is "
                    "nowhere to put the sign-in. Nothing was sent to Google."
                )
                return
            flow = oauth_flow.GoogleSignIn(self.dock)
        except LabelClientError as exc:
            self._fail(str(exc))
            return

        flow.codeReceived.connect(self._exchange_code)
        flow.failed.connect(self._on_sign_in_failed)
        self.signin = flow
        if self.dock is not None:
            self.dock.set_auth_status("Waiting for the browser to finish signing in…")
        try:
            # The previously signed-in address is offered as a hint, so somebody with a
            # personal and a work Google account in the same browser is not silently
            # shown the wrong one -- which produces a token the server correctly refuses,
            # reported as "you do not have access" to a person who does.
            flow.start(login_hint=self.settings.oauth_email)
        except LabelClientError as exc:
            self._abandon_sign_in()
            self._fail(str(exc))
            return
        self._message(
            "Finish signing in with Google in your browser, then return to QGIS.",
            Qgis.MessageLevel.Info,
        )

    def copy_address(self) -> None:
        """Put the signed-in address on the clipboard.

        Exists because of one specific message. When the backend answers "you
        authenticated fine and are not on the access list", the next action is pasting
        that address to an administrator -- and an address retyped from a panel is an
        address that gets a grant written for somebody who does not exist.
        """
        address = self.settings.oauth_email
        if not address:
            self._message(
                "No address to copy - sign in with Google first.", Qgis.MessageLevel.Warning
            )
            return
        clipboard = QApplication.clipboard()
        if clipboard is None:  # pragma: no cover - headless QGIS only
            self._message("No clipboard is available.", Qgis.MessageLevel.Warning)
            return
        clipboard.setText(address)
        self._message(f"Copied {address} to the clipboard.")

    def _abandon_sign_in(self) -> None:
        """Tear the browser flow down. Safe to call when there is none."""
        flow, self.signin = self.signin, None
        if flow is not None:
            flow.abort()
            flow.deleteLater()

    def _exchange_code(self, code: str, verifier: str, redirect: str) -> None:
        """Trade the authorization code for tokens. Main thread; the POST is not.

        Deliberately not done inside :mod:`.oauth_flow`: it is a network request, every
        network request in this plugin runs on a worker thread, and a class that both
        listens on a socket and talks to Google is one with two reasons to change.
        """
        self._abandon_sign_in()
        if self.dock is not None:
            self.dock.set_auth_status("Completing sign-in…")

        def work(feedback: QgsFeedback) -> oauth.Credential:
            payload = network.post_form(
                oauth.TOKEN_ENDPOINT,
                oauth.token_request(code, verifier, redirect),
                feedback=feedback,
            )
            return oauth.credential_from_token_response(payload, time.time())

        self.tasks.run(
            "Complete Google sign-in", work, self._store_credential, self._on_sign_in_failed
        )

    def _store_credential(self, credential: oauth.Credential) -> None:
        """Write the new ID token into every auth config, reusing every id."""
        try:
            # One config per KNOWN track, plus one naming none. On the first sign-in of a
            # fresh profile the track list is empty -- you need a credential to discover
            # it -- so only the un-tracked entry is written, and a later sign-in fans it
            # out. Passing the existing map is what makes every id be REUSED, so saved
            # .qgz projects and already-loaded layers survive the rotation.
            stored = auth.store_id_token_for_tracks(
                credential.id_token,
                [track.name for track in self.tracks],
                self.settings.authcfg_by_track,
            )
            renewable = auth.store_refresh_token(credential.refresh_token)
        except LabelClientError as exc:
            self._fail(str(exc))
            return

        self.settings.set_authcfg_by_track(stored)
        self.settings.set_oauth_session(credential.email, credential.expires_at)
        self._refresh_auth_label()
        self._arm_refresh_timer()

        covered = len([name for name in stored if name])
        note = f" Covers {covered} track(s)." if covered else ""
        who = credential.email or "your Google account"
        self._message(f"Signed in as {who}.{note}", Qgis.MessageLevel.Success)
        if not renewable:
            # Said out loud rather than discovered in an hour. Without a stored refresh
            # token the session simply dies mid-afternoon, and every layer starts 401ing
            # with no obvious cause.
            self._message(
                "This QGIS build could not store the renewal token, so this sign-in will "
                "expire in about an hour and you will have to sign in again.",
                Qgis.MessageLevel.Warning,
            )
        # Deferred work waits on a *renewal*, not on a first sign-in, but replaying it here
        # too costs nothing and covers the analyst who was told to sign in again.
        self._run_deferred()

    def _on_sign_in_failed(self, message: str) -> None:
        self._abandon_sign_in()
        self._deferred.clear()
        if message.startswith(oauth.SignInCancelledError.__name__):
            # Closing the consent tab is a decision, not a fault, and reporting it in red
            # trains people to ignore red.
            self._message("Sign-in was cancelled. Nothing was changed.", Qgis.MessageLevel.Warning)
            self._refresh_auth_label()
            return
        self._fail(message)
        self._refresh_auth_label()

    # ------------------------------------------------------------- staying signed in

    def _credential_needs_refresh(self) -> bool:
        """True when the stored ID token is about to die, or already has."""
        return oauth.needs_refresh(self.settings.oauth_expires_at, time.time())

    def _defer_until_fresh(self, resume: Callable[[], None]) -> bool:
        """Park `resume` behind a renewal if one is due. True when it was parked.

        THE LAZY HALF OF THE EXPIRY STORY, and the half that covers the case the timer
        cannot: a laptop suspended over lunch wakes with a dead token and a timer that
        fired late or not at all. Checking here -- on the way into anything that will put
        a credential on the wire -- means the repair happens before the 401 rather than
        after it, which matters because a 401 from QGIS's own provider cannot be retried.

        Callers pass a bound call to themselves, so a renewal simply re-runs the action
        the analyst asked for. :attr:`_resuming` stops that re-run from deferring itself
        again; without it a renewal that somehow did not move the expiry would loop.
        """
        if self._resuming or not self._credential_needs_refresh():
            return False
        return self._refresh_credential(resume=resume)

    def _refresh_credential(
        self, resume: Callable[[], None] | None = None, quiet: bool = False
    ) -> bool:
        """Renew the ID token from the stored refresh token. True when it took the action.

        `quiet` is for the clock-driven paths -- the startup timer and the 401 net -- where
        a locked authentication database is a state to report in the panel, not a red
        message bar over a QGIS window the analyst has only just opened. When an action is
        waiting behind the renewal, the same condition is a hard failure and says so.
        """
        if resume is not None:
            self._deferred.append(resume)
        if self._refreshing:
            return True

        def refuse(message: str) -> bool:
            self._deferred.clear()
            if quiet:
                log_warning(message)
                if self.dock is not None:
                    self.dock.set_auth_status(message)
            else:
                self._fail(message)
            self._repairing = False
            return True

        try:
            if not auth.master_password_ready(prompt=not quiet):
                # Rewriting an auth config needs qgis-auth.db unlocked THIS session. An
                # analyst who dismissed the prompt has a session that cannot renew itself,
                # and the generic failure that produces reads as a backend outage.
                return refuse(
                    "Unlock the QGIS authentication database to stay signed in "
                    "(Settings > Options > Authentication). Storing the master password "
                    "in your operating system's keychain stops this recurring."
                )
            refresh_token = auth.read_refresh_token()
        except LabelClientError as exc:
            return refuse(str(exc))

        if not refresh_token:
            return refuse(
                "Your Google sign-in has expired and there is no stored renewal token, so "
                "it cannot be renewed silently. Click Sign in with Google."
            )

        self._refreshing = True

        def work(feedback: QgsFeedback) -> oauth.Credential:
            payload = network.post_form(
                oauth.TOKEN_ENDPOINT, oauth.refresh_request(refresh_token), feedback=feedback
            )
            # A refresh reply carries no refresh_token of its own; the one we sent stays
            # valid and is threaded through so the credential is complete either way.
            return oauth.credential_from_token_response(
                payload, time.time(), refresh_token=refresh_token
            )

        self.tasks.run("Renew Google sign-in", work, self._on_refreshed, self._on_refresh_failed)
        return True

    def _on_refreshed(self, credential: oauth.Credential) -> None:
        """Rewrite every auth config under its existing id, then resume what was waiting.

        REWRITING RATHER THAN REPLACING IS THE WHOLE POINT. The authcfg id is written into
        every layer's data source and into any saved project, so a renewal that minted new
        ids would leave every open layer pointing at a credential that no longer exists --
        an hourly self-inflicted outage. :func:`.auth.store_id_token` reuses the id and
        clears QGIS's cached copy of it, so the provider's next request carries the new
        token.
        """
        self._refreshing = False
        try:
            stored = auth.store_id_token_for_tracks(
                credential.id_token,
                [track.name for track in self.tracks],
                self.settings.authcfg_by_track,
            )
            if credential.refresh_token:
                # Google rotates refresh tokens in some configurations. Storing whatever
                # came back keeps the next renewal working; storing nothing would make the
                # session die at the first rotation, a week or a month later.
                auth.store_refresh_token(credential.refresh_token)
        except LabelClientError as exc:
            self._deferred.clear()
            self._repairing = False
            self._fail(f"Could not store the renewed sign-in: {exc}")
            return

        self.settings.set_authcfg_by_track(stored)
        self.settings.set_oauth_session(
            credential.email or self.settings.oauth_email, credential.expires_at
        )
        self._refresh_auth_label()
        self._arm_refresh_timer()
        log("Renewed the Google sign-in.")

        if self._repairing:
            self._repairing = False
            # The honest gap, stated in words. QGIS's OAPIF provider made the request that
            # failed and this plugin is not in its path, so a fresh credential cannot
            # un-fail it. Saying "signed in again" and stopping there leaves the analyst
            # looking at an empty layer that will stay empty.
            self._message(
                "Your sign-in was renewed. Reload the layer (right-click the layer > "
                "Reload) to see your data again.",
                Qgis.MessageLevel.Warning,
            )
        self._run_deferred()

    def _on_refresh_failed(self, message: str) -> None:
        self._refreshing = False
        self._repairing = False
        self._deferred.clear()
        # SignInExpired means the refresh token is dead -- revoked, or expired because the
        # OAuth consent screen is still in testing mode. Retrying cannot fix it and saying
        # "the backend failed" sends the analyst to the wrong place entirely.
        if message.startswith(oauth.SignInExpiredError.__name__):
            log_warning(message)
            if self.dock is not None:
                self.dock.set_auth_status(
                    "Sign-in expired. Click Sign in with Google to sign in again."
                )
            self._message(message.partition(": ")[2] or message, Qgis.MessageLevel.Critical)
            return
        # _report, not _fail: see its docstring. A renewal refused with 401 must not
        # provoke another renewal.
        self._report(f"Could not renew the Google sign-in: {message}")

    def _run_deferred(self) -> None:
        """Replay the actions that were waiting on a fresh credential."""
        pending, self._deferred = self._deferred, []
        self._resuming = True
        try:
            for action in pending:
                action()
        finally:
            self._resuming = False

    def _arm_refresh_timer(self) -> None:
        """Schedule the silent renewal for five minutes before the token dies.

        Single-shot and re-armed after each renewal rather than a repeating interval,
        because the interval is not constant: it is derived from the token that was
        actually issued, and a repeating timer would drift away from it.

        A delay of zero is a legitimate outcome and fires on the next event loop turn --
        which is exactly what should happen when QGIS starts with a token that expired
        overnight.
        """
        if self.dock is None:
            return
        if self.refresh_timer is None:
            # Parented to the dock so Qt destroys it with the panel; see unload().
            self.refresh_timer = QTimer(self.dock)
            self.refresh_timer.setSingleShot(True)
            self.refresh_timer.timeout.connect(self._on_refresh_timer)
        self.refresh_timer.stop()
        expires_at = self.settings.oauth_expires_at
        if not expires_at:
            # No Google session: nothing to renew, and an armed timer would nag about a
            # credential this plugin did not issue.
            return
        delay = oauth.seconds_until_refresh(expires_at, time.time())
        self.refresh_timer.start(int(delay * 1000))

    def _on_refresh_timer(self) -> None:
        self._refresh_credential(quiet=True)

    def sign_out(self) -> None:
        """Remove every credential, and revoke the grant at Google.

        Local removal alone would leave a live, long-lived refresh token on the analyst's
        Google account after the panel said the sign-in was gone. "Signed out" has to be
        true on both sides or it is not a statement about anything.
        """
        stored = self.settings.authcfg_by_track
        refresh_token = ""
        try:
            if auth.master_password_ready(prompt=False):
                # Read BEFORE the configs go, and only if the database is already unlocked:
                # revocation is a courtesy and must never provoke a master-password prompt
                # in the middle of signing out.
                refresh_token = auth.read_refresh_token()
        except LabelClientError as exc:
            log_warning(str(exc))

        removed = 0
        if stored:
            try:
                # Every entry, not just the current track's. One credential left behind is
                # a token still on disk after the analyst was told it was gone.
                removed = auth.remove_all(stored)
            except LabelClientError as exc:
                log_warning(str(exc))
        self.settings.set_authcfg_by_track({})
        self.settings.clear_oauth_session()
        self._deferred.clear()
        self._arm_refresh_timer()
        self._refresh_auth_label()
        self._message(f"Signed out. {removed} credential(s) removed.")

        if refresh_token:
            self._revoke(refresh_token)

    def _revoke(self, refresh_token: str) -> None:
        """Tell Google the refresh token is dead. Best effort, and it says so on failure.

        Failure is logged rather than raised: the local credentials are already gone, so
        the analyst IS signed out of this plugin, and a red bar claiming otherwise would be
        wrong in the direction that matters. What the log line buys is somebody being able
        to find out later that the grant is still live in the Google account settings.
        """

        def work(feedback: QgsFeedback) -> None:
            network.post_form(
                oauth.REVOCATION_ENDPOINT,
                oauth.revocation_request(refresh_token),
                feedback=feedback,
            )

        def revoked(_result: Any) -> None:
            log("Revoked the Google sign-in at Google.")

        def failed(message: str) -> None:
            log_warning(
                "Signed out locally, but Google did not confirm the sign-in was revoked "
                f"({message}). Remove this plugin's access from your Google account "
                "settings if that matters to you."
            )

        self.tasks.run("Revoke Google sign-in", work, revoked, failed)

    def connect_backend(self) -> None:
        """Fetch the collection list and the class registry."""
        if self.dock is None:
            return
        # Every path that will put a credential on the wire asks first. See
        # _defer_until_fresh: the renewal re-runs this method once it lands.
        if self._defer_until_fresh(self.connect_backend):
            return
        url = self._persist_url()
        if not url:
            self._fail("Enter the API URL first.")
            return
        authcfg = self.settings.authcfg
        registry_path = str(self.settings.get("class_registry_path"))
        tracks_path = str(self.settings.get("tracks_path"))
        track = self._track_name()

        self.dock.set_busy(True)
        self.dock.set_status("Connecting…")

        def work(feedback: QgsFeedback) -> dict[str, Any]:
            # Worker thread. Nothing here may touch widgets, iface or QgsProject.
            #
            # Tracks first, and NOT fatal if the endpoint is missing. A backend that
            # predates history tracks answers 404 here, and refusing to connect over it
            # would make this plugin version unusable against it -- so the panel says
            # there are no tracks and every write is refused, which is the honest state
            # rather than a broken one.
            return {
                "tracks": _fetch_tracks_or_none(url, tracks_path, authcfg, feedback),
                "collections": client.fetch_collections(url, authcfg, feedback, track=track),
                "registry": client.fetch_registry(
                    url, registry_path, authcfg, feedback, track=track
                ),
            }

        self.tasks.run("Connect to labeling API", work, self._on_connected, self._fail)

    def _on_connected(self, result: dict[str, Any]) -> None:
        if self.dock is None:
            return
        self.collections = result["collections"]
        self.registry = result["registry"]
        self.tracks = result["tracks"] or []

        # If the stored track has gone -- renamed, archived away, or this credential is no
        # longer permitted to use it -- the setting is NOT rewritten to the default. The
        # panel shows nothing selected and every write is refused until a person chooses,
        # because quietly moving somebody to another dataset is the failure this whole
        # feature exists to prevent.
        self.dock.set_tracks(self.tracks, self.settings.track)
        loaded = {layer_tools.collection_of(layer) for layer in layer_tools.plugin_layers()}
        self.dock.set_collections(self.collections, checked=loaded)
        self.dock.set_registry(self.registry)
        self.dock.set_connected(True)
        self.dock.set_busy(False)
        self._refresh_track_banner()
        self._refresh_recorded_bounds()
        track = self.current_track()
        where = f" Track: {track.name}." if track else " No track selected."
        self.dock.set_status(
            f"Connected. {len(self.collections)} collection(s), "
            f"{len(self.registry)} class(es) in the registry, "
            f"{len(self.tracks)} history track(s).{where}"
        )
        log(
            f"Connected: {len(self.collections)} collections, "
            f"{len(self.registry)} classes from {self.registry.source_url}, "
            f"{len(self.tracks)} tracks"
        )

    # -------------------------------------------------------------------- layers

    def load_collections(self, collection_ids: Sequence[str]) -> None:
        """Add a layer per checked collection and configure it from the registry.

        Layer creation is on the main thread on purpose. Building a ``QgsVectorLayer``
        does its own network round trip, but a layer cannot be constructed on a worker
        thread and handed to ``QgsProject``, so this is the same trade QGIS's own Browser
        panel makes.
        """
        if self.dock is None or not collection_ids:
            return
        if not self.registry:
            self._fail("Connect first: the class registry drives layer configuration.")
            return
        # The most important of these gates. Each layer built below hands an authcfg to
        # QGIS's own OAPIF provider, which then fetches with no plugin code in its path --
        # so a token that dies here produces a layer that 401s and cannot be retried.
        if self._defer_until_fresh(lambda: self.load_collections(collection_ids)):
            return

        titles = {c.collection_id: c.display_name for c in self.collections}
        project = QgsProject.instance()
        # Historical layers deliberately do not count as "already loaded". The whole use
        # case is the live layer and one or more past-belief layers open together, and a
        # historical layer occupying its collection's slot would make the live one
        # unloadable -- silently, by a `continue`.
        existing = {layer_tools.collection_of(layer): layer for layer in layer_tools.live_layers()}
        # May be None, and that is allowed for a READ: the API answers a request naming no
        # track from the deployment default, which is the right thing for somebody looking
        # around. Writes are the ones that refuse -- see _require_track.
        track = self.current_track()

        added = 0
        self.dock.set_busy(True)
        try:
            for collection_id in collection_ids:
                if collection_id in existing:
                    continue
                try:
                    layer = layer_tools.create_layer(
                        self.settings,
                        collection_id,
                        titles.get(collection_id, collection_id),
                        self.registry,
                        track,
                    )
                except LabelClientError as exc:
                    log_warning(str(exc))
                    self._message(str(exc), Qgis.MessageLevel.Warning)
                    continue
                if self._refuse_unpinned_historical(layer, collection_id):
                    continue
                layer_tools.apply_registry(layer, self.registry)
                project.addMapLayer(layer)
                added += 1
                self._warn_on_track_mismatch(layer, track)
        finally:
            self.dock.set_busy(False)

        self._refresh_track_banner()
        where = f" on track {track.name}" if track else ""
        self.dock.set_status(f"Loaded {added} layer(s){where}.")

    def _refuse_unpinned_historical(self, layer, collection_id: str) -> bool:
        """Refuse a transaction-time collection checked in the collection list.

        The failure this closes is the one this codebase keeps ruling against. That
        collection's view resolves at ``now()`` when no instant reaches it, so a layer built
        from the collection list would load, be full of features, be titled after a
        past-belief view, and show **the present**. Populated and wrong.

        Recognised by shape rather than by id -- the echo column, whose name comes from the
        registry -- because collection ids are a deployment's choice. The id is remembered
        on the way past, so the panel's own control does not have to ask for it later.
        """
        if not self.registry:
            return False
        names = [field.name() for field in layer.fields()]
        if not recorded.exposes_recorded_axis(names, self.registry.fields):
            return False
        self.settings.set("recorded_collection", collection_id)
        self._message(recorded.unpinned_warning(collection_id), Qgis.MessageLevel.Warning)
        log_warning(recorded.unpinned_warning(collection_id))
        return True

    def apply_as_of(self) -> None:
        """Re-point every plugin layer at the chosen valid-time instant.

        Historical layers included, and that is correct rather than an oversight: "what we
        believed in January about what was true in March" is exactly the bitemporal query
        the backend answers, and re-pointing carries each layer's own instant through --
        see :func:`~.layers.repoint_for`, which reads it off the layer rather than being
        told.
        """
        if self.dock is None:
            return
        as_of = self.dock.as_of()
        mechanism = AsOfMechanism.parse(self.dock.as_of_mechanism())
        self.settings.set_as_of(as_of)
        self.settings.set("as_of_mechanism", mechanism.value)

        track = self.current_track()
        targets = layer_tools.plugin_layers()
        for layer in targets:
            # Carries the track clause and the transaction-time instant through with it:
            # repoint_layer rebuilds the provider, so an as-of change would otherwise
            # silently drop the canary -- or turn a historical layer into a live one.
            layer_tools.repoint_for(layer, self.settings, self.registry, track)

        summary = describe(as_of, mechanism)
        self.dock.set_status(f"{summary} - {len(targets)} layer(s) updated.")
        self._refresh_axes()
        log(summary)

    # --------------------------------------------------- transaction time

    def request_recorded_view(self) -> None:
        """The menu entry. One instant source, which is the panel's picker.

        Deliberately not a second date dialog. Two pickers for one axis is two places for
        the remembered default to live and two chances for them to disagree about what UTC
        means; and the panel is where the instant, the floor and the two-axis status line
        already are.
        """
        if self.dock is None:
            return
        self.dock.setVisible(True)
        moment = self.dock.recorded_at()
        if not moment:
            self._message(
                "Tick 'Pin a historical layer to an instant' in the panel's Historical "
                "view box, choose the instant, then press Add historical layer.",
                Qgis.MessageLevel.Info,
            )
            return
        self.open_recorded_view(moment)

    def open_recorded_view(self, moment: str) -> None:
        """Add a read-only layer showing what the team believed at `moment`.

        DISTINCT FROM :meth:`apply_as_of`, which re-points the layers already loaded on the
        other axis. This *adds*, because the point of a historical view is comparing it
        against the live one -- and against another historical one at a different instant,
        which works because the instant lives in each layer's own data source rather than
        in a session setting.
        """
        if self.dock is None:
            return
        if not self.registry:
            self._fail("Connect first: the class registry drives layer configuration.")
            return
        if self._defer_until_fresh(lambda: self.open_recorded_view(moment)):
            return
        parsed = recorded.parse_instant(moment)
        if parsed is None:
            self._fail(
                f"{moment!r} is not an instant this plugin can send. Expected "
                "YYYY-MM-DDTHH:MM:SSZ - pick one from the panel rather than typing it."
            )
            return
        try:
            # The picker's ceiling already stops this, but the panel is a view and the
            # controller must not depend on a widget constraint: the bounds are set at
            # Connect, and a Connect that failed leaves them unset.
            recorded.validate(parsed)
        except LabelClientError as exc:
            self._fail(str(exc))
            return

        collection_id = self._collection_setting(
            "recorded_collection",
            "Historical view collection",
            "Which collection serves the world as it was BELIEVED at a past instant?",
        )
        if not collection_id:
            self._fail("No collection chosen. No historical layer was added.")
            return

        # None is allowed, exactly as it is for any other read: the API answers a request
        # naming no track from the deployment default. Nothing here writes.
        track = self.current_track()
        title = next(
            (c.display_name for c in self.collections if c.collection_id == collection_id), ""
        )
        name = recorded.layer_name(moment, recorded.base_name(title, collection_id))

        self.dock.set_busy(True)
        try:
            layer = layer_tools.create_layer(
                self.settings, collection_id, name, self.registry, track, recorded_at=moment
            )
        except LabelClientError as exc:
            self._fail(str(exc))
            return
        finally:
            self.dock.set_busy(False)

        layer_tools.apply_registry(layer, self.registry, historical=True)
        QgsProject.instance().addMapLayer(layer)
        self.settings.set_recorded_at(moment)
        self._warn_on_track_mismatch(layer, track)
        self._warn_if_writable(layer)
        self._report_recorded_view(layer, moment, track)

    def _warn_if_writable(self, layer) -> None:
        """Say so if the server let a pinned request look editable.

        :func:`~.layers.mark_read_only` has already made the layer safe on this side, so
        this is not a fix -- it is the observation that a *different* client, one that knows
        nothing about any of this, would have been allowed to edit a past belief. That is a
        server-side misconfiguration and it is worth a loud line rather than a quiet
        workaround.
        """
        if not layer_tools.provider_advertises_writes(layer):
            return
        message = (
            f"{layer.name()}: the server advertised this historical layer as WRITABLE. "
            "The plugin has forced it read-only, but the editability probe should have "
            "come back without the write verbs for a request naming an instant. Report "
            "this: another client would be allowed to edit a past belief."
        )
        log_error(message)
        self._message(message, Qgis.MessageLevel.Critical)

    def _report_recorded_view(self, layer, moment: str, track: Track | None) -> None:
        """Say what was added, and -- when it is empty -- which kind of empty it is."""
        if self.dock is None:
            return
        try:
            count = int(layer.featureCount())
        except (TypeError, ValueError):
            count = -1
        track_name = track.name if track else ""
        if count == 0:
            note = recorded.empty_view_message(
                moment, track_name, track.earliest_recorded if track else ""
            )
            self._message(note, Qgis.MessageLevel.Warning)
            log_warning(note)
        else:
            self._message(recorded.read_only_reason(moment), Qgis.MessageLevel.Info)
        counted = f"{count} feature(s)" if count >= 0 else "an unknown number of features"
        self.dock.set_status(f"Added {layer.name()} - {counted}.")
        self._refresh_axes(moment)
        log(f"Historical layer added at {moment} on track {track_name or '(default)'}.")

    # ------------------------------------------------------------------- imagery

    def refresh_imagery(self) -> None:
        """Mint fresh signed URLs and swap them into the raster layers."""
        url = self._persist_url()
        if not url:
            self._fail("Enter the API URL first.")
            return
        if self._defer_until_fresh(self.refresh_imagery):
            return
        authcfg = self.settings.authcfg
        path = str(self.settings.get("signed_urls_path"))
        # Imagery is shared between tracks by design -- one GeoTIFF serves both, because a
        # capture is a fact about the world and a track is a body of belief about it. The
        # track is sent for the edge's audit line, not to scope anything.
        track = self._track_name()
        if self.dock is not None:
            self.dock.set_busy(True)
            self.dock.set_imagery_status("Requesting signed URLs…")

        def work(feedback: QgsFeedback):
            return client.fetch_signed_assets(url, path, authcfg, feedback, track=track)

        self.tasks.run("Refresh imagery URLs", work, self._on_signed_urls, self._fail)

    def _on_signed_urls(self, result) -> None:
        assets, expires_at = result
        _, unmatched, applied = imagery.refresh_sources(assets)
        if self.dock is None:
            return
        self.dock.set_busy(False)
        expiry = f" Valid until {expires_at.isoformat()}." if expires_at else ""
        note = (
            f" {len(unmatched)} raster layer(s) matched nothing - see the log." if unmatched else ""
        )
        self.dock.set_imagery_status(
            f"{applied} layer(s) re-pointed from {len(assets)} signed URL(s).{expiry}{note}"
        )
        self._warn_if_expiring(expires_at)

    def _warn_if_expiring(self, expires_at) -> None:
        """Say so when the freshly minted URLs are already close to death.

        Worth saying out loud rather than only printing the timestamp: an expired signed
        URL does not blank the map. GDAL keeps serving what it has cached, so the raster
        looks right for a while and then starts failing mid-pan, which reads as a QGIS
        problem rather than as "refresh the imagery".
        """
        if expires_at is None:
            return
        minutes = int(self.settings.get("expiry_warning_minutes"))
        if minutes <= 0:
            return
        remaining = (expires_at - datetime.now(timezone.utc)).total_seconds() / 60
        if remaining <= minutes:
            self._message(
                f"These signed imagery URLs expire in {int(remaining)} minute(s). "
                "Refresh again before they do; an expired URL fails silently from "
                "GDAL's cache rather than blanking the layer.",
                Qgis.MessageLevel.Warning,
            )

    # ------------------------------------------------------------------------ QA

    def show_history(self) -> None:
        """Show every recorded belief about the selected label."""
        if not self.registry:
            self._fail("Connect first.")
            return
        if self._defer_until_fresh(self.show_history):
            return
        layer = layer_tools.find_label_layer(self.registry)
        if layer is None:
            self._fail("No label layer loaded.")
            return
        selected = list(layer.getSelectedFeatures())
        if len(selected) != 1:
            self._message(
                "Select exactly one label first. History is per label, keyed on its "
                "immutable label_id.",
                Qgis.MessageLevel.Warning,
            )
            return

        field = self.registry.fields.label_id
        index = layer.fields().indexOf(field)
        label_id = selected[0].attribute(index) if index >= 0 else None
        if not label_id:
            self._fail(
                f"The selected feature has no {field}. History needs the server-assigned "
                "identity; a feature that has never been saved does not have one yet."
            )
            return

        collection = str(self.settings.get("history_collection")).strip()
        if not collection:
            collection = self._ask_history_collection()
            if not collection:
                return
            self.settings.set("history_collection", collection)

        url = self.settings.api_base_url
        authcfg = self.settings.authcfg
        track = self._track_name()

        def work(feedback: QgsFeedback):
            return client.fetch_history(
                url,
                collection,
                str(label_id),
                authcfg,
                field,
                limit=HISTORY_LIMIT,
                feedback=feedback,
                track=track,
            )

        self.tasks.run(
            "Fetch label history",
            work,
            lambda entries: self._on_history(str(label_id), entries, track),
            self._fail,
        )

    def _ask_history_collection(self) -> str:
        return self._ask_collection(
            "History collection", "Which collection serves the label audit trail?"
        )

    def _ask_collection(self, title: str, question: str) -> str:
        """Ask which collection serves a purpose, rather than assuming a name.

        Collection ids are a deployment's choice. Guessing one and failing produces a
        404 that reads like an outage; asking once and remembering costs a dialog.
        """
        choices = [c.collection_id for c in self.collections] or [""]
        value, accepted = QInputDialog.getItem(
            self.iface.mainWindow(), title, question, choices, 0, False
        )
        return value if accepted else ""

    def _collection_setting(self, key: str, title: str, question: str) -> str:
        """A remembered collection id, asked for once."""
        stored = str(self.settings.get(key)).strip()
        if stored:
            return stored
        chosen = self._ask_collection(title, question).strip()
        if chosen:
            self.settings.set(key, chosen)
        return chosen

    def _on_history(self, label_id: str, entries, track: str = "") -> None:
        if self.dock is None:
            return
        self.dock.set_busy(False)
        if not entries:
            # Naming the track matters most on the empty answer. label_history is scoped to
            # one track by row-level security, so "no history" from the wrong track and
            # "no history" from the right one are the same sentence and opposite facts.
            where = f" on track {track}" if track else ""
            self._message(f"No history recorded for {label_id}{where}.")
            return
        if len(entries) >= HISTORY_LIMIT:
            # An audit trail silently cut off at the limit reads as the whole story.
            self._message(
                f"Showing the {HISTORY_LIMIT} most recent recorded beliefs for this "
                "label. There may be older ones.",
                Qgis.MessageLevel.Warning,
            )
        HistoryDialog(label_id, entries, self.iface.mainWindow(), track=track).exec()

    def check_coverage(self) -> None:
        """Flag labels sitting outside any exhaustive survey extent for their class."""
        if self.dock is None:
            return
        if not self.registry:
            self._fail("Connect first.")
            return
        label_layer = layer_tools.find_label_layer(self.registry)
        extent_layer = layer_tools.find_extent_layer(self.registry)
        try:
            report, coverage_by_fid = qa.check_coverage(label_layer, extent_layer, self.registry)
        except LabelClientError as exc:
            self._fail(str(exc))
            return

        # Select the problem features so the finding is on the canvas, not in a dialog.
        flagged = [
            fid
            for fid, coverage in coverage_by_fid.items()
            if coverage in ("unsurveyed", "partial")
        ]
        label_layer.selectByIds(flagged)

        # The track is named on the coverage result for the same reason it is named on an
        # empty history: "all labels are inside an exhaustive extent" is a different fact
        # about a test dataset than about the analysts' one, and the sentence alone cannot
        # tell them apart.
        track = self.current_track()
        summary = report.summary()
        if track is not None:
            summary = f"[{track.name}] {summary}"
        self.dock.set_qa_result(summary)
        level = Qgis.MessageLevel.Success if report.clean else Qgis.MessageLevel.Warning
        self._message(summary, level)
        log(summary)

        if report.classes_without_extents:
            QMessageBox.information(
                self.iface.mainWindow(),
                "No survey extent declared",
                "These classes have labels here but no exhaustive labeled_extent among "
                f"the extents checked on history track {track.name if track else '(unknown)'}:"
                "\n\n"
                + "\n".join(f"  - {class_id}" for class_id in report.classes_without_extents)
                + "\n\nThat ground is UNKNOWN to the export pipeline, not negative. "
                "Recording where you swept cannot be reconstructed later.\n\n"
                # The extent layer is fetched with the canvas restriction on, so this is
                # a statement about the current view, not about the whole deployment -- and
                # now about one track, because both layers are scoped to it.
                "Scoped to the current map extent AND to this history track: an extent "
                "declared elsewhere in the country, or on another track, is not counted "
                "here.",
            )

    # ----------------------------------------------------------------- bootstrap

    def publish_local_layers(self) -> None:
        """Send the vector layers open in this project to the backend as new labels.

        The bootstrap path, and the one the analyst's whole existing workflow has to pass
        through once: 1,246 features in dated folders of shapefiles, becoming the founding
        dataset of a system that finally has identity and history. Everything is previewed
        first, because the server assigns identity and therefore nothing here can
        recognise a second run as a repeat.
        """
        if self.dock is None:
            return
        if self.publishing:
            self._message(
                "A publish is already running. Wait for it, or cancel it from the task "
                "manager in the status bar.",
                Qgis.MessageLevel.Warning,
            )
            return
        url = self._persist_url()
        if not url:
            self._fail("Enter the API URL first.")
            return
        if not self.registry:
            self._fail("Connect first: the class registry is what the layers are mapped onto.")
            return
        # Before the preview dialog, not after it. This is the one irreversible bulk write,
        # and a token that expires between the preview and the send leaves a publish that
        # stopped halfway with no way to tell which features landed.
        if self._defer_until_fresh(self.publish_local_layers):
            return
        # Before the dialog, not inside it. A preview that cannot say which dataset it
        # would write into is a preview of nothing -- and this is the plugin's one
        # irreversible bulk write, so the refusal belongs in front of the whole flow
        # rather than on a greyed-out button at the end of it.
        track = self._require_track("publish")
        if track is None:
            return

        sources = publish_tools.describe_layers()
        if not sources:
            self._message(
                "No local vector layers in this project. Layers loaded from the backend "
                "are excluded - they are already there, and QGIS's OAPIF provider edits "
                "them in place.",
                Qgis.MessageLevel.Warning,
            )
            return

        dialog = PublishDialog(sources, self.registry, self.iface.mainWindow(), track=track)
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            plan = dialog.plan()
        finally:
            # Parented to the main window, so Qt keeps the C++ object alive after the last
            # Python reference goes and unload() has nothing to detach it with. Publishing
            # three times would otherwise leave three hidden dialogs -- each still holding
            # a whole ClassRegistry -- as children of the QGIS window for the session.
            dialog.deleteLater()
        if not accepted:
            return
        selected = list(plan.selected())
        if not selected:
            return
        if not self._confirm_republish(plan):
            return

        collection = self._collection_setting(
            "label_collection", "Label collection", "Which collection holds the labels?"
        )
        if not collection:
            self._fail("No collection chosen. Nothing was published.")
            return

        extent_collection = ""
        if any(layer_plan.choice.declare_extent for layer_plan in selected):
            extent_collection = self._collection_setting(
                "extent_collection",
                "Survey extent collection",
                "Which collection holds the survey extents (labeled_extent)?",
            )

        try:
            # Main thread: builds the thread-safe feature sources and transforms the
            # worker will use. See publish.prepare for why this cannot happen in run().
            prepared = publish_tools.prepare(selected)
        except LabelClientError as exc:
            self._fail(str(exc))
            return

        request = publish_tools.PublishRequest(
            base_url=url,
            collection_id=collection,
            authcfg=self.settings.authcfg_for(track.name),
            layers=prepared,
            extent_collection=extent_collection,
            fields=self.registry.fields,
            track=track.name,
        )

        self.publishing = True
        self.publish_action.setEnabled(False)
        self.dock.set_busy(True)
        self.dock.set_publish_status(
            f"Publishing {plan.total_features()} feature(s) to {collection} on track {track.name}…"
        )

        def work(feedback: QgsFeedback) -> PublishReport:
            # Worker thread. Nothing here may touch widgets, iface or QgsProject.
            return publish_tools.publish(request, feedback)

        def failed(message: str) -> None:
            self._end_publish()
            self._fail(message)

        self.tasks.run(
            f"Publish local layers to {track.name}",
            work,
            lambda report: self._on_published(selected, collection, report, track.name),
            failed,
            # A cancelled publish is not a discarded read: part of it is already on the
            # server, and the user needs the summary saying which part.
            deliver_when_cancelled=True,
        )

    def _end_publish(self) -> None:
        self.publishing = False
        self.publish_action.setEnabled(True)

    def _confirm_republish(self, plan: PublishPlan) -> bool:
        """Make a second publish of the same layer a deliberate act.

        The plugin cannot deduplicate: identity is the server's, so it has no way to ask
        "is this feature already there?". What it can do is remember that it sent this
        layer once and refuse to let the second send be an accident.
        """
        repeats = plan.republished()
        if not repeats:
            return True
        # The track is in the question, not only in the record lines. "Publish a second
        # copy?" and "publish a second copy into the analysts' dataset?" are the same
        # click and different decisions.
        where = f" into history track {plan.track_name!r}" if plan.track_name else ""
        answer = QMessageBox.warning(
            self.iface.mainWindow(),
            "These layers have been published before",
            "\n\n".join(
                f"{layer_plan.source.name}: {layer_plan.source.previous.describe()}"
                for layer_plan in repeats
                if layer_plan.source.previous
            )
            + f"\n\nPublishing again adds a second copy of every feature{where}. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_published(
        self, plans, collection_id: str, report: PublishReport, track: str = ""
    ) -> None:
        self._end_publish()
        if self.dock is None:
            return
        self.dock.set_busy(False)
        # Stamped on the main thread, after the fact, and after a partial run too: "some
        # of this is already up there" is exactly what a cancelled run leaves behind.
        # The track is passed explicitly rather than read from the panel: by now the
        # selection may have moved, and the stamp records where these features went.
        publish_tools.stamp_published(plans, report, collection_id, track=track)

        self.dock.set_publish_status(report.summary())
        level = Qgis.MessageLevel.Success if report.clean else Qgis.MessageLevel.Warning
        self._message(report.summary(), level)
        log(report.summary())
        results = PublishReportDialog(report, self.iface.mainWindow())
        try:
            results.exec()
        finally:
            # See publish_local_layers: parented to the main window, so nothing else
            # detaches it.
            results.deleteLater()
