"""What the server refused, said for the person who has to fix it.

The API answers a refused write with a JSON error document: a ``code`` naming the rule
and a ``description`` written for whoever reads the API's logs as much as for an
analyst. Shown raw, a self-intersecting polygon arrived as an HTTP status, a URL and
300 characters of JSON cut off mid-word. This module turns that document into one
sentence about the data and one about what to do, using the structured fields when the
server sends them and the established wording of its description when it does not.

Pure: no QGIS imports, so every phrasing here is tested without a running QGIS.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

#: GEOS's validity reasons, as ST_IsValidReason spells them, in plain words.
_GEOS_REASONS = {
    "self-intersection": "its outline crosses itself",
    "ring self-intersection": "one of its rings touches itself",
    "too few points in geometry component": "a part of it has too few points",
    "hole lies outside shell": "a hole lies outside its outer ring",
    "holes are nested": "one hole lies inside another",
    "nested holes": "one hole lies inside another",
    "interior is disconnected": "its holes split it into separate pieces",
    "nested shells": "one part lies inside another",
    "duplicate rings": "it repeats a ring",
    "invalid coordinate": "it has an invalid coordinate",
    "ring is not closed": "a ring does not end where it starts",
}

_INVALID = re.compile(
    r"not a valid geometry:\s*(?P<reason>.+?)"
    r"(?:\s+at\s+POINT\s*\(\s*(?P<x>-?\d+(?:\.\d+)?)\s+(?P<y>-?\d+(?:\.\d+)?)\s*\))?"
    r"(?:\.\s|\.$|$)",
    re.IGNORECASE | re.DOTALL,
)

#: What to do, per refusal code. A code missing here falls back to the description.
_ACTIONS = {
    "GeometryInvalid": (
        "Find it with Vector ▸ Geometry Tools ▸ Check Validity, fix it (or run Fix "
        "Geometries), then Save again."
    ),
    "GeometryWrongCrs": "Reproject the layer to EPSG:4326, then Save again.",
    "GeometryHasZ": (
        "Drop the Z values (Vector ▸ Geometry Tools ▸ Drop M/Z Values), then Save again."
    ),
}


def _location(payload: Mapping[str, Any], match: re.Match[str] | None) -> str:
    """``near 45.678901° N, 12.345679° E``, or empty when the server named no place."""
    point = payload.get("location")
    x = y = None
    if isinstance(point, list | tuple) and len(point) == 2:
        x, y = point
    elif match and match.group("x") is not None:
        x, y = float(match.group("x")), float(match.group("y"))
    if not isinstance(x, int | float) or not isinstance(y, int | float):
        return ""
    lat = f"{abs(y):.6f}° {'N' if y >= 0 else 'S'}"
    lon = f"{abs(x):.6f}° {'E' if x >= 0 else 'W'}"
    return f" near {lat}, {lon}"


def _first_sentences(text: str, limit: int = 400) -> str:
    """Whole sentences up to ``limit`` characters: never a cut-off word."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text.rfind(". ", 0, limit)
    return text[: cut + 1] if cut > 0 else text[:limit].rsplit(" ", 1)[0] + "…"


def _text(payload: Mapping[str, Any]) -> str:
    """The document's prose. FastAPI's validation ``detail`` is a list of problems,
    each with a ``msg``; it is said as those messages, never as a Python repr."""
    value = payload.get("description") or payload.get("detail") or ""
    if isinstance(value, list):
        messages = [
            str(item.get("msg") or "").strip() if isinstance(item, Mapping) else str(item)
            for item in value
        ]
        return "; ".join(message for message in messages if message)
    return str(value).strip()


def describe(payload: object) -> str | None:
    """One readable sentence saying what was refused and what to do, or ``None``.

    ``None`` means the error document says nothing usable, and the caller keeps its
    own wording.
    """
    if not isinstance(payload, Mapping):
        return None
    code = str(payload.get("code") or "")
    text = _text(payload)
    if code == "GeometryInvalid":
        # The GEOS phrasing only for a GEOS verdict. The API uses the same code for the
        # structural refusals it decides itself -- an empty polygon, a ring that is
        # short or not closed, a short line -- and their own sentence is the message.
        match = _INVALID.search(text)
        raw = str(payload.get("reason") or (match.group("reason") if match else "")).strip()
        if raw:
            reason = _GEOS_REASONS.get(raw.casefold().rstrip("."), raw.rstrip("."))
            return (
                f"The server refused a shape because {reason}{_location(payload, match)}. "
                + _ACTIONS[code]
            )
    if not text:
        return None
    sentence = _first_sentences(text)
    action = _ACTIONS.get(code)
    return f"{sentence} {action}" if action and action not in sentence else sentence


def is_refusal(status: int | None) -> bool:
    """The server answered and wrote nothing: safe to send again once fixed.

    Every 4xx except 408, which is the request timing out, not the server deciding.
    The class provider draws the same line between a refusal and an unknown outcome.
    """
    return status is not None and 400 <= status < 500 and status != 408
