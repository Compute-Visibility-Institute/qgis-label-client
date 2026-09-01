"""The preview that stands between 1,246 local features and an empty backend.

WHY A DIALOG AND NOT A BUTTON

This is a bootstrap into a system that has nothing in it. The server assigns identity, so
the plugin cannot recognise what it has already sent; a wrong class mapping cannot be
undone by re-running, and a name published with its final character missing becomes the
authoritative name. Every one of those is cheap to prevent here and expensive to unpick
afterwards, which is the entire argument for making the user look at a table first.

THE FOUR THINGS THIS SCREEN EXISTS TO SAY OUT LOUD

* **which history track these features join** -- the dataset, named at the top of the
  dialog and again in the button, because it is the one thing on this screen that is
  chosen somewhere else and therefore the one thing nobody thinks to check;
* **which class each layer becomes** -- guessed from the layer name, never applied without
  being shown, and always overridable from a combo populated from the live registry;
* **how many names are already damaged** -- with the count, and a choice about them that
  defaults to publishing because ``Name_en`` often survives where ``Name:ch`` did not;
* **that nothing here records where anyone looked** -- the one omission that cannot be
  reconstructed later and that silently poisons every model trained on the export.

The track is *first* and it is above the table rather than below it, which is a deliberate
inversion of how the other three are laid out. The damaged names and the missing survey
extent are properties of the data in the table, so they belong under it. The track is a
property of the whole publish and is not visible anywhere in the table at all -- and it was
selected minutes ago, in a different panel, possibly by somebody else who saved the
project. A warning at the bottom of a scrolled dialog is a warning nobody reads before
clicking the button they were already reaching for.

The dialog is a view. It reads a plan, renders it, and hands back
:class:`~.core.publish.LayerChoice` objects; it makes no network call, touches no layer and
decides nothing. Every decision it renders comes from :mod:`.core.publish`.
"""

from __future__ import annotations

from collections.abc import Sequence

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .core.fields import COMPLETENESS_EXHAUSTIVE, COMPLETENESS_PARTIAL
from .core.publish import (
    LayerChoice,
    LayerPlan,
    PublishPlan,
    PublishReport,
    SourceLayer,
    build_plan,
)
from .core.registry import ClassRegistry
from .core.routing import CollectionRoutes
from .core.tracks import Track

_COLUMNS = (
    "Publish",
    "Features",
    "Geometry",
    # Next to Geometry, because it is decided BY the geometry and by nothing else on this
    # screen. The class combo is the control people expect to change the destination, and
    # it does not; putting the two columns apart would invite exactly that reading.
    "Collection (by geometry)",
    "CRS",
    "Class (from the registry)",
    "Fields",
    "Surveyed this box?",
    "Notes",
)

COL_LAYER = 0
COL_FEATURES = 1
COL_GEOMETRY = 2
COL_COLLECTION = 3
COL_CRS = 4
COL_CLASS = 5
COL_FIELDS = 6
COL_EXTENT = 7
COL_NOTES = 8

