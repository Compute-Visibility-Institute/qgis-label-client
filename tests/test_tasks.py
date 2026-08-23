"""The QgsTask wrapper: the three traps, each with a test."""

from __future__ import annotations

from qgis_label_client.tasks import FunctionTask, TaskRunner


def test_success_is_delivered_to_the_callback():
    seen: list[object] = []
    task = FunctionTask("work", lambda feedback: 42, on_success=seen.append)
    assert task.run() is True
    task.finished(True)
    assert seen == [42]


def test_an_exception_in_run_is_reported_rather_than_swallowed():
    # Trap 1: QGIS discards exceptions escaping run(). Without this catch the symptom is
    # a button that does nothing, with no traceback anywhere.
    errors: list[str] = []

    def boom(feedback):
        raise ValueError("no good")

    task = FunctionTask("work", boom, on_error=errors.append)
    assert task.run() is False
    task.finished(False)
    assert errors and "ValueError: no good" in errors[0]


def test_the_traceback_is_captured_on_the_worker_thread():
    def boom(feedback):
        raise KeyError("k")

    task = FunctionTask("work", boom)
    task.run()
    assert "KeyError" in task._traceback
    assert "boom" in task._traceback


def test_cancelling_stops_the_callbacks_firing():
    seen: list[object] = []
    task = FunctionTask("work", lambda feedback: 1, on_success=seen.append)
    task.run()
    task.cancel()
    task.finished(True)
    assert seen == []


def test_a_cancelled_write_still_reports_what_it_managed_to_do():
    # Cancelling a read discards a result nobody wanted. Cancelling a write cannot: some
    # of it already happened on a server, and the summary is the only record of which
    # part. The bootstrap publish opts in for exactly that reason.
    seen: list[object] = []
    task = FunctionTask(
        "publish", lambda feedback: "partial", on_success=seen.append, deliver_when_cancelled=True
    )
    task.run()
    task.cancel()
    task.finished(True)
    assert seen == ["partial"]


def test_opting_in_does_not_defeat_detach():
    # detach(), not the cancellation guard, is what stops a task calling into a widget
    # that unload() has destroyed.
    seen: list[object] = []
    runner = TaskRunner()
    task = runner.run(
        "publish", lambda feedback: 1, on_success=seen.append, deliver_when_cancelled=True
    )
    runner.shutdown()
    task.finished(True)
    assert seen == []


def test_cancel_also_cancels_the_feedback_so_the_socket_aborts():
    task = FunctionTask("work", lambda feedback: None)
    assert task._feedback.isCanceled() is False
    task.cancel()
    assert task._feedback.isCanceled() is True


def test_the_work_callable_receives_the_feedback_handle():
    received: list[object] = []
    task = FunctionTask("work", lambda feedback: received.append(feedback))
    task.run()
    assert received and received[0] is task._feedback


def test_runner_holds_a_reference_so_the_task_is_not_collected_mid_flight():
    # Trap 2: a task with no Python reference is garbage collected and the request never
    # completes.
    runner = TaskRunner()
    runner.run("work", lambda feedback: None)
    assert len(runner) == 1


def test_shutdown_cancels_and_detaches():
    runner = TaskRunner()
    seen: list[object] = []
    task = runner.run("work", lambda feedback: 1, on_success=seen.append)
    runner.shutdown()

    assert task.isCanceled()
    assert len(runner) == 0
    # Detached: even a completion that slips through cannot call into a destroyed widget.
    task.finished(True)
    assert seen == []
