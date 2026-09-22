"""Temporal pickers change a view only through their explicit action buttons."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

from qgis_label_client import dockwidget


def _day(year, month, day):
    return SimpleNamespace(year=lambda: year, month=lambda: month, day=lambda: day)


def _date_picker(monkeypatch, dock):
    selected = [_day(2026, 9, 1)]
    picker = Mock()
    picker.date.side_effect = lambda: selected[0]
    picker.setDate.side_effect = lambda value: selected.__setitem__(0, value)
    monkeypatch.setattr(dockwidget, "QDate", _day)
    dock.asof_date = picker
    return selected


def test_valid_date_requires_apply_and_clear_emits_an_unpinned_view(monkeypatch):
    dock = dockwidget.LabelClientDock(None)
    selected = _date_picker(monkeypatch, dock)
    applied = []
    dock.asOfApplied.connect(lambda: applied.append(dock.as_of()))
    assert dock.as_of() is None
    selected[0] = _day(2026, 9, 2)
    assert dock.as_of() is None
    assert applied == []
    dock._apply_asof_date()
    assert applied == [date(2026, 9, 2)]
    dock._clear_asof_date()
    assert applied == [date(2026, 9, 2), None]
    assert dock.as_of() is None


def test_restoring_applied_date_after_rejected_change_does_not_emit_another_request(monkeypatch):
    dock = dockwidget.LabelClientDock(None)
    _date_picker(monkeypatch, dock)
    applied = Mock()
    dock.asOfApplied.connect(applied)
    dock.set_as_of(date(2026, 8, 12))
    assert dock.as_of() == date(2026, 8, 12)
    applied.assert_not_called()
    dock._clear_asof_date()
    assert dock.as_of() is None
    dock.set_as_of(date(2026, 8, 12))
    assert dock.as_of() == date(2026, 8, 12)
    assert applied.call_count == 1
    dock.set_as_of(None)
    assert dock.as_of() is None
    assert applied.call_count == 1


def test_historical_date_is_always_readable_but_default_restore_does_not_add_a_layer(monkeypatch):
    dock = dockwidget.LabelClientDock(None)

    def qt_datetime(moment):
        return SimpleNamespace(
            date=lambda: _day(moment.year, moment.month, moment.day),
            time=lambda: SimpleNamespace(
                hour=lambda: moment.hour, minute=lambda: moment.minute, second=lambda: moment.second
            ),
        )

    monkeypatch.setattr(dockwidget, "_as_qdatetime", qt_datetime)
    selected = [None]
    dock.recorded_datetime = Mock()
    dock.recorded_datetime.setDateTime.side_effect = lambda value: selected.__setitem__(0, value)
    dock.recorded_datetime.dateTime.side_effect = lambda: selected[0]
    requested = []
    dock.recordedViewRequested.connect(requested.append)
    dock.set_recorded_default("2026-09-01T12:13:14Z")
    assert dock.recorded_at() == "2026-09-01T12:13:14Z"
    assert requested == []
    assert dock.as_of() is None
    dock._emit_recorded_view()
    assert requested == ["2026-09-01T12:13:14Z"]


def test_temporal_pickers_need_no_arming_and_actions_wait_for_connection():
    dock = dockwidget.LabelClientDock(None)
    dock.asof_date = Mock()
    dock.recorded_datetime = Mock()
    dock.add_recorded_button = Mock()
    dock.set_connected(False)
    dock.asof_date.setEnabled.assert_called_with(True)
    dock.recorded_datetime.setEnabled.assert_called_with(True)
    dock.add_recorded_button.setEnabled.assert_called_with(False)
    dock.set_connected(True)
    dock.add_recorded_button.setEnabled.assert_called_with(True)
    dock.set_busy(True)
    dock.asof_date.setEnabled.assert_called_with(False)
    dock.recorded_datetime.setEnabled.assert_called_with(False)
    dock.add_recorded_button.setEnabled.assert_called_with(False)
