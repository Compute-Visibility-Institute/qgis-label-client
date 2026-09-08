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

from qgis.core import QgsApplication
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
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .core import recorded
from .core.asof import AsOfMechanism
from .core.collections import CollectionGroup
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


def _collapsible(title: str, parent: QWidget, *, collapsed: bool = False) -> QgsCollapsibleGroupBox:
    """A group box that remembers whether the analyst collapsed it.

    QgsCollapsibleGroupBox has always collapsed on click -- the arrows in the panel are
    not decoration. What it could not do here is REMEMBER, because QGIS keys the saved
    state on the widget's object name and none of these had one. Every restart reopened
    all nine groups, so the panel was a wall of text that had to be re-tidied each
    session and could only be scrolled.

    `collapsed` is the state on FIRST run only; the saved value wins afterwards. It is set
    for the groups that are not part of a working day -- a one-time bootstrap, a
    vocabulary reference -- so a fresh install opens on the controls somebody is about to
    use rather than on everything the plugin can do.
    """
    group = QgsCollapsibleGroupBox(title, parent)
    # Stable, derived from the title rather than hand-written, so a renamed group cannot
    # silently inherit another one's saved state.
    group.setObjectName("cvi_" + "".join(c if c.isalnum() else "_" for c in title.lower()))
    group.setSaveCollapsedState(True)
    if collapsed:
        group.setCollapsed(True)
    return group


def _collection_group_tooltip(group: CollectionGroup) -> str:
    """The hover text for one row of the collection list.

    A size-one group renders exactly as a single collection always has -- ``id:``, its
    description, its own transactional line. A collapsed group cannot use an ``id:`` line
    at all (no one id names the row any more), lists each member's description against its
    own id rather than merging them (the per-geometry descriptions in this deployment name
    different classes per family, which prose merging would lose), and states editability
    as a single line only when every member agrees, naming the split otherwise -- a
    checkbox is worse than useless if it is confidently wrong about part of what it means.
    """
    members = group.members
    if len(members) == 1:
        collection = members[0]
        lines = [f"id: {collection.collection_id}"]
        if collection.description:
            lines.append(collection.description)
        lines.append(_transactional_line(collection.transactional))
        return "\n".join(line for line in lines if line)

    lines = ["ids: " + ", ".join(sorted(group.collection_ids))]
    lines.extend(
        f"{member.collection_id}: {member.description}" for member in members if member.description
    )
    states = {member.transactional for member in members}
    if len(states) == 1:
        lines.append(_transactional_line(states.pop()))
    else:
        # Not expected in this deployment -- provider-identical siblings under one stem
        # agree by construction -- but the OGC API - Features spec does not guarantee it,
        # and a message naming the split ends an investigation instead of starting one.
        named = ", ".join(
            f"{member.collection_id}={_transactional_word(member.transactional)}"
            for member in sorted(members, key=lambda m: m.collection_id)
        )
        lines.append(f"Editability disagrees between parts of this collection: {named}.")
    return "\n".join(line for line in lines if line)


def _transactional_line(transactional: bool | None) -> str:
    """The one-line editability sentence, or ``""`` when there is nothing worth adding.

    ``False`` adds no line at all, on purpose: a read-only collection needs no sentence
    telling an analyst what they already know from not being able to edit it, and this
    asymmetry is the one :func:`_collection_group_tooltip` has to preserve for both a
    plain collection and a collapsed group.
    """
    if transactional is True:
        return "Editable (OGC API - Features Part 4)."
    if transactional is None:
        return "Editability not advertised by the server."
    return ""


