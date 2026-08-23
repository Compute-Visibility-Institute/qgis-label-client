"""The teardown stack. If this is wrong, unload() is wrong, and you get five buttons."""

from __future__ import annotations

from qgis_label_client.core.teardown import Teardown


def test_callbacks_run_in_reverse_order():
    # Attachment order is usually dependency order: create the dock, then dock it.
    order: list[str] = []
    teardown = Teardown()
    teardown.add("first", lambda: order.append("first"))
    teardown.add("second", lambda: order.append("second"))
    teardown.run()
    assert order == ["second", "first"]


def test_a_failing_callback_does_not_strand_the_others():
    # A half-completed teardown is exactly the state that leaves a toolbar button behind.
    done: list[str] = []

    def boom() -> None:
        raise RuntimeError("detach failed")

    teardown = Teardown()
    teardown.add("outer", lambda: done.append("outer"))
    teardown.add("boom", boom)
    teardown.add("inner", lambda: done.append("inner"))

    failures = teardown.run()
    assert done == ["inner", "outer"]
    assert [f.label for f in failures] == ["boom"]
    assert isinstance(failures[0].error, RuntimeError)


def test_running_twice_is_a_no_op():
    # QGIS calls unload() more than once in some reload paths.
    calls: list[int] = []
    teardown = Teardown()
    teardown.add("one", lambda: calls.append(1))
    teardown.run()
    teardown.run()
    assert calls == [1]
    assert len(teardown) == 0


def test_labels_are_reported_in_registration_order():
    teardown = Teardown()
    teardown.add("a", lambda: None)
    teardown.add("b", lambda: None)
    assert teardown.labels == ["a", "b"]
    assert len(teardown) == 2
