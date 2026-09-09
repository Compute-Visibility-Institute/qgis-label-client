"""Main-thread session ownership, independent of Qt, credentials and network I/O."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ReadContext:
    generation: int
    url: str
    email: str
    configs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ReplayResult:
    completed: int = 0
    failed: int = 0
    cancelled: int = 0


class SessionCoordinator:
    """Own renewal and queued actions until their session ends.

    Advancing the session cancels queued work and invalidates captured reads. Token
    renewal keeps the generation: existing auth-config ids are rewritten in place.
    The Qt adapter still owns its timer and the actual credential storage.
    """

    def __init__(self) -> None:
        self.generation = 0
        self.refreshing = False
        self.repairing = False
        self._replay_context: tuple[int, int] | None = None
        self._cancellation = 0
        self._deferred: list[Callable[[], None]] = []

    @property
    def pending_count(self) -> int:
        return len(self._deferred)

    @property
    def resuming(self) -> bool:
        return self._replay_context == (self.generation, self._cancellation)

    def defer(self, action: Callable[[], None]) -> None:
        self._deferred.append(action)

    def cancel_pending(self) -> int:
        # A replay batch is detached from the queue. Its remaining actions must
        # observe cancellation too, without invalidating completed read identity.
        self._cancellation += 1
        count = len(self._deferred)
        self._deferred.clear()
        return count

    def end_refresh(self, *, discard_pending: bool = False) -> None:
        self.refreshing = False
        if discard_pending:
            self.repairing = False
            self.cancel_pending()

    def advance(self) -> None:
        self.generation += 1
        self.end_refresh(discard_pending=True)

    def capture_read(self, url: str, email: str, configs: Mapping[str, str]) -> ReadContext:
        return ReadContext(self.generation, url, email, tuple(configs.items()))

    def allows_read(
        self, context: ReadContext, url: str, email: str, configs: Mapping[str, str]
    ) -> bool:
        # Renewal may discover new tracks, but cannot replace the ids captured by
        # a worker. Expiry is deliberately absent; permission checks are stricter.
        return (
            context.generation == self.generation
            and context.url == url
            and context.email == email
            and all(configs.get(track) == config for track, config in context.configs)
        )

    def resume(self, on_error: Callable[[Exception], None]) -> ReplayResult:
        if self.resuming:
            return ReplayResult()
        pending, self._deferred = self._deferred, []
        generation = self.generation
        cancellation = self._cancellation
        previous = self._replay_context
        self._replay_context = (generation, cancellation)
        completed = failed = 0
        try:
            for index, action in enumerate(pending):
                if generation != self.generation or cancellation != self._cancellation:
                    return ReplayResult(completed, failed, len(pending) - index)
                try:
                    action()
                except Exception as exc:  # noqa: BLE001 - one failed UI action must not drop its peers
                    failed += 1
                    on_error(exc)
                else:
                    completed += 1
        finally:
            self._replay_context = previous
        return ReplayResult(completed, failed)
