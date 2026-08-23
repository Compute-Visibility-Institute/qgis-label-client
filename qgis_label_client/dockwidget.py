"""The dock panel.

A labeling tool persists while the user works, so it belongs in a dock rather than a
dialog: the as-of date, the connection state and the imagery expiry are all things you
want visible while drawing, not things you open, use and close.

The panel is a **view only**. It builds widgets, emits signals and renders whatever state
it is handed; it makes no network calls, touches no layers and holds no tasks. The plugin
module is the controller. Keeping that boundary means the panel can be constructed and
destroyed repeatedly without leaking anything, which is what the reload test checks.

Built in code rather than from a ``.ui`` file. Two reasons: ``.ui`` loading drags in
``uic`` differences between Qt5 and Qt6, and a code-built panel makes the attach/detach
pairing visible in one place.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date

from qgis.gui import QgsCollapsibleGroupBox
from qgis.PyQt.QtCore import QDate, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDockWidget,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .core.asof import AsOfMechanism
from .core.collections import Collection
from .core.registry import ClassRegistry
from .settings import PLACEHOLDER_API_URL

#: Item data role carrying a collection id on a list row.
COLLECTION_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class LabelClientDock(QDockWidget):
    """Connection, collections, imagery, as-of and QA, in one persistent panel."""

    connectRequested = pyqtSignal()
    signInRequested = pyqtSignal()
    signOutRequested = pyqtSignal()
    loadLayersRequested = pyqtSignal(list)
    refreshImageryRequested = pyqtSignal()
    asOfApplied = pyqtSignal()
    historyRequested = pyqtSignal()
    coverageRequested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("CVI Label Client", parent)
        self.setObjectName("CviLabelClientDock")
        # Set before the groups are built: _build_vocabulary_group connects a signal that
        # can fire during construction.
        self._registry: ClassRegistry | None = None
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        container = QWidget(scroll)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)

        layout.addWidget(self._build_connection_group(container))
        layout.addWidget(self._build_collections_group(container))
        layout.addWidget(self._build_imagery_group(container))
        layout.addWidget(self._build_asof_group(container))
        layout.addWidget(self._build_qa_group(container))
        layout.addWidget(self._build_vocabulary_group(container))

        self.status_label = QLabel("Not connected.", container)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        scroll.setWidget(container)
        self.setWidget(scroll)

        self.set_connected(False)

    # --- construction ---------------------------------------------------------

    def _build_connection_group(self, parent: QWidget) -> QWidget:
        group = QgsCollapsibleGroupBox("Backend", parent)
        form = QFormLayout(group)

        self.url_edit = QLineEdit(group)
        # Placeholder, never a default value. This repository is public and must not
        # contain a deployment hostname.
        self.url_edit.setPlaceholderText(PLACEHOLDER_API_URL)
        self.url_edit.setToolTip(
            "Landing page of the OGC API - Features endpoint served by the api service."
        )
        form.addRow("API URL", self.url_edit)

        self.auth_label = QLabel("No credential stored.", group)
        self.auth_label.setWordWrap(True)
        form.addRow("Credential", self.auth_label)

        buttons = QHBoxLayout()
        self.sign_in_button = QPushButton("Sign in…", group)
        self.sign_in_button.setToolTip(
            "Stores an API token in the QGIS authentication database. The token is never "
            "written to a project file or to this plugin's settings."
        )
        self.sign_out_button = QPushButton("Sign out", group)
        self.connect_button = QPushButton("Connect", group)
        self.connect_button.setDefault(True)
        buttons.addWidget(self.sign_in_button)
        buttons.addWidget(self.sign_out_button)
        buttons.addStretch(1)
        buttons.addWidget(self.connect_button)
        form.addRow(buttons)

        self.sign_in_button.clicked.connect(self.signInRequested)
        self.sign_out_button.clicked.connect(self.signOutRequested)
        self.connect_button.clicked.connect(self.connectRequested)
        return group

    def _build_collections_group(self, parent: QWidget) -> QWidget:
        group = QgsCollapsibleGroupBox("Collections", parent)
        layout = QVBoxLayout(group)

        hint = QLabel(
            "Listed from the backend. QGIS's native OGC API - Features provider does the "
            "reading and the editing; nothing here is hardcoded.",
            group,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.collection_list = QListWidget(group)
        self.collection_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.collection_list.setMinimumHeight(110)
        layout.addWidget(self.collection_list)

        self.load_button = QPushButton("Load checked collections", group)
        self.load_button.clicked.connect(self._emit_load_layers)
        layout.addWidget(self.load_button)
        return group

    def _build_imagery_group(self, parent: QWidget) -> QWidget:
        group = QgsCollapsibleGroupBox("Imagery", parent)
        layout = QVBoxLayout(group)

        hint = QLabel(
            "Imagery is streamed straight from the bucket over signed URLs that expire. "
            "Refresh at the start of a session, and whenever rasters stop drawing.",
            group,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.imagery_status = QLabel("No signed URLs fetched yet.", group)
        self.imagery_status.setWordWrap(True)
        layout.addWidget(self.imagery_status)

        self.refresh_imagery_button = QPushButton("Refresh imagery URLs", group)
        self.refresh_imagery_button.clicked.connect(self.refreshImageryRequested)
        layout.addWidget(self.refresh_imagery_button)
        return group

    def _build_asof_group(self, parent: QWidget) -> QWidget:
        group = QgsCollapsibleGroupBox("As-of date (valid time)", parent)
        layout = QVBoxLayout(group)

        hint = QLabel(
            "Shows the world as it was on the ground on a chosen date. This is valid "
            "time. Reproducing what we <i>believed</i> on a date is transaction time and "
            "is a server-side query - it has no OGC parameter.",
            group,
        )
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(hint)

        self.asof_enabled = QCheckBox("Pin layers to a date", group)
        layout.addWidget(self.asof_enabled)

        form = QFormLayout()
        self.asof_date = QDateEdit(group)
        self.asof_date.setCalendarPopup(True)
        self.asof_date.setDisplayFormat("yyyy-MM-dd")
        self.asof_date.setDate(QDate.currentDate())
        form.addRow("Date (UTC)", self.asof_date)

        self.asof_mechanism = QComboBox(group)
        self.asof_mechanism.addItem("datetime (OGC standard)", AsOfMechanism.DATETIME.value)
        self.asof_mechanism.addItem("CQL2 filter on valid_from/valid_to", AsOfMechanism.CQL2.value)
        self.asof_mechanism.setToolTip(
            "datetime is the standard parameter. Switch to CQL2 if the server does not "
            "propagate it to item requests - CQL2 is sent on every request and cannot be "
            "silently dropped."
        )
        form.addRow("Sent as", self.asof_mechanism)
        layout.addLayout(form)

        self.apply_asof_button = QPushButton("Apply to loaded layers", group)
        self.apply_asof_button.clicked.connect(self.asOfApplied)
        layout.addWidget(self.apply_asof_button)

        self.asof_enabled.toggled.connect(self.asof_date.setEnabled)
        self.asof_enabled.toggled.connect(self.asof_mechanism.setEnabled)
        self.asof_date.setEnabled(False)
        self.asof_mechanism.setEnabled(False)
        return group

    def _build_qa_group(self, parent: QWidget) -> QWidget:
        group = QgsCollapsibleGroupBox("QA", parent)
        layout = QVBoxLayout(group)

        self.history_button = QPushButton("History of selected label…", group)
        self.history_button.setToolTip(
            "Every recorded belief about the selected label, keyed on its immutable label_id."
        )
        self.history_button.clicked.connect(self.historyRequested)
        layout.addWidget(self.history_button)

        self.coverage_button = QPushButton("Check survey coverage", group)
        self.coverage_button.setToolTip(
            "Finds labels outside any exhaustive labeled_extent for their class. That "
            "ground is unknown to the export pipeline, never negative."
        )
        self.coverage_button.clicked.connect(self.coverageRequested)
        layout.addWidget(self.coverage_button)

        self.qa_result = QLabel("", group)
        self.qa_result.setWordWrap(True)
        layout.addWidget(self.qa_result)
        return group

    def _build_vocabulary_group(self, parent: QWidget) -> QWidget:
        group = QgsCollapsibleGroupBox("Class vocabulary", parent)
        layout = QVBoxLayout(group)

        hint = QLabel(
            "Served by the backend, not built into the plugin. Attributes live in JSONB "
            "governed by each class's JSON Schema, so adding one needs no plugin release.",
            group,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.class_combo = QComboBox(group)
        self.class_combo.currentIndexChanged.connect(self._show_class_help)
        layout.addWidget(self.class_combo)

        self.class_help = QPlainTextEdit(group)
        self.class_help.setReadOnly(True)
        self.class_help.setMinimumHeight(120)
        layout.addWidget(self.class_help)
        return group

    # --- view state -----------------------------------------------------------

    def set_connected(self, connected: bool) -> None:
        for widget in (
            self.load_button,
            self.refresh_imagery_button,
            self.apply_asof_button,
            self.history_button,
            self.coverage_button,
        ):
            widget.setEnabled(connected)

    def set_busy(self, busy: bool) -> None:
        self.connect_button.setEnabled(not busy)
        self.setCursor(Qt.CursorShape.BusyCursor if busy else Qt.CursorShape.ArrowCursor)

    def set_status(self, message: str) -> None:
        self.status_label.setText(message)

    def set_auth_status(self, message: str) -> None:
        self.auth_label.setText(message)

    def set_imagery_status(self, message: str) -> None:
        self.imagery_status.setText(message)

    def set_qa_result(self, message: str) -> None:
        self.qa_result.setText(message)

    def api_url(self) -> str:
        return self.url_edit.text().strip()

    def set_api_url(self, url: str) -> None:
        self.url_edit.setText(url)

    def as_of(self) -> date | None:
        if not self.asof_enabled.isChecked():
            return None
        value = self.asof_date.date()
        return date(value.year(), value.month(), value.day())

    def set_as_of(self, value: date | None) -> None:
        self.asof_enabled.setChecked(value is not None)
        if value is not None:
            self.asof_date.setDate(QDate(value.year, value.month, value.day))

    def as_of_mechanism(self) -> str:
        return str(self.asof_mechanism.currentData())

    def set_as_of_mechanism(self, mechanism: str) -> None:
        index = self.asof_mechanism.findData(mechanism)
        if index >= 0:
            self.asof_mechanism.setCurrentIndex(index)

    def set_collections(
        self, collections: Sequence[Collection], checked: Iterable[str] = ()
    ) -> None:
        """Populate the collection list, preserving which rows were checked."""
        preselected = set(checked)
        self.collection_list.clear()
        for collection in collections:
            item = QListWidgetItem(collection.display_name, self.collection_list)
            item.setData(COLLECTION_ROLE, collection.collection_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if collection.collection_id in preselected
                else Qt.CheckState.Unchecked
            )
            tooltip = [f"id: {collection.collection_id}"]
            if collection.description:
                tooltip.append(collection.description)
            if collection.transactional is True:
                tooltip.append("Editable (OGC API - Features Part 4).")
            elif collection.transactional is None:
                tooltip.append("Editability not advertised by the server.")
            item.setToolTip("\n".join(tooltip))

    def checked_collections(self) -> list[str]:
        return [
            str(self.collection_list.item(row).data(COLLECTION_ROLE))
            for row in range(self.collection_list.count())
            if self.collection_list.item(row).checkState() == Qt.CheckState.Checked
        ]

    def set_registry(self, registry: ClassRegistry | None) -> None:
        # Assign before clearing: clear() emits currentIndexChanged, and _show_class_help
        # would otherwise render the previous registry's text for one frame.
        self._registry = registry
        self.class_combo.clear()
        if registry is None:
            self.class_help.setPlainText("")
            return
        for label_class in registry:
            suffix = "" if label_class.active else "  (retired)"
            self.class_combo.addItem(label_class.display_name + suffix, label_class.class_id)
        self._show_class_help()

    def _show_class_help(self) -> None:
        registry = self._registry
        if registry is None:
            return
        class_id = self.class_combo.currentData()
        label_class = registry.get(str(class_id)) if class_id else None
        self.class_help.setPlainText(label_class.help_text() if label_class else "")

    def _emit_load_layers(self) -> None:
        self.loadLayersRequested.emit(self.checked_collections())