#: Item data role carrying a layer id on a row.
LAYER_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class PublishDialog(QDialog):
    """Confirm what will be published, as what, and what is being left unsaid."""

    def __init__(
        self,
        sources: Sequence[SourceLayer],
        registry: ClassRegistry,
        parent: QWidget | None = None,
        track: Track | None = None,
        routes: CollectionRoutes | None = None,
    ) -> None:
        super().__init__(parent)
        # The track is in the WINDOW TITLE as well, because a modal dialog's title bar is
        # the one piece of chrome that stays visible while the person scrolls the table,
        # and because a screenshot in a support thread then carries it too.
        self.setWindowTitle(
            f"Publish local layers to track: {track.name}" if track else "Publish local layers"
        )
        self.setMinimumSize(1000, 560)

        self._sources = list(sources)
        self._registry = registry
        self._track = track
        self._routes = routes
        # Set before the table is built: building it connects signals that fire while the
        # rows are being populated, and a refresh mid-build would read half a table.
        self._refreshing = True

        layout = QVBoxLayout(self)
        self.track_label = QLabel("", self)
        self.track_label.setWordWrap(True)
        self.track_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.track_label)
        layout.addWidget(self._build_heading())

        self.table = self._build_table()
        layout.addWidget(self.table, 1)

        self.skip_damaged = QCheckBox(
            "Omit names that carry the truncation signature, rather than publishing them",
            self,
        )
        self.skip_damaged.setToolTip(
            "Off by default. An absent name is honest, but the English name often survives "
            "where the Chinese one did not, and dropping the Chinese name loses more than "
            "it protects. Correct the names at source if you can; this is the fallback."
        )
        self.skip_damaged.toggled.connect(self._refresh)
        layout.addWidget(self.skip_damaged)

        self.damage_label = QLabel("", self)
        self.damage_label.setWordWrap(True)
        self.damage_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.damage_label)

        self.coverage_label = QLabel("", self)
        self.coverage_label.setWordWrap(True)
        self.coverage_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.coverage_label)

        self.summary_label = QLabel("", self)
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.summary_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Publish")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._populate()
        self._refreshing = False
        self._refresh()

    # --- construction ---------------------------------------------------------

    def _build_heading(self) -> QLabel:
        heading = QLabel(
            "<b>Publishing sends these features to the backend as new labels.</b><br/>"
            "The server assigns each one an immutable <code>label_id</code>; nothing here "
            "invents an identity, and the source <code>id</code> column is not used. That "
            "also means a second publish cannot be recognised as a repeat - it creates a "
            "second copy. Check the class mapping before you confirm.",
            self,
        )
        heading.setWordWrap(True)
        heading.setTextFormat(Qt.TextFormat.RichText)
        return heading

    def _build_table(self) -> QTableWidget:
        table = QTableWidget(len(self._sources), len(_COLUMNS), self)
        table.setHorizontalHeaderLabels(list(_COLUMNS))
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        table.itemChanged.connect(self._on_item_changed)
        return table

    def _populate(self) -> None:
        """Render the opening plan: guessed classes, defaults already applied."""
        plan = build_plan(self._sources, self._registry, None, self._track, self._routes)
        for row, layer_plan in enumerate(plan):
            source = layer_plan.source

            name_item = QTableWidgetItem(source.name)
            name_item.setData(LAYER_ROLE, source.layer_id)
            name_item.setFlags(name_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            name_item.setCheckState(
                Qt.CheckState.Checked if layer_plan.choice.publish else Qt.CheckState.Unchecked
            )
            self.table.setItem(row, COL_LAYER, name_item)

            # "unknown", not "0": a provider that cannot count in advance answers -1, and
            # showing that as zero against a layer visibly full of features is a
            # contradiction with nothing on screen to resolve it.
            self.table.setItem(
                row,
                COL_FEATURES,
                _readonly(str(source.feature_count) if source.count_known else "unknown"),
            )
            self.table.setItem(row, COL_GEOMETRY, _readonly(source.geometry_type))
            # Filled once, not on every refresh: the destination follows from the layer's
            # geometry type, which nothing on this screen can change. A routing refusal is
            # a blocking problem and appears in the Notes column with the rest of them.
            self.table.setItem(row, COL_COLLECTION, _collection_cell(layer_plan))
            self.table.setItem(row, COL_CRS, _readonly(source.crs_authid))

            self.table.setCellWidget(row, COL_CLASS, self._class_combo(layer_plan.choice.class_id))

            self.table.setItem(row, COL_FIELDS, _readonly(""))

            self.table.setItem(row, COL_EXTENT, _readonly(""))
            self.table.setCellWidget(row, COL_EXTENT, self._extent_combo())

            self.table.setItem(row, COL_NOTES, _readonly(""))

        self.table.resizeColumnsToContents()

    def _extent_combo(self) -> QComboBox:
        """The survey-extent claim, as a value rather than a tick.

        ``completeness`` is the field on ``labeled_extent`` that changes what a training
        run does: only *exhaustive* licenses the export pipeline to treat unlabeled ground
        inside the polygon as negative, and everything outside a declared exhaustive extent
        is unknown rather than negative. A checkbox cannot express that, and when the tool
        picks the value itself the person clicking has not made the claim -- the tool has,
        in the direction that poisons every model trained from the result.

        So the choice is explicit, the default declares nothing, and the option that
        licenses negative sampling says out loud what it licenses.
        """
        combo = QComboBox(self.table)
        combo.addItem("no - declare nothing", "")
        combo.addItem("partial - do NOT sample as negative", COMPLETENESS_PARTIAL)
        combo.addItem("exhaustive - safe to sample as negative", COMPLETENESS_EXHAUSTIVE)
        combo.setItemData(
            0,
            "Nothing is written about where anyone looked. Honest, and unrecoverable "
            "later: the knowledge is in the surveyor's memory.",
            int(Qt.ItemDataRole.ToolTipRole),
        )
        combo.setItemData(
            1,
            "Records that this area was surveyed for this class, but not completely. "
            "Nothing inside it is treated as negative. The safe choice when the layer is "
            "the result of triage rather than a sweep.",
            int(Qt.ItemDataRole.ToolTipRole),
        )
        combo.setItemData(
            2,
            "Claims that EVERY feature of this class inside the layer's bounding box is "
            "labeled here. Every unlabeled thing inside that rectangle then becomes "
            "supervised background for the detector. Choose it only if the rectangle - "
            "not the features, the whole rectangle - really was swept.",
            int(Qt.ItemDataRole.ToolTipRole),
        )
        combo.setToolTip(
            "Whichever you choose, the extent written here is the layer's bounding box "
            "and names no imagery capture - the shapefiles do not record one. The export "
            "pipeline refuses an extent with no capture unless it is explicitly "
            "overridden, so both need correcting later; that is recorded in the row's "
            "caveat."
        )
        # Never pre-selected past the first entry. A bounding box is a claim about where
        # somebody looked, and only a human can make that claim honestly.
        combo.setCurrentIndex(0)
        combo.currentIndexChanged.connect(self._refresh)
        return combo

    def _class_combo(self, selected: str | None) -> QComboBox:
        """A picker over the live registry. No class name is compiled into this plugin."""
        combo = QComboBox(self.table)
        combo.addItem("- choose a class -", "")
        for label_class in self._registry.active():
            combo.addItem(label_class.display_name, label_class.class_id)
            combo.setItemData(
                combo.count() - 1,
                f"{label_class.class_id} - expects {label_class.geom_type} geometry",
                int(Qt.ItemDataRole.ToolTipRole),
            )
        index = combo.findData(selected or "")
        combo.setCurrentIndex(max(index, 0))
        combo.currentIndexChanged.connect(self._refresh)
        return combo

    # --- state ----------------------------------------------------------------

    def _row_choice(self, row: int) -> LayerChoice | None:
        item = self.table.item(row, COL_LAYER)
        if item is None:
            return None
        combo = self.table.cellWidget(row, COL_CLASS)
        extent = self.table.cellWidget(row, COL_EXTENT)
        class_id = str(combo.currentData() or "") if combo is not None else ""
        return LayerChoice(
            layer_id=str(item.data(LAYER_ROLE)),
            publish=item.checkState() == Qt.CheckState.Checked,
            class_id=class_id or None,
            extent_completeness=str(extent.currentData() or "") if extent is not None else "",
            skip_damaged_names=self.skip_damaged.isChecked(),
        )

    def choices(self) -> dict[str, LayerChoice]:
        """What the user decided, keyed by layer id."""
        decisions: dict[str, LayerChoice] = {}
        for row in range(self.table.rowCount()):
            choice = self._row_choice(row)
            if choice is not None:
                decisions[choice.layer_id] = choice
        return decisions

    def plan(self) -> PublishPlan:
        """The plan as currently configured."""
        return build_plan(self._sources, self._registry, self.choices(), self._track, self._routes)

    # --- rendering ------------------------------------------------------------

    def _on_item_changed(self, _item: QTableWidgetItem) -> None:
        self._refresh()

    def _refresh(self) -> None:
        """Re-render everything derived from the current choices.

        Guarded against re-entry: writing the notes column emits ``itemChanged``, which is
        the signal that called this.
        """
        if self._refreshing:
            return
        self._refreshing = True
        try:
            plan = self.plan()
            self._render_track(plan)
            self._render_rows(plan)
            self._render_damage(plan)
            self._render_coverage(plan)
            self._render_summary(plan)
        finally:
            self._refreshing = False

    def _render_track(self, plan: PublishPlan) -> None:
        """Name the dataset, above everything else, whether or not anything is wrong.

        Rendered unconditionally, which no other label on this screen is. The damaged-name
        and survey-extent warnings appear only when there is something to warn about, so a
        clean preview says nothing at all about where the features are going -- and "where"
        is the one decision on this screen that was made in another panel, at another time,
        possibly by the person who saved the project rather than the person clicking now.
        """
        problems = plan.track_problems()
        if problems:
            self.track_label.setText("<b>" + "<br/>".join(problems) + "</b>")
            return
        claim = plan.track_claim()
        elsewhere = plan.republished_elsewhere()
        if elsewhere:
            # Not a duplicate warning. Sending the same shapefile into a second track is
            # how a test dataset is populated, and calling it a duplicate would teach
            # people to click through the warning that catches the real one.
            claim += (
                "<br/><br/>"
                + ", ".join(sorted(p.source.name for p in elsewhere))
                + " was last published to a <i>different</i> track, so this is a first "
                "publish here rather than a second copy."
            )
        self.track_label.setText(f"<b>Track:</b> {claim}")

    def _render_rows(self, plan: PublishPlan) -> None:
        """Re-render the two cells that depend on which class the row is set to."""
        by_layer = {layer_plan.source.layer_id: layer_plan for layer_plan in plan}
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_LAYER)
            note_item = self.table.item(row, COL_NOTES)
            field_item = self.table.item(row, COL_FIELDS)
            if item is None or note_item is None:
                continue
            layer_plan = by_layer.get(str(item.data(LAYER_ROLE)))
            if layer_plan is None:
                continue
            lines = list(layer_plan.problems()) + list(layer_plan.notes())
            note_item.setText(" ".join(lines))
            note_item.setToolTip("\n\n".join(lines))
            if field_item is not None:
                # The counts fit in a cell; the mapping itself does not, and it is the
                # part worth reading. See LayerPlan.mapping_lines for why it is shown at
                # all rather than trusted.
                field_item.setText(layer_plan.mapping_summary())
                field_item.setToolTip(
                    "\n".join(layer_plan.mapping_lines()) or "Choose a class first."
                )

    def _render_damage(self, plan: PublishPlan) -> None:
        damaged = plan.damaged_name_count()
        if not damaged:
            self.damage_label.setText("")
            return
        fate = (
            "They will be <b>omitted</b>, so their absence is honest."
            if self.skip_damaged.isChecked()
            else "They will be <b>published exactly as they are</b> and become "
            "authoritative in the new system."
        )
        self.damage_label.setText(
            f"<b>Up to {damaged} feature(s) carry a name that has lost its final "
            "character.</b> Six of the seven source layers declare UTF-7 and the encoder "
            "never flushed its final escape run, so 数据中心 is stored as 数据中X8. The "
            "lost bits are gone; nothing can recover them from these files. "
            "<b>That number is an upper bound:</b> the residue of a cut escape run is "
            "arbitrary, so a two-character site designator after a Chinese character "
            "(数据中心B2) cannot be told apart from damage, and omitting it would destroy "
            f"an intact name. {fate}"
        )

    def _render_coverage(self, plan: PublishPlan) -> None:
        missing = plan.classes_without_extent()
        if not missing:
            self.coverage_label.setText("")
            return
        self.coverage_label.setText(
            "<b>No survey extent is being declared for: "
            + ", ".join(missing)
            + ".</b> This publish records WHAT was found, not WHERE ANYONE LOOKED. Ground "
            "outside a declared exhaustive extent is <i>unknown</i> to the export pipeline, "
            "never negative - and it cannot be reconstructed later, because the knowledge "
            "is in the surveyor's memory. Choose <i>exhaustive</i> only where the layer "
            "really is a complete sweep of its whole bounding box; <i>partial</i> records "
            "that you looked without licensing anything inside it to be sampled as "
            "background."
        )

    def _render_summary(self, plan: PublishPlan) -> None:
        problems = plan.problems()
        self.summary_label.setText(
            ("<b>" + " ".join(problems) + "</b><br/>" if problems else "") + plan.summary()
        )
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(bool(plan.selected()) and not problems)
            # The track goes in the button text, not only in the banner. This is the last
            # thing the eye lands on before an irreversible bulk write, and a button that
            # says which dataset it writes into is the cheapest possible check on the one
            # decision that was made somewhere else.
            ok.setText(f"Publish to {plan.track_name}" if plan.track_name else "Publish")


