"""Initialize bootstrap styles without replacing an existing custom class style."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .errors import BackendError
from .registry import ClassRegistry
from .styleproposals import StyleProposal

PATH = "v1/classes/{classId}/bootstrap-style"


def supported(document: object) -> bool:
    if not isinstance(document, Mapping):
        return False
    block = document.get("bootstrap_style")
    return isinstance(block, Mapping) and block.get("path") in (PATH, "/" + PATH)


def problems(proposals: Sequence[StyleProposal], available: bool) -> tuple[str, ...]:
    conflicts = sorted({p.class_id for p in proposals if p.status == "conflict"})
    result = []
    if conflicts:
        result.append(
            "Different included styles target the same class: "
            + ", ".join(conflicts)
            + ". Keep Include checked for only one style per class."
        )
    if not available and any(p.status in ("proposed", "unchanged") for p in proposals):
        result.append(
            "This connection does not support saving bootstrap styles. Reconnect after "
            "the server is updated, or uncheck Include to upload labels without their styles."
        )
    return tuple(result)


@dataclass(frozen=True)
class StyleResult:
    class_id: str
    status: str
    style: Mapping[str, Any]

    def describe(self) -> str:
        labels = {
            "initialized": "Style saved to the server",
            "unchanged": "Style already matches the server",
            "preserved": "Existing server style preserved; local style was not applied",
        }
        return f"{self.class_id}: {labels[self.status]}."


def parse_result(payload: object, class_id: str) -> StyleResult:
    if (
        not isinstance(payload, Mapping)
        or payload.get("class_id") != class_id
        or payload.get("status") not in ("initialized", "unchanged", "preserved")
        or not isinstance(payload.get("style"), Mapping)
    ):
        raise BackendError("The server did not confirm the bootstrap style result.")
    return StyleResult(class_id, payload["status"], dict(payload["style"]))


def update_registry(registry: ClassRegistry, results: Sequence[StyleResult]) -> ClassRegistry:
    """Use authoritative replies for new layers without restyling existing project layers."""
    styles = {result.class_id: result.style for result in results}
    return replace(
        registry,
        classes=tuple(
            replace(cls, style=dict(styles[cls.class_id])) if cls.class_id in styles else cls
            for cls in registry
        ),
    )
