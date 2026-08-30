"""Building QGIS expressions safely, in one place.

WHY THIS IS A MODULE AND NOT TWO HELPER FUNCTIONS IN THE FILE THAT NEEDS THEM

Two features now put a clause into the OAPIF provider's ``filter`` parameter -- the as-of
control (:mod:`.asof`) and the history-track canary (:mod:`.tracks`) -- and they have to
be able to appear together in one expression. Once two modules quote identifiers, a third
will, and quoting rules that exist in three places are quoting rules that differ in three
places.

THE THING TO KNOW BEFORE EDITING ANY CALLER

The provider's ``filter`` does **not** take CQL2-text. It takes a *QGIS expression*, which
the provider compiles to CQL2 itself and sends as ``filter=...&filter-lang=cql2-text``.
Handing it literal CQL2 is not ignored -- an expression QGIS cannot parse makes the layer
*invalid*, so the analyst gets no layer rather than an unfiltered one. That is why
identifiers are double-quoted (a QGIS expression's column reference) and values are
single-quoted strings rather than typed constructors. :mod:`.asof` has the worked example.
"""

from __future__ import annotations

from collections.abc import Iterable


def identifier(name: str) -> str:
    """Quote a column name as a QGIS expression column reference."""
    return '"' + name.replace('"', '""') + '"'


def literal(text: str) -> str:
    """Quote a value as a QGIS expression string literal."""
    return "'" + text.replace("'", "''") + "'"


def equals(column: str, value: str) -> str:
    """``"column" = 'value'``, both sides quoted for their kind."""
    return f"{identifier(column)} = {literal(value)}"


def all_of(*clauses: str | None) -> str:
    """Combine clauses with AND, parenthesising each, dropping the empty ones.

    Parenthesised because the clauses are written independently and at least one of them
    (:func:`.asof.cql2_filter`) already contains a bare ``AND`` and an ``OR``. Without the
    brackets, appending a second clause silently rebinds that ``OR`` and changes which
    features come back -- a filter that is wrong rather than absent, which is the harder
    of the two to notice.
    """
    parts = [f"({clause})" for clause in clauses if clause]
    if not parts:
        return ""
    if len(parts) == 1:
        # One clause needs no wrapper, and leaving it bare keeps the common case's URI
        # readable in the layer properties dialog -- where a person actually reads it.
        return parts[0][1:-1]
    return " AND ".join(parts)


def any_of(clauses: Iterable[str]) -> str:
    """Combine clauses with OR. Same bracketing rule as :func:`all_of`."""
    parts = [f"({clause})" for clause in clauses if clause]
    return " OR ".join(parts)
