"""A registry of undo callbacks, so ``unload()`` cannot forget anything.

WHY THIS EXISTS

QGIS cleans up nothing on plugin unload. Every ``addToolBarIcon``, ``addPluginToMenu``,
``addDockWidget`` and ``signal.connect`` has to be matched by hand in ``unload()``, and
the failure mode is quiet: reload five times with Plugin Reloader and you have five
toolbar buttons, five dock panels and four stale signal connections firing into dead
objects.

The fix is structural rather than disciplinary. Nothing is attached without registering
its detach in the same statement, so the two cannot drift apart in a later edit. That
turns "did you remember to remove it?" from a code-review question into an invariant.

Two details that matter:

* callbacks run in **reverse** order, because attachment order is usually dependency
  order (create the dock, then add it to the window);
* a callback that raises is recorded and the rest still run. A half-completed teardown
  is exactly the state that leaves a button behind, so one bad detach must not strand
  the others.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TeardownFailure:
    """One detach callback that raised, kept so the caller can log it."""

    label: str
    error: BaseException


@dataclass
class Teardown:
    """Ordered stack of detach callbacks."""

    _entries: list[tuple[str, Callable[[], None]]] = field(default_factory=list)

    def add(self, label: str, callback: Callable[[], None]) -> None:
        """Register `callback` to run on :meth:`run`, newest first."""
        self._entries.append((label, callback))

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def labels(self) -> list[str]:
        """Registered labels, in the order they were added. For diagnostics."""
        return [label for label, _ in self._entries]

    def run(self) -> list[TeardownFailure]:
        """Run every callback in reverse order and empty the stack.

        The stack is emptied even when callbacks fail, so a second ``unload()`` (QGIS
        does call it more than once in some reload paths) is a no-op rather than a
        second round of exceptions.
        """
        failures: list[TeardownFailure] = []
        entries, self._entries = self._entries, []
        for label, callback in reversed(entries):
            try:
                callback()
            except BaseException as exc:  # noqa: BLE001 - see module docstring
                failures.append(TeardownFailure(label=label, error=exc))
        return failures