def _transactional_word(transactional: bool | None) -> str:
    """One word per member, for the disagreement line only -- never shown on its own."""
    if transactional is True:
        return "editable"
    if transactional is None:
        return "not advertised"
    return "read-only"


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
    #: "Copy my address". The single most common next action after the backend answers
    #: "you authenticated fine and are not on the access list" is pasting that address to
    #: an administrator, and retyping an address by eye is how a grant lands on nobody.
    copyAddressRequested = pyqtSignal()
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
        self._busy = False
        self._write_access: bool | None = None
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )

        shell = QWidget(self)
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(shell)
        self.scroll_area = scroll
        scroll.setWidgetResizable(True)

        # Wrapped forms and separate action rows keep controls reachable in a narrow
        # dock. The canvas should not lose width just because a service title is long.
        scroll.setMinimumWidth(220)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
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

        layout.addStretch(1)
        for label in container.findChildren(QLabel):
            if label.wordWrap():
                label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        for combo in container.findChildren(QComboBox):
            combo.setMinimumWidth(0)
            combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        scroll.setWidget(container)
        shell_layout.addWidget(scroll, 1)
        footer = QWidget(shell)
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(8, 4, 8, 8)
        self.status_label = QLabel("Not connected.", footer)
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        footer_layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar(footer)
        self.progress_bar.setAccessibleName("Current operation progress")
        self.progress_bar.setVisible(False)
        footer_layout.addWidget(self.progress_bar)
        shell_layout.addWidget(footer)
        self.setWidget(shell)

        self.set_connected(False)

    # --- construction ---------------------------------------------------------

    def _build_connection_group(self, parent: QWidget) -> QWidget:
        group = _collapsible("Backend", parent)
        form = QFormLayout(group)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)

        self.url_edit = QLineEdit(group)
        # Without this the field's own sizeHint sets the dock's floor -- see the
        # minimum-width note above.
        self.url_edit.setMinimumWidth(120)
        # The placeholder is a reserved example domain; the real default lives in
        # settings.DEFAULTS and is filled in before this is ever seen. It survives for the
        # case that default was blanked deliberately, where an example is more useful than
        # an empty box.
        self.url_edit.setPlaceholderText(PLACEHOLDER_API_URL)
        self.url_edit.setToolTip(
            "Landing page of the OGC API - Features endpoint served by the api service."
        )
        form.addRow("API URL", self.url_edit)

        self.auth_label = QLabel("Not signed in.", group)
        self.auth_label.setWordWrap(True)
        # Selectable so the address, and a refusal message from the server, can be copied
        # into an email. A message somebody has to retype is a message that gets
        # paraphrased, and the whole value of the server's 403 text is that it is exact.
        self.auth_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        form.addRow("Account", self.auth_label)

        self.access_label = QLabel(
            "Read-only access. Contact an administrator to enable editing.", group
        )
        self.access_label.setWordWrap(True)
        self.access_label.setVisible(False)
        form.addRow(self.access_label)

        buttons = QHBoxLayout()
        self.sign_in_button = QPushButton("Sign in", group)
        self.sign_in_button.setToolTip(
            "Opens your browser to sign in with Google. The resulting token is stored "
            "encrypted in the QGIS authentication database and is never written to a "
            "project file or to this plugin's settings."
        )
        self.sign_out_button = QPushButton("Sign out", group)
        self.sign_out_button.setToolTip(
            "Removes every stored credential and revokes this plugin's access at Google, "
            "so 'signed out' is true on both sides."
        )
        self.copy_address_button = QPushButton("Copy address", group)
        self.copy_address_button.setIcon(QgsApplication.getThemeIcon("/mActionEditCopy.svg"))
        self.copy_address_button.setToolTip(
            "Copies the address you are signed in as. Paste it to an administrator if the "
            "backend says you are not on the access list."
        )
        self.connect_button = QPushButton("Connect", group)
        self.connect_button.setIcon(QgsApplication.getThemeIcon("/mIconConnect.svg"))
        self.connect_button.setDefault(True)
        buttons.addWidget(self.sign_in_button)
        buttons.addWidget(self.sign_out_button)
        form.addRow(buttons)
        actions = QHBoxLayout()
        actions.addWidget(self.copy_address_button)
        actions.addWidget(self.connect_button)
        form.addRow(actions)

        self.sign_in_button.clicked.connect(self.signInRequested)
        self.sign_out_button.clicked.connect(self.signOutRequested)
        self.copy_address_button.clicked.connect(self.copyAddressRequested)
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
        group = _collapsible("History track", parent)
        layout = QVBoxLayout(group)

        hint = QLabel(
            "Choose the dataset you want to work in.",
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
        group = _collapsible("Collections", parent)
        layout = QVBoxLayout(group)

        hint = QLabel(
            "Choose which layers to add to your project.",
            group,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.collection_list = QListWidget(group)
        self.collection_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.collection_list.setMinimumHeight(110)
        self.collection_list.setMaximumHeight(190)
        layout.addWidget(self.collection_list)

        self.load_button = QPushButton("Add selected layers", group)
        self.load_button.setIcon(QgsApplication.getThemeIcon("/mActionAddOgrLayer.svg"))
        self.load_button.clicked.connect(self._emit_load_layers)
        layout.addWidget(self.load_button)
        return group

    def _build_bootstrap_group(self, parent: QWidget) -> QWidget:
        group = _collapsible("Bootstrap", parent, collapsed=True)
        layout = QVBoxLayout(group)

        hint = QLabel(
            "Publish local layers to the shared dataset. Review the mapping and styles first.",
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
        group = _collapsible("Imagery", parent)
        layout = QVBoxLayout(group)

        hint = QLabel(
            "Refresh imagery access when images stop drawing.",
            group,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.imagery_status = QLabel("Imagery has not been refreshed yet.", group)
        self.imagery_status.setWordWrap(True)
        layout.addWidget(self.imagery_status)

        self.refresh_imagery_button = QPushButton("Refresh imagery", group)
        self.refresh_imagery_button.setIcon(QgsApplication.getThemeIcon("/mActionRefresh.svg"))
        self.refresh_imagery_button.clicked.connect(self.refreshImageryRequested)
        layout.addWidget(self.refresh_imagery_button)
        return group

    def _build_asof_group(self, parent: QWidget) -> QWidget:
        group = _collapsible("As-of date (valid time)", parent)
        layout = QVBoxLayout(group)

        hint = QLabel(
            "Show what was on the ground on a chosen date.",
            group,
        )
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(hint)

        self.asof_enabled = QCheckBox("Pin layers to a date", group)
        layout.addWidget(self.asof_enabled)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.asof_date = QDateEdit(group)
        self.asof_date.setCalendarPopup(True)
        self.asof_date.setDisplayFormat("yyyy-MM-dd")
        self.asof_date.setDate(QDate.currentDate())
        form.addRow("Date (UTC)", self.asof_date)

        layout.addLayout(form)
        advanced = _collapsible("Advanced date options", group, collapsed=True)
        advanced_form = QFormLayout(advanced)
        advanced_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.asof_mechanism = QComboBox(advanced)
        self.asof_mechanism.addItem("datetime (OGC standard)", AsOfMechanism.DATETIME.value)
        self.asof_mechanism.addItem("CQL2 filter on valid_from/valid_to", AsOfMechanism.CQL2.value)
        self.asof_mechanism.setToolTip(
            "datetime is the standard parameter. Switch to CQL2 if the server does not "
            "propagate it to item requests - CQL2 is sent on every request and cannot be "
            "silently dropped."
        )
        advanced_form.addRow("Date query method", self.asof_mechanism)
        layout.addWidget(advanced)

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
        group = _collapsible("Historical view (transaction time)", parent)
        layout = QVBoxLayout(group)

        hint = QLabel(
            "Add a read-only layer showing what the team believed at a chosen time.",
            group,
        )
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(hint)

        self.recorded_enabled = QCheckBox("Choose a historical instant", group)
        layout.addWidget(self.recorded_enabled)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
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
        self.add_recorded_button.setIcon(QgsApplication.getThemeIcon("/mActionHistory.svg"))
        self.add_recorded_button.clicked.connect(self._emit_recorded_view)
        layout.addWidget(self.add_recorded_button)

        note = QLabel(
            "Your current layers stay open for comparison.",
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
        self.add_recorded_button.setEnabled(
            self._connected and not self._busy and self.recorded_enabled.isChecked()
        )

    def _emit_recorded_view(self) -> None:
        moment = self.recorded_at()
        if moment:
            self.recordedViewRequested.emit(moment)

    def _build_qa_group(self, parent: QWidget) -> QWidget:
        group = _collapsible("QA", parent, collapsed=True)
        layout = QVBoxLayout(group)

        self.history_button = QPushButton("Selected label history…", group)
        self.history_button.setIcon(QgsApplication.getThemeIcon("/mActionHistory.svg"))
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
        group = _collapsible("Class vocabulary", parent, collapsed=True)
        layout = QVBoxLayout(group)

        hint = QLabel(
            "Look up a class and its available fields.",
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
        self._sync_actions()

    def _sync_actions(self) -> None:
        available = self._connected and not self._busy
        for widget in (
            self.load_button,
            self.refresh_imagery_button,
            self.apply_asof_button,
            self.history_button,
            self.coverage_button,
        ):
            widget.setEnabled(available)
        self.connect_button.setEnabled(not self._busy)
        self.track_combo.setEnabled(available)
        self.publish_button.setEnabled(available and self._write_access is not False)
        # Gated on the checkbox as well as the connection: it is the one button here that
        # adds a layer rather than changing one, and it sits next to "Apply to loaded
        # layers" on the other axis.
        self._sync_recorded_button()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync_actions()
        self.progress_bar.setRange(0, 0 if busy else 100)
        self.progress_bar.setVisible(busy)
        self.setCursor(Qt.CursorShape.BusyCursor if busy else Qt.CursorShape.ArrowCursor)

    def set_write_access(self, allowed: bool | None) -> None:
        self._write_access = allowed
        self.access_label.setVisible(allowed is False)
        self._sync_actions()

    def set_progress(self, percent: float) -> None:
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(max(0, min(100, round(percent))))

    def set_status(self, message: str) -> None:
        self.status_label.setText(message)

    def set_auth_status(self, message: str) -> None:
        self.auth_label.setText(message)

    def set_imagery_status(self, message: str) -> None:
        self.imagery_status.setText(message)
        self.set_status(message)

    def set_qa_result(self, message: str) -> None:
        self.qa_result.setText(message)

    def set_publish_status(self, message: str) -> None:
        self.publish_status.setText(message)
        self.set_status(message)

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
                f"Record start{where} is unknown. Earlier dates may show an empty layer."
            )
            return
        self.recorded_datetime.setMinimumDateTime(_as_qdatetime(floor))
        shown = recorded.display_instant(recorded.instant(floor))
        self.recorded_floor_label.setText(f"The record{where} starts at {shown}.")

    def set_axes(self, message: str) -> None:
        """The line that always names both time axes. Composed by the controller."""
        self.axes_label.setText(message)

    def set_collections(
        self, groups: Sequence[CollectionGroup], checked: Iterable[str] = ()
    ) -> None:
        """Populate the collection list, preserving which rows were checked.

        One row per :class:`CollectionGroup`, never per collection: a group of geometry-
        typed siblings (``label_current_point``/``_line``/``_polygon``) is one checkbox
        for one mode, which is the whole point of grouping upstream in
        :func:`.core.collections.group_by_mode` rather than here. A group of size one
        (every collection this deployment has not split by geometry) renders identically
        to a plain collection before this method learned about groups.
        """
        preselected = set(checked)
        self.collection_list.clear()
        for group in groups:
            item = QListWidgetItem(group.display_name, self.collection_list)
            # A comma-joined STRING, not the tuple itself. Every other item-data role in
            # this codebase (LAYER_ROLE in publishdialog.py, the combo boxes' userData)
            # already stores a plain string -- one of them re-wraps with str() on read as
            # a defensive habit -- and a collection id is an OGC API slug that cannot
            # contain a comma, so the join is unambiguous and lossless. Composite Python
            # objects are commonly said to survive Qt's QVariant marshaling, but nothing
            # in this codebase has needed that yet and nothing here can execute against a
            # real Qt binding to prove it does; matching the established plain-string
            # convention costs one join/split and needs no such proof.
            item.setData(COLLECTION_ROLE, ",".join(group.collection_ids))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            # ALL members, not any: a group with two of three siblings on the map is a
            # mode that is only PARTIALLY loaded, and a checked box asserting otherwise is
            # worse than an unchecked one -- it hides the missing third rather than
            # inviting a click that would complete it. See load_collections' per-id skip:
            # checking this box again sends every id and only the missing ones get added.
            item.setCheckState(
                Qt.CheckState.Checked
                if all(cid in preselected for cid in group.collection_ids)
                else Qt.CheckState.Unchecked
            )
            item.setToolTip(_collection_group_tooltip(group))

    def checked_collections(self) -> list[str]:
        ids: list[str] = []
        for row in range(self.collection_list.count()):
            item = self.collection_list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                ids.extend(str(item.data(COLLECTION_ROLE)).split(","))
        return ids

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
