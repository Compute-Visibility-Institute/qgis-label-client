"""Background work, with the three ``QgsTask`` traps closed by construction.

THE TRAPS

1. **An exception escaping ``run()`` is swallowed silently.** No traceback, no message
   bar, no log line -- the task simply reports failure and the UI does nothing. So
   :class:`FunctionTask` catches everything, keeps the formatted traceback, and hands it
   to an error callback on the main thread.
2. **A task with no Python reference is garbage collected mid-flight**, and the request
   never completes. :class:`TaskRunner` holds one until the task finishes.
3. **``run()`` is a worker thread.** No Qt widgets, no ``iface``, no ``QgsProject``. Only
   the callbacks below are back on the main thread, and the ``work`` callable is given
   nothing but a ``QgsFeedback`` so there is nothing thread-unsafe in scope to misuse.

The runner also has a :meth:`TaskRunner.shutdown`, called from ``unload()``. A task that
outlives the plugin and then calls back into a destroyed dock widget is a crash, not a
warning, so shutdown both cancels the tasks and detaches their callbacks.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from qgis.core import QgsApplication, QgsFeedback, QgsTask

from .log import log, log_error

#: Work runs on a thread and receives only a cancellation handle.
WorkCallable = Callable[[QgsFeedback], Any]
SuccessCallable = Callable[[Any], None]
ErrorCallable = Callable[[str], None]


class FunctionTask(QgsTask):
    """Run a callable on a worker thread and report back on the main thread."""

    def __init__(
        self,
        description: str,
        work: WorkCallable,
        on_success: SuccessCallable | None = None,
        on_error: ErrorCallable | None = None,
    ) -> None:
        super().__init__(description, QgsTask.Flag.CanCancel)
        self._work = work
        self._on_success = on_success
        self._on_error = on_error
        self._feedback = QgsFeedback()
        self._result: Any = None
        self._message: str = ""
        self._traceback: str = ""

    # --- worker thread ---------------------------------------------------------

    def run(self) -> bool:
        try:
            self._result = self._work(self._feedback)
            return True
        except BaseException as exc:  # noqa: BLE001 - see module docstring, trap 1
            # Formatting the traceback here, on the worker thread, is deliberate: the
            # exception object does not survive the thread boundary usefully but a string
            # does, and losing it is how a plugin becomes "the button does nothing".
            self._message = f"{type(exc).__name__}: {exc}"
            self._traceback = traceback.format_exc()
            return False

    def cancel(self) -> None:
        # Cancel the feedback as well as the task, so a blocking network request aborts
        # its socket instead of running to completion inside a cancelled task.
        self._feedback.cancel()
        super().cancel()

    # --- main thread -----------------------------------------------------------

    def finished(self, result: bool) -> None:
        if self.isCanceled():
            return
        if result:
            if self._on_success is not None:
                self._on_success(self._result)
            return
        if self._traceback:
            log_error(f"{self.description()} failed\n{self._traceback}")
        if self._on_error is not None:
            self._on_error(self._message or f"{self.description()} failed.")

    def detach(self) -> None:
        """Drop the callbacks so a task in flight cannot call into a destroyed widget."""
        self._on_success = None
        self._on_error = None


class TaskRunner:
    """Owns the strong references that keep tasks alive, and cancels them on unload."""

    def __init__(self) -> None:
        self._tasks: list[FunctionTask] = []

    def run(
        self,
        description: str,
        work: WorkCallable,
        on_success: SuccessCallable | None = None,
        on_error: ErrorCallable | None = None,
    ) -> FunctionTask:
        task = FunctionTask(description, work, on_success, on_error)

        def _release() -> None:
            # Runs on the main thread via the task's own signals, so mutating the list
            # here needs no lock.
            if task in self._tasks:
                self._tasks.remove(task)

        task.taskCompleted.connect(_release)
        task.taskTerminated.connect(_release)

        # Append BEFORE handing the task to the manager: trap 2 is a race, and the window
        # where the only reference is the local variable is exactly where it bites.
        self._tasks.append(task)
        QgsApplication.taskManager().addTask(task)
        return task

    def shutdown(self) -> None:
        """Cancel every in-flight task and detach its callbacks. Call from ``unload()``."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.detach()
            task.cancel()
        if tasks:
            log(f"Cancelled {len(tasks)} in-flight task(s) on unload.")

    def __len__(self) -> int:
        return len(self._tasks)
