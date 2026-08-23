"""A read-only view of one label's edit history.

The rows come from the append-only ``label_history`` table, written by a database
trigger. That is worth knowing while reading this dialog: the history is not something
the API assembles and could get wrong, and it is not something a client can skip by
writing directly to the database. It is the one record nothing routes around.
"""

from __future__ import annotations

from collections.abc import Sequence

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .core.history import HistoryEntry

_COLUMNS = (
    "Recorded from",
    "Recorded to",
    "Operation",
    "Changed",
    "Editor",
    "Reason",
    "Name at the time",
)


class HistoryDialog(QDialog):
    """Tabular history for a single ``label_id``."""

    def __init__(
        self,
        label_id: str,
        entries: Sequence[HistoryEntry],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Label history")
        self.setMinimumSize(820, 400)

        layout = QVBoxLayout(self)

        heading = QLabel(
            f"<b>{len(entries)}</b> recorded belief(s) for label "
            f"<code>{label_id}</code>.<br/>"
            "<i>Recorded to</i> is empty for the belief currently in force. "
            "This is transaction time - when we believed it - not when it was true on "
            "the ground."
        )
        heading.setWordWrap(True)
        heading.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(heading)

        table = QTableWidget(len(entries), len(_COLUMNS), self)
        table.setHorizontalHeaderLabels(list(_COLUMNS))
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        for row, entry in enumerate(entries):
            values = (
                entry.recorded_from or "",
                entry.recorded_to or "(current belief)",
                entry.operation,
                entry.changed_summary(),
                entry.actor or "",
                entry.reason or "",
                entry.name_summary(),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                table.setItem(row, column, item)

        table.resizeColumnsToContents()
        layout.addWidget(table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