class PublishReportDialog(QDialog):
    """What actually happened, in enough detail to act on.

    Per-layer counts first, then the deduplicated issues, then the coverage warning. The
    ordering is the point: the counts say whether it worked, the issues say what to fix,
    and the warning says what is still missing even though everything worked.
    """

    def __init__(self, report: PublishReport, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            f"Publish results - track {report.track}" if report.track else "Publish results"
        )
        self.setMinimumSize(820, 460)

        layout = QVBoxLayout(self)

        heading = QLabel(f"<b>{report.summary()}</b>", self)
        heading.setWordWrap(True)
        heading.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(heading)

        detail = QPlainTextEdit(self)
        detail.setReadOnly(True)
        detail.setPlainText("\n".join(report.detail_lines()))
        layout.addWidget(detail, 1)

        warning = report.coverage_warning()
        if warning:
            coverage = QPlainTextEdit(self)
            coverage.setReadOnly(True)
            coverage.setPlainText(warning)
            coverage.setMaximumHeight(190)
            layout.addWidget(coverage)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


def _readonly(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemFlag.ItemIsEnabled)
    item.setToolTip(text)
    return item


def _collection_cell(layer_plan: LayerPlan) -> QTableWidgetItem:
    """Which collection this layer's features would be created in.

    On the screen rather than only in the log, because it is a decision the analyst never
    makes and can only check here. The features fan out across collections by geometry
    type; a route that turns out to be wrong is discovered as rows in the wrong place, and
    the server assigns identity, so nothing afterwards can find them again to move them.
    """
    if layer_plan.collection_id:
        item = _readonly(layer_plan.collection_id)
        item.setToolTip(
            f"{layer_plan.source.geometry_type or 'This geometry'} publishes into "
            f"{layer_plan.collection_id}. Each collection stores one geometry type; the "
            "class is an attribute of the feature, not of the collection."
        )
        return item
    # "-" rather than an empty cell: blank reads as "not filled in yet", and this is a
    # layer that has nowhere to go. The Notes column carries the reason.
    item = _readonly("-")
    item.setToolTip(layer_plan.routing_problem or "No collection resolved for this layer.")
    return item
