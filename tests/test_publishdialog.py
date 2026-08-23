"""What can be checked about the preview dialog without a running Qt.

Not much, and deliberately so: the dialog is a view, every decision it renders comes from
:mod:`~qgis_label_client.core.publish`, and that is where the tests are. What is checked
here is the one thing a view can get wrong on its own -- addressing the wrong cell. The
column constants and the header labels are two lists that have to agree, and when they
drift the symptom is a checkbox written into the notes column, which no unit test of the
plan would ever notice.
"""

from __future__ import annotations

from qgis_label_client import publishdialog

COLUMN_CONSTANTS = (
    publishdialog.COL_LAYER,
    publishdialog.COL_FEATURES,
    publishdialog.COL_GEOMETRY,
    publishdialog.COL_CRS,
    publishdialog.COL_CLASS,
    publishdialog.COL_FIELDS,
    publishdialog.COL_EXTENT,
    publishdialog.COL_NOTES,
)


def test_every_column_has_a_constant_and_every_constant_a_column():
    assert sorted(COLUMN_CONSTANTS) == list(range(len(publishdialog._COLUMNS)))


def test_the_column_constants_are_distinct():
    assert len(set(COLUMN_CONSTANTS)) == len(COLUMN_CONSTANTS)


def test_the_notes_column_is_last():
    # The table stretches its last section, and the notes are the only cell whose content
    # has no natural width.
    assert len(publishdialog._COLUMNS) - 1 == publishdialog.COL_NOTES
