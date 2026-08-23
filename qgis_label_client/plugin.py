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
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from qgis.core import Qgis, QgsFeedback, QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QDialog, QInputDialog, QLineEdit, QMessageBox

from . import auth, client, imagery, qa
from . import layers as layer_tools
from . import publish as publish_tools
from .core.asof import AsOfMechanism, describe
from .core.collections import Collection
from .core.errors import LabelClientError
from .core.publish import PublishPlan, PublishReport
from .core.registry import ClassRegistry
from .core.teardown import Teardown
from .dockwidget import LabelClientDock
from .historydialog import HistoryDialog
from .log import log, log_error, log_warning
from .publishdialog import PublishDialog, PublishReportDialog
from .settings import PluginSettings
from .tasks import TaskRunner

MENU_NAME = "&CVI Label Client"
PLUGIN_DIR = os.path.dirname(__file__)

#: How many recorded beliefs one history request asks for. A cap rather than paging
#: because the dialog is a read, not a browser -- but a silent cap would present a
#: truncated history as a complete one, so :meth:`LabelClientPlugin._on_history` says
#: when it has been hit.
HISTORY_LIMIT = 200


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
        # A publish in flight. Guarded rather than merely discouraged: the menu entry and
        # the panel button both reach it, and two concurrent runs would double the data
        # with nothing able to tell afterwards which copy is which.
        self.publishing = False

    # ------------------------------------------------------------------ lifecycle

    def initGui(self) -> None:  # noqa: N802 - name fixed by the QGIS plugin contract
        icon = QIcon(os.path.join(PLUGIN_DIR, "icons", "cvi.svg"))

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
        log("Plugin loaded.")

    def unload(self) -> None:
        """Detach everything. QGIS cleans up nothing."""
        # Tasks first: a request that completes after the dock is gone would call into a
        # destroyed widget, which is a crash rather than a warning.
        self.tasks.shutdown()
        for failure in self.teardown.run():
            log_error(f"Teardown step {failure.label!r} failed: {failure.error}")
        self.dock = None
        self.registry = None
        self.collections = []
        self.publishing = False
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
        self.dock.loadLayersRequested.connect(self.load_collections)
        self.dock.refreshImageryRequested.connect(self.refresh_imagery)
        self.dock.asOfApplied.connect(self.apply_as_of)
        self.dock.historyRequested.connect(self.show_history)
        self.dock.coverageRequested.connect(self.check_coverage)
        self.dock.publishRequested.connect(self.publish_local_layers)

    def _restore_settings(self) -> None:
        if self.dock is None:
            return
        self.dock.set_api_url(self.settings.api_base_url)
        self.dock.set_as_of(self.settings.as_of)
        self.dock.set_as_of_mechanism(self.settings.as_of_mechanism.value)
        self._refresh_auth_label()

    # ------------------------------------------------------------------- helpers

    def _message(self, text: str, level: Qgis.MessageLevel = Qgis.MessageLevel.Info) -> None:
        self.iface.messageBar().pushMessage("CVI Label Client", text, level, 8)

    def _fail(self, text: str) -> None:
        log_error(text)
        self._message(text, Qgis.MessageLevel.Critical)
        if self.dock is not None:
            self.dock.set_busy(False)
            self.dock.set_status(text)

    def _refresh_auth_label(self) -> None:
        if self.dock is None:
            return
        try:
            summary = auth.summarise(self.settings.authcfg)
        except LabelClientError as exc:
            self.dock.set_auth_status(str(exc))
            return
        self.dock.set_auth_status(
            summary.describe() if summary else "No credential stored. Anonymous requests."
        )

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
        """Prompt for an API token and hand it to ``QgsAuthManager``.

        The token is read from the dialog, passed straight to the auth manager and
        dropped. It is never logged, never put in settings and never held on the plugin
        object.
        """
        token, accepted = QInputDialog.getText(
            self.iface.mainWindow(),
            "Sign in to the labeling API",
            "API token (stored encrypted in the QGIS authentication database, "
            "never in a project file):",
            QLineEdit.EchoMode.Password,
        )
        if not accepted or not token.strip():
            return
        try:
            # The token goes straight through to the auth manager. It is not assigned to
            # an attribute, not written to settings and not logged; the only durable copy
            # is the encrypted one in qgis-auth.db.
            authcfg = auth.store_bearer_token(token, self.settings.authcfg)
        except LabelClientError as exc:
            self._fail(str(exc))
            return
        self.settings.set("authcfg", authcfg)
        self._refresh_auth_label()
        self._message(f"Credential stored as {authcfg}.", Qgis.MessageLevel.Success)

    def sign_out(self) -> None:
        authcfg = self.settings.authcfg
        if not authcfg:
            return
        try:
            auth.remove(authcfg)
        except LabelClientError as exc:
            log_warning(str(exc))
        self.settings.set("authcfg", "")
        self._refresh_auth_label()
        self._message("Credential removed.")

    def connect_backend(self) -> None:
        """Fetch the collection list and the class registry."""
        if self.dock is None:
            return
        url = self._persist_url()
        if not url:
            self._fail("Enter the API URL first.")
            return
        authcfg = self.settings.authcfg
        registry_path = str(self.settings.get("class_registry_path"))

        self.dock.set_busy(True)
        self.dock.set_status("Connecting…")

        def work(feedback: QgsFeedback) -> dict[str, Any]:
            # Worker thread. Nothing here may touch widgets, iface or QgsProject.
            return {
                "collections": client.fetch_collections(url, authcfg, feedback),
                "registry": client.fetch_registry(url, registry_path, authcfg, feedback),
            }

        self.tasks.run("Connect to labeling API", work, self._on_connected, self._fail)

    def _on_connected(self, result: dict[str, Any]) -> None:
        if self.dock is None:
            return
        self.collections = result["collections"]
        self.registry = result["registry"]
        loaded = {layer_tools.collection_of(layer) for layer in layer_tools.plugin_layers()}
        self.dock.set_collections(self.collections, checked=loaded)
        self.dock.set_registry(self.registry)
        self.dock.set_connected(True)
        self.dock.set_busy(False)
        self.dock.set_status(
            f"Connected. {len(self.collections)} collection(s), "
            f"{len(self.registry)} class(es) in the registry."
        )
        log(
            f"Connected: {len(self.collections)} collections, "
            f"{len(self.registry)} classes from {self.registry.source_url}"
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

        titles = {c.collection_id: c.display_name for c in self.collections}
        project = QgsProject.instance()
        existing = {
            layer_tools.collection_of(layer): layer for layer in layer_tools.plugin_layers()
        }

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
                    )
                except LabelClientError as exc:
                    log_warning(str(exc))
                    self._message(str(exc), Qgis.MessageLevel.Warning)
                    continue
                layer_tools.apply_registry(layer, self.registry)
                project.addMapLayer(layer)
                added += 1
        finally:
            self.dock.set_busy(False)

        self.dock.set_status(f"Loaded {added} layer(s).")

    def apply_as_of(self) -> None:
        """Re-point every plugin layer at the chosen valid-time instant."""
        if self.dock is None:
            return
        as_of = self.dock.as_of()
        mechanism = AsOfMechanism.parse(self.dock.as_of_mechanism())
        self.settings.set_as_of(as_of)
        self.settings.set("as_of_mechanism", mechanism.value)

        targets = layer_tools.plugin_layers()
        for layer in targets:
            uri = layer_tools.build_layer_uri(
                self.settings, layer_tools.collection_of(layer), self.registry
            )
            layer_tools.repoint_layer(layer, uri)

        summary = describe(as_of, mechanism)
        self.dock.set_status(f"{summary} - {len(targets)} layer(s) updated.")
        log(summary)

    # ------------------------------------------------------------------- imagery

    def refresh_imagery(self) -> None:
        """Mint fresh signed URLs and swap them into the raster layers."""
        url = self._persist_url()
        if not url:
            self._fail("Enter the API URL first.")
            return
        authcfg = self.settings.authcfg
        path = str(self.settings.get("signed_urls_path"))
        if self.dock is not None:
            self.dock.set_busy(True)
            self.dock.set_imagery_status("Requesting signed URLs…")

        def work(feedback: QgsFeedback):
            return client.fetch_signed_assets(url, path, authcfg, feedback)

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

        def work(feedback: QgsFeedback):
            return client.fetch_history(
                url,
                collection,
                str(label_id),
                authcfg,
                field,
                limit=HISTORY_LIMIT,
                feedback=feedback,
            )

        self.tasks.run(
            "Fetch label history",
            work,
            lambda entries: self._on_history(str(label_id), entries),
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

    def _on_history(self, label_id: str, entries) -> None:
        if self.dock is None:
            return
        self.dock.set_busy(False)
        if not entries:
            self._message(f"No history recorded for {label_id}.")
            return
        if len(entries) >= HISTORY_LIMIT:
            # An audit trail silently cut off at the limit reads as the whole story.
            self._message(
                f"Showing the {HISTORY_LIMIT} most recent recorded beliefs for this "
                "label. There may be older ones.",
                Qgis.MessageLevel.Warning,
            )
        HistoryDialog(label_id, entries, self.iface.mainWindow()).exec()

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

        self.dock.set_qa_result(report.summary())
        level = Qgis.MessageLevel.Success if report.clean else Qgis.MessageLevel.Warning
        self._message(report.summary(), level)
        log(report.summary())

        if report.classes_without_extents:
            QMessageBox.information(
                self.iface.mainWindow(),
                "No survey extent declared",
                "These classes have labels here but no exhaustive labeled_extent among "
                "the extents checked:\n\n"
                + "\n".join(f"  - {class_id}" for class_id in report.classes_without_extents)
                + "\n\nThat ground is UNKNOWN to the export pipeline, not negative. "
                "Recording where you swept cannot be reconstructed later.\n\n"
                # The extent layer is fetched with the canvas restriction on, so this is
                # a statement about the current view, not about the whole deployment.
                "Scoped to the current map extent: an extent declared elsewhere in the "
                "country is not counted here.",
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

        sources = publish_tools.describe_layers()
        if not sources:
            self._message(
                "No local vector layers in this project. Layers loaded from the backend "
                "are excluded - they are already there, and QGIS's OAPIF provider edits "
                "them in place.",
                Qgis.MessageLevel.Warning,
            )
            return

        dialog = PublishDialog(sources, self.registry, self.iface.mainWindow())
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
            authcfg=self.settings.authcfg,
            layers=prepared,
            extent_collection=extent_collection,
            fields=self.registry.fields,
        )

        self.publishing = True
        self.publish_action.setEnabled(False)
        self.dock.set_busy(True)
        self.dock.set_publish_status(
            f"Publishing {plan.total_features()} feature(s) to {collection}…"
        )

        def work(feedback: QgsFeedback) -> PublishReport:
            # Worker thread. Nothing here may touch widgets, iface or QgsProject.
            return publish_tools.publish(request, feedback)

        def failed(message: str) -> None:
            self._end_publish()
            self._fail(message)

        self.tasks.run(
            "Publish local layers",
            work,
            lambda report: self._on_published(selected, collection, report),
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
        answer = QMessageBox.warning(
            self.iface.mainWindow(),
            "These layers have been published before",
            "\n\n".join(
                f"{layer_plan.source.name}: {layer_plan.source.previous.describe()}"
                for layer_plan in repeats
                if layer_plan.source.previous
            )
            + "\n\nPublishing again adds a second copy of every feature. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_published(self, plans, collection_id: str, report: PublishReport) -> None:
        self._end_publish()
        if self.dock is None:
            return
        self.dock.set_busy(False)
        # Stamped on the main thread, after the fact, and after a partial run too: "some
        # of this is already up there" is exactly what a cancelled run leaves behind.
        publish_tools.stamp_published(plans, report, collection_id)

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
