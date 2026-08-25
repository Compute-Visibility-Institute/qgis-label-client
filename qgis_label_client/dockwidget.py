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
from datetime import date, datetime, timezone

from qgis.gui import QgsCollapsibleGroupBox
from qgis.PyQt.QtCore import QDate, QDateTime, Qt, QTime, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
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

from .core import recorded
from .core.asof import AsOfMechanism
from .core.collections import Collection
from .core.registry import ClassRegistry
from .core.tracks import Track
from .settings import PLACEHOLDER_API_URL

#: Item data role carrying a collection id on a list row.
COLLECTION_ROLE = int(Qt.ItemDataRole.UserRole) + 1


def _as_qdatetime(moment: datetime) -> QDateTime:
    """A ``QDateTime`` whose displayed components are `moment`'s UTC ones.

    Deliberately built with no time spec, so the widget holds a plain wall-clock value that
    is *labelled* UTC rather than a UTC-aware value Qt might convert on display. Two
    reasons: :meth:`LabelClientDock.recorded_at` reads the components straight back, so a
    conversion in either direction could only introduce an offset; and Qt 6.9 deprecated
    the time-spec form of this constructor in favour of one taking a ``QTimeZone``, which
    Qt 5 does not have -- this plugin has to build against both.
    """
    utc = moment.astimezone(timezone.utc)
    return QDateTime(QDate(utc.year, utc.month, utc.day), QTime(utc.hour, utc.minute, utc.second))


