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


@pytest.mark.parametrize(
    "connected,selected,advertised,visible",
    [
        (True, "dev", ["dev", "default"], True),
        (True, "default", ["dev", "default"], False),
        (True, "dev", ["default"], False),
        (True, "", ["dev", "default"], False),
        (False, "dev", ["dev", "default"], False),
    ],
)
def test_qa_requires_connected_explicit_advertised_development_environment(
    monkeypatch, connected, selected, advertised, visible
):
    dock = LabelClientDock(None)
    dock.qa_group = Mock()
    dock.coverage_button = Mock()
    monkeypatch.setattr(dock, "selected_track", lambda: selected)
    dock._tracks = [Track(name) for name in advertised]
    dock.set_connected(connected)
    dock.qa_group.setVisible.assert_called_with(visible)
    dock.coverage_button.setEnabled.assert_called_with(visible)
    dock.set_busy(True)
    dock.qa_group.setVisible.assert_called_with(visible)
    dock.coverage_button.setEnabled.assert_called_with(False)


def test_qa_hides_immediately_when_leaving_dev_or_disconnecting(monkeypatch):
    dock = LabelClientDock(None)
    dock.qa_group = Mock()
    dock.coverage_button = Mock()
    selected = ["dev"]
    monkeypatch.setattr(dock, "selected_track", lambda: selected[0])
    dock._tracks = [Track("dev"), Track("default")]
    dock.set_connected(True)
    dock.qa_group.setVisible.assert_called_with(True)
    selected[0] = "default"
    dock._emit_track_changed()
    dock.qa_group.setVisible.assert_called_with(False)
    dock.coverage_button.setEnabled.assert_called_with(False)
    selected[0] = "dev"
    dock._emit_track_changed()
    dock.qa_group.setVisible.assert_called_with(True)
    dock.set_connected(False)
    dock.qa_group.setVisible.assert_called_with(False)


def test_readers_can_pull_without_being_offered_push_and_busy_blocks_both():
    dock = LabelClientDock(None)
    dock.push_all_local_button = Mock()
    dock.pull_all_remote_button = Mock()
    dock.set_connected(True)
    dock.set_write_access(False)
    dock.push_all_local_button.setEnabled.assert_called_with(False)
    dock.pull_all_remote_button.setEnabled.assert_called_with(True)
    dock.set_busy(True)
    dock.push_all_local_button.setEnabled.assert_called_with(False)
    dock.pull_all_remote_button.setEnabled.assert_called_with(False)


@pytest.mark.parametrize("advertised,enabled", [([], False), ([Track("dev")], True)])
def test_disconnected_environment_selector_allows_recovery_but_not_while_busy(advertised, enabled):
    dock = LabelClientDock(None)
    dock.track_combo = Mock()
    dock._tracks = advertised

    dock.set_connected(False)

    dock.track_combo.setEnabled.assert_called_with(enabled)
    dock.set_busy(True)
    dock.track_combo.setEnabled.assert_called_with(False)
    dock.set_busy(False)
    dock.track_combo.setEnabled.assert_called_with(enabled)
