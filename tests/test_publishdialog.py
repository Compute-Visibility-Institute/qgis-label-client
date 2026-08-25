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


# --- the history track ------------------------------------------------------
#
# The dialog still cannot be constructed without a real Qt, so what is tested here is the
# same class of thing as the column constants: wiring a view can get wrong on its own,
# checked by calling the methods against a stand-in. The wiring matters because the track
# is the one decision on this screen that was made somewhere else -- in another panel,
# minutes earlier, possibly by whoever saved the project -- so if the preview silently
# stops carrying it, nothing on screen contradicts the person clicking Publish.

import inspect  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from snapshot_fixtures import ARCHIVED_TRACK, REGISTRY, TRACK  # noqa: E402

from qgis_label_client.core.publish import SourceLayer, build_plan  # noqa: E402


class _Recorder:
    """Captures what a label or a button was told to say."""

    def __init__(self) -> None:
        self.text = ""
        self.enabled = None

    def setText(self, value):  # noqa: N802 - Qt naming
        self.text = value

    def setEnabled(self, value):  # noqa: N802
        self.enabled = value


def _stand_in(track):
    """The three attributes PublishDialog.plan and the renderers actually read."""
    return SimpleNamespace(
        _sources=[SourceLayer(layer_id="c", name="Compounds", feature_count=10)],
        _registry=REGISTRY,
        _track=track,
        choices=lambda: {},
        track_label=_Recorder(),
        summary_label=_Recorder(),
        buttons=SimpleNamespace(button=lambda _standard: None),
    )


def test_the_dialog_takes_a_track():
    assert "track" in inspect.signature(publishdialog.PublishDialog.__init__).parameters


def test_the_preview_plans_against_the_track_it_was_given():
    # If this stops holding, the preview describes one dataset and the publish writes to
    # another, with nothing on screen saying so.
    plan = publishdialog.PublishDialog.plan(_stand_in(TRACK))
    assert plan.track is TRACK


def test_the_banner_names_the_dataset_even_on_a_clean_plan():
    """Rendered unconditionally, which no other label on this screen is.

    The damaged-name and survey-extent warnings appear only when there is something to
    warn about, so a clean preview would otherwise say nothing at all about where 1,246
    permanent features are going.
    """
    dialog = _stand_in(TRACK)
    publishdialog.PublishDialog._render_track(
        dialog, build_plan(dialog._sources, REGISTRY, None, TRACK)
    )
    assert TRACK.name in dialog.track_label.text
    assert "cannot be undone" in dialog.track_label.text


def test_the_banner_leads_with_the_problem_when_the_track_cannot_take_writes():
    dialog = _stand_in(ARCHIVED_TRACK)
    plan = build_plan(dialog._sources, REGISTRY, None, ARCHIVED_TRACK)
    publishdialog.PublishDialog._render_track(dialog, plan)
    assert "archived" in dialog.track_label.text
