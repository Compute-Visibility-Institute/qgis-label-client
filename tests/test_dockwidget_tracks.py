"""Track selection shown in the panel must agree with the resolved publishing track."""

from unittest.mock import Mock

import pytest

from qgis_label_client.core.tracks import Track
from qgis_label_client.dockwidget import LabelClientDock


def test_history_track_is_the_last_panel_section(monkeypatch):
    sections = []
    for name in (
        "connection",
        "collections",
        "bootstrap",
        "asof",
        "recorded",
        "qa",
        "vocabulary",
        "reference",
        "track",
    ):
        method_name = f"_build_{name}_group"
        original = getattr(LabelClientDock, method_name)

        def build(self, parent, original=original, name=name):
            sections.append(name)
            return original(self, parent)

        monkeypatch.setattr(LabelClientDock, method_name, build)
    dock = LabelClientDock(None)
    assert sections[-2:] == ["bootstrap", "track"]
    assert sections.index("reference") < sections.index("bootstrap")
    assert "refresh_imagery_button" not in vars(dock)


@pytest.mark.parametrize(
    ("saved", "default_status", "declares_default", "expected_index"),
    [
        ("", "active", True, 1),
        ("dev", "active", True, 0),
        ("missing", "active", True, -1),
        ("", "active", False, -1),
        ("", "archived", True, -1),
    ],
)
def test_panel_selection_respects_default_saved_and_unavailable_tracks(
    saved, default_status, declares_default, expected_index
):
    dock = LabelClientDock(None)
    combo = Mock()
    combo.count.side_effect = [1, 2]
    combo.findData.side_effect = ["dev", "default"].index
    dock.track_combo = combo
    tracks = [
        Track(name="dev"),
        Track(name="default", is_default=declares_default, status=default_status),
    ]

    dock.set_tracks(tracks, saved)

    combo.setCurrentIndex.assert_called_once_with(expected_index)
    assert not dock._loading_tracks
