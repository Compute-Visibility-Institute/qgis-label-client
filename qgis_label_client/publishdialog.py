"""The preview that stands between 1,246 local features and an empty backend.

WHY A DIALOG AND NOT A BUTTON

This is a bootstrap into a system that has nothing in it. The server assigns identity, so
the plugin cannot recognise what it has already sent; a wrong class mapping cannot be
undone by re-running, and a name published with its final character missing becomes the
authoritative name. Every one of those is cheap to prevent here and expensive to unpick
afterwards, which is the entire argument for making the user look at a table first.

THE THREE THINGS THIS SCREEN EXISTS TO SAY OUT LOUD

* **which class each layer becomes** -- guessed from the layer name, never applied without
  being shown, and always overridable from a combo populated from the live registry;
* **how many names are already damaged** -- with the count, and a choice about them that
  defaults to publishing because ``Name_en`` often survives where ``Name:ch`` did not;
* **that nothing here records where anyone looked** -- the one omission that cannot be
  reconstructed later and that silently poisons every model trained on the export.

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

from .core.publish import LayerChoice, PublishPlan, PublishReport, SourceLayer, build_plan
from .core.registry import ClassRegistry

_COLUMNS = (
    "Publish",
    "Features",
    "Geometry",
    "CRS",
    "Class (from the registry)",
    "Declare survey extent",
    "Notes",
)

COL_LAYER = 0
COL_FEATURES = 1
COL_GEOMETRY = 2
COL_CRS = 3
COL_CLASS = 4
COL_EXTENT = 5
COL_NOTES = 6

#: Item data role carrying a layer id on a row.
LAYER_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class PublishDialog(QDialog):
    """Confirm what will be published, as what, and what is being left unsaid."""

    def __init__(
        self,
        sources: Sequence[SourceLayer],
        registry: ClassRegistry,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Publish local layers")
        self.setMinimumSize(1000, 520)

        self._sources = list(sources)
        self._registry = registry
        # Set before the table is built: building it connects signals that fire while the
        # rows are being populated, and a refresh mid-build would read half a table.
        self._refreshing = True

        layout = QVBoxLayout(self)
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
        plan = build_plan(self._sources, self._registry)
        for row, layer_plan in enumerate(plan):
            source = layer_plan.source

            name_item = QTableWidgetItem(source.name)
            name_item.setData(LAYER_ROLE, source.layer_id)
            name_item.setFlags(name_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            name_item.setCheckState(
                Qt.CheckState.Checked if layer_plan.choice.publish else Qt.CheckState.Unchecked
            )
            self.table.setItem(row, COL_LAYER, name_item)

            self.table.setItem(row, COL_FEATURES, _readonly(str(source.feature_count)))
            self.table.setItem(row, COL_GEOMETRY, _readonly(source.geometry_type))
            self.table.setItem(row, COL_CRS, _readonly(source.crs_authid))

            self.table.setCellWidget(row, COL_CLASS, self._class_combo(layer_plan.choice.class_id))

            extent_item = QTableWidgetItem("")
            extent_item.setFlags(
                (extent_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                & ~Qt.ItemFlag.ItemIsSelectable
            )
            # Never pre-ticked. A bounding box is a claim about where somebody looked, and
            # only a human can make that claim honestly.
            extent_item.setCheckState(Qt.CheckState.Unchecked)
            extent_item.setToolTip(
                "Declares a labeled_extent for this class from the layer's bounding box, "
                "marked exhaustive with a caveat recording that it is a bounding box. "
                "Tick it only if this layer really is a complete sweep of that rectangle."
            )
            self.table.setItem(row, COL_EXTENT, extent_item)

            self.table.setItem(row, COL_NOTES, _readonly(""))

        self.table.resizeColumnsToContents()

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
        extent = self.table.item(row, COL_EXTENT)
        class_id = str(combo.currentData() or "") if combo is not None else ""
        return LayerChoice(
            layer_id=str(item.data(LAYER_ROLE)),
            publish=item.checkState() == Qt.CheckState.Checked,
            class_id=class_id or None,
            declare_extent=extent is not None and extent.checkState() == Qt.CheckState.Checked,
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
        return build_plan(self._sources, self._registry, self.choices())

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
            self._render_notes(plan)
            self._render_damage(plan)
            self._render_coverage(plan)
            self._render_summary(plan)
        finally:
            self._refreshing = False

    def _render_notes(self, plan: PublishPlan) -> None:
        by_layer = {layer_plan.source.layer_id: layer_plan for layer_plan in plan}
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_LAYER)
            note_item = self.table.item(row, COL_NOTES)
            if item is None or note_item is None:
                continue
            layer_plan = by_layer.get(str(item.data(LAYER_ROLE)))
            if layer_plan is None:
                continue
            lines = list(layer_plan.problems()) + list(layer_plan.notes())
            note_item.setText(" ".join(lines))
            note_item.setToolTip("\n\n".join(lines))

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
            f"<b>{damaged} feature(s) carry a name that has lost its final character.</b> "
            "Six of the seven source layers declare UTF-7 and the encoder never flushed "
            "its final escape run, so 数据中心 is stored as 数据中X8. The lost bits are "
            f"gone; nothing can recover them from these files. {fate}"
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
            "is in the surveyor's memory. Tick the extent box only where the layer really "
            "is a complete sweep."
        )

    def _render_summary(self, plan: PublishPlan) -> None:
        problems = plan.problems()
        self.summary_label.setText(
            ("<b>" + " ".join(problems) + "</b><br/>" if problems else "") + plan.summary()
        )
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(bool(plan.selected()) and not problems)


class PublishReportDialog(QDialog):
    """What actually happened, in enough detail to act on.

    Per-layer counts first, then the deduplicated issues, then the coverage warning. The
    ordering is the point: the counts say whether it worked, the issues say what to fix,
    and the warning says what is still missing even though everything worked.
    """

    def __init__(self, report: PublishReport, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Publish results")
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