class LabelClientDock(QDockWidget):
    """Connection, collections, imagery, both time axes and QA, in one persistent panel.

    THE TWO TIME CONTROLS ARE TWO BOXES, and that is a design decision rather than a
    layout one. "As-of date (valid time)" asks what was true on the ground; "Historical
    view (transaction time)" asks what the team believed. Merging them into one control
    with a mode switch would hide the single most important thing about the pair.
    """

    connectRequested = pyqtSignal()
    signInRequested = pyqtSignal()
    signOutRequested = pyqtSignal()
    loadLayersRequested = pyqtSignal(list)
    refreshImageryRequested = pyqtSignal()
    asOfApplied = pyqtSignal()
    #: The transaction-time axis. Carries the rendered wire instant rather than a QDateTime
    #: so that the conversion happens exactly once, in core.recorded.instant, and the panel
    #: and the layer cannot disagree about what was asked for.
    recordedViewRequested = pyqtSignal(str)
    historyRequested = pyqtSignal()
    coverageRequested = pyqtSignal()
    publishRequested = pyqtSignal()
    trackChanged = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("CVI Label Client", parent)
        self.setObjectName("CviLabelClientDock")
        # Set before the groups are built: _build_vocabulary_group connects a signal that
        # can fire during construction.
        self._registry: ClassRegistry | None = None
        self._tracks: list[Track] = []
        # Guards the track combo the same way _refreshing guards the publish dialog:
        # repopulating it emits currentIndexChanged, and letting that reach the controller
        # would re-point every layer as a side effect of a refresh.
        self._loading_tracks = False
        # Remembered so set_busy can re-enable only what set_connected allows.
        self._connected = False
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        container = QWidget(scroll)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)

        layout.addWidget(self._build_connection_group(container))
        # Above Collections, because it scopes everything below it. A collection list read
        # without knowing which dataset it belongs to is a list of names.
        layout.addWidget(self._build_track_group(container))
        layout.addWidget(self._build_collections_group(container))
        layout.addWidget(self._build_bootstrap_group(container))
        layout.addWidget(self._build_imagery_group(container))
        layout.addWidget(self._build_asof_group(container))
        # Immediately below, and never inside it. Two time axes, two boxes -- see
        # _build_recorded_group.
        layout.addWidget(self._build_recorded_group(container))

        # Directly under both time controls, because it is about both of them.
        self.axes_label = QLabel("", container)
        self.axes_label.setWordWrap(True)
        self.axes_label.setToolTip(
            "Valid time is when a label was true on the ground. Transaction time is when "
            "the team believed it. Both are always in force, so both are always named."
        )
        layout.addWidget(self.axes_label)

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

    def _build_track_group(self, parent: QWidget) -> QWidget:
        """The dataset selector, and the banner that keeps saying which one it is.

        NOT COLLAPSIBLE-BY-DEFAULT, and the banner is outside the combo rather than being
        the combo's own text. Both for the same reason: the track is the piece of state
        that is most expensive to be wrong about and least visible while you are drawing.
        An annotator spends an afternoon in the map canvas, not in this panel, and the
        thing they need on screen is not a control -- it is an answer.
        """
        group = QgsCollapsibleGroupBox("History track", parent)
        layout = QVBoxLayout(group)

        hint = QLabel(
            "An isolated dataset. Labels drawn on one track are invisible from another, "
            "and the database enforces that - this panel only chooses which one you are "
            "in. Track names come from the backend; none is built into the plugin.",
            group,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.track_combo = QComboBox(group)
        self.track_combo.currentIndexChanged.connect(self._emit_track_changed)
        layout.addWidget(self.track_combo)

        self.track_banner = QLabel("Not connected.", group)
        self.track_banner.setWordWrap(True)
        self.track_banner.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.track_banner)
        return group

    def _emit_track_changed(self) -> None:
        if self._loading_tracks:
            return
        self.trackChanged.emit(self.selected_track())

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

    def _build_bootstrap_group(self, parent: QWidget) -> QWidget:
        group = QgsCollapsibleGroupBox("Bootstrap", parent)
        layout = QVBoxLayout(group)

        hint = QLabel(
            "One-time: send the vector layers already open in this project to the backend "
            "as the founding dataset. Everything is previewed first, and the server - not "
            "this plugin - assigns each feature its permanent identity.",
            group,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.publish_button = QPushButton("Publish local layers…", group)
        self.publish_button.setToolTip(
            "Reads the local vector layers in this project, guesses a class for each from "
            "the registry, and shows you the mapping before anything is sent."
        )
        self.publish_button.clicked.connect(self.publishRequested)
        layout.addWidget(self.publish_button)

        self.publish_status = QLabel("", group)
        self.publish_status.setWordWrap(True)
        layout.addWidget(self.publish_status)
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

    def _build_recorded_group(self, parent: QWidget) -> QWidget:
        """The transaction-time control: what the team BELIEVED at a chosen instant.

        A SECOND, SEPARATE BOX, never a mode of the one above it. The two answer different
        questions -- "what was true on the ground" and "what did we think" -- and a single
        control with a mode switch would make the most important thing about this feature
        (that they are different) into the least visible thing about it.

        The vocabulary is kept disjoint for the same reason. This box says **believed**; the
        box above says **as-of**. If both said "as of", a screenshot of the panel would not
        say which axis produced the map.

        IT ADDS A LAYER; it does not re-point the ones already loaded, unlike the as-of
        control. That is the whole use case: the live layer and a historical one open at
        once, and two historical ones at different instants if you want to compare beliefs.
        """
        group = QgsCollapsibleGroupBox("Historical view (transaction time)", parent)
        layout = QVBoxLayout(group)

        hint = QLabel(
            "Adds a layer showing the labels <i>as the team believed them</i> at one "
            "instant - including labels deleted since, and the geometry of labels edited "
            "since. This is transaction time; the box above is valid time, which is a "
            "different question.",
            group,
        )
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(hint)

        self.recorded_enabled = QCheckBox("Pin a historical layer to an instant", group)
        layout.addWidget(self.recorded_enabled)

        form = QFormLayout()
        self.recorded_datetime = QDateTimeEdit(group)
        self.recorded_datetime.setCalendarPopup(True)
        # Seconds shown, because the wire format has them and a picker that hides them
        # would let two different instants look identical in the UI.
        self.recorded_datetime.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.recorded_datetime.setToolTip(
            "UTC. An instant in the future is refused: the belief set at a future time is "
            "simply the current one, so the layer would be full of features under a "
            "caption asserting something nobody has ever believed."
        )
        form.addRow("Instant (UTC)", self.recorded_datetime)
        layout.addLayout(form)

        self.add_recorded_button = QPushButton("Add historical layer", group)
        self.add_recorded_button.clicked.connect(self._emit_recorded_view)
        layout.addWidget(self.add_recorded_button)

        note = QLabel(
            "<b>Read-only.</b> A past belief is a record, not a draft, so QGIS greys the "
            "pencil out on this layer - that is not a fault. Adds a layer; it does not "
            "change the layers you already have.",
            group,
        )
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(note)

        self.recorded_floor_label = QLabel("", group)
        self.recorded_floor_label.setWordWrap(True)
        layout.addWidget(self.recorded_floor_label)

        self.recorded_enabled.toggled.connect(self.recorded_datetime.setEnabled)
        self.recorded_enabled.toggled.connect(self._sync_recorded_button)
        self.recorded_datetime.setEnabled(False)
        self.add_recorded_button.setEnabled(False)
        return group

    def _sync_recorded_button(self) -> None:
        self.add_recorded_button.setEnabled(self._connected and self.recorded_enabled.isChecked())

    def _emit_recorded_view(self) -> None:
        moment = self.recorded_at()
        if moment:
            self.recordedViewRequested.emit(moment)

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
        self._connected = connected
        for widget in (
            self.load_button,
            self.publish_button,
            self.refresh_imagery_button,
            self.apply_asof_button,
            self.history_button,
            self.coverage_button,
        ):
            widget.setEnabled(connected)
        # Gated on the checkbox as well as the connection: it is the one button here that
        # adds a layer rather than changing one, and it sits next to "Apply to loaded
        # layers" on the other axis.
        self._sync_recorded_button()

    def set_busy(self, busy: bool) -> None:
        self.connect_button.setEnabled(not busy)
        # Publishing is the one action that must not be startable twice. A second run
        # while the first is in flight doubles the data, and the server assigns identity
        # so nothing can recognise the repeat afterwards.
        self.publish_button.setEnabled(self._connected and not busy)
        self.setCursor(Qt.CursorShape.BusyCursor if busy else Qt.CursorShape.ArrowCursor)

    def set_status(self, message: str) -> None:
        self.status_label.setText(message)

    def set_auth_status(self, message: str) -> None:
        self.auth_label.setText(message)

    def set_imagery_status(self, message: str) -> None:
        self.imagery_status.setText(message)

    def set_qa_result(self, message: str) -> None:
        self.qa_result.setText(message)

    def set_publish_status(self, message: str) -> None:
        self.publish_status.setText(message)

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

    # --- the transaction-time axis --------------------------------------------

    def recorded_at(self) -> str:
        """The picked instant in wire form, or ``""`` when the control is off.

        Rendered here and nowhere else in the UI, so the header, the canary and the layer
        name all descend from one conversion. The widget's components are *read as UTC*,
        which is what the field label promises -- see :func:`_as_qdatetime` for why the
        widget itself is left in local time.
        """
        if not self.recorded_enabled.isChecked():
            return ""
        value = self.recorded_datetime.dateTime()
        day, clock = value.date(), value.time()
        try:
            moment = datetime(
                int(day.year()),
                int(day.month()),
                int(day.day()),
                int(clock.hour()),
                int(clock.minute()),
                int(clock.second()),
                tzinfo=timezone.utc,
            )
        except ValueError:
            return ""
        return recorded.instant(moment)

    def set_recorded_default(self, moment: str) -> None:
        """Open the picker on a remembered instant. Does NOT arm the control.

        A remembered default, never a restored state: a ticked box on startup would say a
        historical layer is in play when none is. The instant a layer is actually a view of
        lives on that layer, not here.
        """
        parsed = recorded.parse_instant(moment) or datetime.now(timezone.utc)
        self.recorded_datetime.setDateTime(_as_qdatetime(parsed))

    def set_recorded_bounds(self, earliest: str = "", track_name: str = "") -> None:
        """Constrain the picker to instants the backend can actually answer.

        The ceiling is now: a future instant resolves to the *current* belief set, which is
        a full layer under a caption asserting something nobody has ever believed.

        The floor is the track's earliest recorded belief, when the backend publishes one.
        Not because an earlier instant is an error -- "nothing was believed yet" is a
        correct answer -- but because an empty layer and a broken one look identical, and
        the cheapest fix is to make the case hard to reach and explain it when it is not.
        """
        now = datetime.now(timezone.utc)
        self.recorded_datetime.setMaximumDateTime(_as_qdatetime(now))
        floor = recorded.parse_rfc3339(earliest)
        where = f" on track {track_name}" if track_name else ""
        if floor is None:
            self.recorded_floor_label.setText(
                f"The backend did not say how far back the record{where} goes, so the "
                "picker has no floor. An instant before the data existed is a valid "
                "question; the answer is an empty layer."
            )
            return
        self.recorded_datetime.setMinimumDateTime(_as_qdatetime(floor))
        shown = recorded.display_instant(recorded.instant(floor))
        self.recorded_floor_label.setText(f"The record{where} starts at {shown}.")

    def set_axes(self, message: str) -> None:
        """The line that always names both time axes. Composed by the controller."""
        self.axes_label.setText(message)

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

    def set_tracks(self, tracks: Sequence[Track], selected: str = "") -> None:
        """Populate the track combo, preserving the selection where it still exists.

        A stored track that the backend no longer offers is **not** silently replaced by
        the default. It is shown as missing, with nothing selected, because answering a
        request for one dataset from another is the contamination failure in reverse: you
        would conclude the track you asked for was empty.
        """
        self._tracks = list(tracks)
        self._loading_tracks = True
        try:
            self.track_combo.clear()
            for track in self._tracks:
                self.track_combo.addItem(track.describe(), track.name)
                tooltip = [f"name: {track.name}"]
                if track.track_id:
                    tooltip.append(f"track_id: {track.track_id}")
                if track.description:
                    tooltip.append(track.description)
                if track.warning():
                    tooltip.append(track.warning())
                self.track_combo.setItemData(
                    self.track_combo.count() - 1,
                    "\n".join(tooltip),
                    int(Qt.ItemDataRole.ToolTipRole),
                )
            index = self.track_combo.findData(selected) if selected else -1
            self.track_combo.setCurrentIndex(index)
        finally:
            self._loading_tracks = False

    def selected_track(self) -> str:
        return str(self.track_combo.currentData() or "")

    def set_track_banner(self, message: str) -> None:
        """The persistent "you are here" line. Set by the controller, never derived here."""
        self.track_banner.setText(message)

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
