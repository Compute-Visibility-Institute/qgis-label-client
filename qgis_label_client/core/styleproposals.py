"""Reviewable style proposals; this module never writes the class registry."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .stylecapture import CaptureResult, css_color, is_parseable_color


def _comparable(style: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(style)
    for key in ("fill", "stroke"):
        if key in result and is_parseable_color(result[key]):
            result[key] = css_color(result[key])
    return result


@dataclass(frozen=True)
class StyleProposal:
    layer_id: str
    layer_name: str
    class_id: str
    capture: CaptureResult
    current: Mapping[str, Any]
    proposed: Mapping[str, Any] | None = None
    status: str = "refused"

    def detail_lines(self) -> list[str]:
        labels = {
            "proposed": "Proposed for administrator review",
            "unchanged": "Matches the current class style",
            "excluded": "Style proposal not included",
            "refused": "Style could not be captured",
            "conflict": "Conflicting styles: choose one layer's proposal or leave for review",
        }
        lines = [f"{self.layer_name} → {self.class_id}: {labels[self.status]}."]
        lines.append(self.capture.summary())
        if self.proposed is not None:
            lines.append(
                "Current: " + json.dumps(dict(self.current), ensure_ascii=False, sort_keys=True)
            )
            lines.append(
                "Proposed: " + json.dumps(dict(self.proposed), ensure_ascii=False, sort_keys=True)
            )
        return lines


def propose_style(
    layer_id: str,
    layer_name: str,
    class_id: str,
    capture: CaptureResult,
    current: Mapping[str, Any],
    include: bool,
) -> StyleProposal:
    proposal = StyleProposal(layer_id, layer_name, class_id, capture, dict(current))
    if not include:
        return replace(proposal, status="excluded")
    if not capture.captured:
        return proposal
    # Capture describes only QGIS symbology. Preserve zoom limits and future keys,
    # and measurements the reader could not supply. A solid stroke explicitly
    # replaces a custom dash; otherwise the old dash would survive invisibly.
    proposed = dict(current)
    if capture.kind in ("fill", "line") and "dash" not in capture.style:
        proposed.pop("dash", None)
    proposed.update(capture.style)
    status = "unchanged" if _comparable(proposed) == _comparable(current) else "proposed"
    return replace(proposal, proposed=proposed, status=status)


def resolve_styles(proposals: Iterable[StyleProposal]) -> tuple[StyleProposal, ...]:
    proposals = tuple(proposals)
    by_class: dict[str, list[dict[str, Any]]] = {}
    for proposal in proposals:
        if proposal.status in ("proposed", "unchanged") and proposal.proposed is not None:
            by_class.setdefault(proposal.class_id, []).append(_comparable(proposal.proposed))
    conflicts = {
        class_id
        for class_id, styles in by_class.items()
        if any(style != styles[0] for style in styles[1:])
    }
    return tuple(
        replace(p, status="conflict")
        if p.class_id in conflicts and p.status in ("proposed", "unchanged")
        else p
        for p in proposals
    )


def proposals_json(proposals: Iterable[StyleProposal]) -> str:
    """Only resolved changes are transferable; conflicts remain in the report."""
    by_class: dict[str, dict[str, Any]] = {}
    for proposal in resolve_styles(proposals):
        if proposal.status != "proposed":
            continue
        entry = by_class.setdefault(
            proposal.class_id,
            {
                "class_id": proposal.class_id,
                "current_style": dict(proposal.current),
                "style": dict(proposal.proposed),
                "layers": [],
                "notes": [],
            },
        )
        entry["layers"].append(proposal.layer_name)
        for note in proposal.capture.notes:
            if note.detail not in entry["notes"]:
                entry["notes"].append(note.detail)
    return json.dumps(
        {"schema": "cvi-style-proposals/v1", "proposals": list(by_class.values())},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
