"""Overlapping real controller/task callbacks must own their own busy state."""

import pytest

from qgis_label_client.core.activity import ActivityRegistry
from qgis_label_client.plugin import LabelClientPlugin
from qgis_label_client.tasks import TaskRunner


@pytest.fixture
def plugin(fake_iface):
    instance = LabelClientPlugin(fake_iface)
    instance.initGui()
    yield instance
    instance.unload()


@pytest.mark.parametrize("first_fails", [False, True])
def test_finishing_one_task_does_not_enable_controls_for_another(plugin, first_fails):
    first = plugin.tasks.run("first", lambda _: None, on_error=plugin._report)
    second = plugin.tasks.run("second", lambda _: None)
    assert plugin.dock._busy
    first.finished(not first_fails)
    assert plugin.dock._busy
    assert plugin.activities.state.count == 1
    second.finished(True)
    assert not plugin.dock._busy


def test_reporting_an_unrelated_error_does_not_end_active_work(plugin):
    task = plugin.tasks.run("pending", lambda _: None)
    plugin._report("A separate command was refused")
    assert plugin.dock._busy
    task.finished(True)
    assert not plugin.dock._busy


def test_progress_restores_surviving_operation_and_ignores_late_updates():
    seen = []
    activities = ActivityRegistry(seen.append)
    runner = TaskRunner(activities)
    first = runner.run("first", lambda _: None)
    first.progressChanged.emit(25)
    assert activities.state.progress == 25
    second = runner.run("second", lambda _: None)
    second.progressChanged.emit(90)
    assert activities.state.progress is None
    second.finished(True)
    assert activities.state.progress == 25
    second.progressChanged.emit(100)
    assert activities.state.progress == 25
    first.finished(True)
    count = len(seen)
    first.progressChanged.emit(50)
    assert len(seen) == count
    assert not activities.state.busy


def test_cancelled_read_releases_activity_without_delivering_its_result(plugin):
    seen = []
    task = plugin.tasks.run("read", lambda _: "discard", on_success=seen.append)
    task.run()
    task.cancel()
    task.finished(True)
    assert seen == []
    assert not plugin.dock._busy


def test_queued_termination_releases_activity_even_without_finished_callback(plugin):
    task = plugin.tasks.run("queued", lambda _: None)
    task.cancel()
    task.taskTerminated.emit()
    assert not plugin.dock._busy
    task.finished(False)
    assert not plugin.dock._busy


def test_callback_exception_still_releases_only_its_own_activity(plugin):
    def broken_callback(_):
        raise ValueError("UI failed")

    broken = plugin.tasks.run("broken", lambda _: None, on_success=broken_callback)
    pending = plugin.tasks.run("pending", lambda _: None)
    with pytest.raises(ValueError, match="UI failed"):
        broken.finished(True)
    assert plugin.activities.state.count == 1
    pending.finished(True)
    assert not plugin.dock._busy


def test_followup_started_from_completion_keeps_busy_without_a_gap():
    states = []
    runner = TaskRunner(ActivityRegistry(states.append))
    followups = []
    first = runner.run(
        "read",
        lambda _: None,
        on_error=lambda _: followups.append(runner.run("renew", lambda _: None)),
    )
    first.finished(False)
    assert all(state.busy for state in states)
    followups[0].finished(True)
    assert not states[-1].busy


def test_background_access_check_does_not_own_foreground_progress(plugin):
    background = plugin.tasks.run("access", lambda _: None, busy=False)
    assert not plugin.dock._busy
    foreground = plugin.tasks.run("publish", lambda _: None)
    foreground.progressChanged.emit(40)
    background.finished(True)
    assert plugin.activities.state.progress == 40
    foreground.finished(True)


def test_unload_invalidates_tokens_and_late_callbacks(plugin):
    seen = []
    task = plugin.tasks.run(
        "publish", lambda _: None, on_success=seen.append, deliver_when_cancelled=True
    )
    plugin.unload()
    assert not plugin.activities.state.busy
    task.progressChanged.emit(90)
    task.finished(True)
    assert seen == []
    assert not plugin.activities.state.busy
