"""Main-thread activity ownership, independent of QGIS and credential state."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ActivityState:
    count: int
    progress: float | None = None

    @property
    def busy(self) -> bool:
        return self.count > 0


class ActivityToken:
    """Only this operation may finish or update its activity; late updates are ignored."""

    def __init__(self, registry: ActivityRegistry, key: object) -> None:
        self._registry = registry
        self._key = key

    def progress(self, percent: float) -> None:
        self._registry._progress(self._key, percent)

    def close(self) -> None:
        self._registry._close(self._key)


class ActivityRegistry:
    """Derive busy/progress from live operations, never from the last callback to finish.

    With multiple operations there is no meaningful combined percentage: show an
    indeterminate indicator, then restore the remaining operation's progress.
    """

    def __init__(self, changed: Callable[[ActivityState], None]) -> None:
        self._changed = changed
        self._operations: dict[object, float | None] = {}

    @property
    def state(self) -> ActivityState:
        progress = next(iter(self._operations.values())) if len(self._operations) == 1 else None
        return ActivityState(len(self._operations), progress)

    def begin(self) -> ActivityToken:
        key = object()
        self._operations[key] = None
        self._changed(self.state)
        return ActivityToken(self, key)

    def clear(self) -> None:
        self._operations.clear()
        self._changed(self.state)

    def _progress(self, key: object, percent: float) -> None:
        if key in self._operations and math.isfinite(percent):
            self._operations[key] = max(0, min(100, percent))
            self._changed(self.state)

    def _close(self, key: object) -> None:
        if key in self._operations:
            del self._operations[key]
            self._changed(self.state)
