"""``label.names``, and the damage this plugin must not launder into it.

WHY THERE IS A WHOLE MODULE FOR TWO STRING FIELDS

Six of the seven source ``.cpg`` sidecars declare UTF-7, and the writer that produced them
never flushes the final escape run. UTF-7 encodes CJK as ``+<modified-base64>-``; sixteen
bits per character do not divide into six-bit base64 groups, so the last character of a
value is emitted as its high 12 bits and the residual 2 or 4 bits are dropped along with
the terminating ``-``. A lenient decoder reads every whole character it can and then hands
back the leftover base64 as literal ASCII.

That is why the corruption is always the *final* character, and why the trailing garbage
differs every time (``fw``, ``X8``, ``XM``, ``Vu``, ``U/``) -- the run gives up at a
different bit offset depending on what preceded it:

    数据中心  ->  数据中X8
    智算中心  ->  智算中fw
    一号基地  ->  一号基XM

At least 52% of the Chinese compound names in the source are affected -- 81 of 157 named
compounds. The bits are gone; nothing here repairs them.

WHAT THIS MODULE IS THEREFORE FOR

The bootstrap publish is a one-way door. Whatever lands in ``label.names`` becomes the
authoritative name in a system that will outlive the shapefiles, and a name missing its
final character is indistinguishable from a correct one once it is in there. So the
damage is *detected* and *counted* before anything is sent, and the person publishing is
told the number and made to choose. This module does the detecting and the counting; the
choosing belongs in the dialog, where a human is.

Absent names are omitted rather than stored as empty strings. ``jsonb ? 'zh'`` should mean
"somebody recorded a Chinese name", and an empty string is a claim that the name is the
empty string.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: Language key meaning "the field name does not say; decide from the content". A field
#: literally called ``Name``, with no marker anywhere, is the Substation layer's case.
AUTO = "auto"

#: BCP 47 primary subtags, used as ``label.names`` keys. Not governed vocabulary --
#: ``names`` is free-form JSONB and its keys are whatever a client writes -- but the
#: platform's own examples use these two and consistency is worth more here than freedom.
CHINESE = "zh"
ENGLISH = "en"

#: CJK ranges that matter for "is this Chinese?": Unified Ideographs, Extension A, the
#: compatibility block, and the supplementary planes (Extensions B and beyond).
_CJK_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x3FFFF),
)

#: RFC 2152 "modified base64" -- standard base64 with ``=`` padding removed. These are the
#: characters a truncated escape run leaves behind when a lenient decoder gives up.
B64_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")

#: A truncated final character survives as its high 12 bits, which is exactly two base64
#: characters; three is allowed for decoders that surrender a group earlier.
#:
#: The lower bound is the interesting one. A *single* trailing ASCII letter after a CJK
#: character is a common, legitimate building designator -- 恒通数据中心-B is real data,
#: not damage -- and flagging those would bury the 81 genuinely broken names in noise.
MIN_TAIL_LENGTH = 2
MAX_TAIL_LENGTH = 3


#: Everything a DBF field can be padded with: space, tab and the C0 control characters,
#: plus DEL. Used as the trim set rather than ``str.strip()``'s whitespace-only default.
_PADDING = "".join(chr(code) for code in range(0x21)) + "\x7f"


def clean_text(value: object) -> str:
    """One source value as text, with padding trimmed from both ends.

    ``str.strip()`` removes whitespace, which is what a DBF *should* pad a fixed-width
    field with. Some writers pad with NUL instead, and a surviving NUL is silent twice
    over:

    * it stops the truncation scan dead. :func:`truncated_tail` walks back from the end of
      the string looking for base64 characters, a NUL is not one, so every damaged name in
      a NUL-padded layer reports as clean -- and a scan that returns zero reads as "this
      layer was checked and is fine", which is the one direction this module refuses to
      fail in.
    * it cannot be stored. ``\\u0000`` has no representation in ``jsonb``; Postgres
      refuses the whole row, and the feature service reports that as a bare 500.

    Padding is not content, so trimming it loses nothing.
    """
    return ("" if value is None else str(value)).strip(_PADDING)


def is_cjk(char: str) -> bool:
    """True for one CJK ideograph."""
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def looks_chinese(text: str) -> bool:
    """True when `text` contains any CJK ideograph.

    Used only to file an unmarked ``Name`` column under the right language key. Guessing
    from content beats silently filing Chinese under ``en``, which is a claim about the
    data rather than an admission of ignorance.
    """
    return any(is_cjk(char) for char in text)


def truncated_tail(text: str) -> str:
    """The trailing base64 run left by a cut UTF-7 escape, or ``""``.

    The signature is narrow on purpose, in both directions:

    * the run must be 2-3 characters -- see :data:`MIN_TAIL_LENGTH`; and
    * the character immediately before it must be CJK, because the damage is always the
      tail of an escape run and an escape run only exists where non-ASCII was encoded.

    That second condition is what keeps ordinary English out of it. "Ulanqab Data Center"
    ends in six base64-alphabet characters and is not damaged; nothing CJK precedes them.

    WHAT THIS CANNOT DISTINGUISH, AND WHY IT STILL FIRES

    The residual of a cut escape run is arbitrary base64, so a two-character site
    designator after a Chinese character -- 数据中心B2, 一号基地A1 -- is *indistinguishable*
    from damage by any test on the string alone. Those are flagged too. The count this
    produces is therefore an upper bound on the damage, not a measurement of it, and every
    caller that shows the number to a person has to say so: the number is what a human
    weighs when deciding whether to omit these names, and omitting an intact name destroys
    it permanently.

    Erring this way round is deliberate. An over-report costs a sentence of explanation; an
    under-report reads as "this layer is clean" and publishes the damage as authoritative.
    Narrowing it further has been tried and does not survive the real data: the tail length
    that the encoding arithmetic predicts from the surviving run length disagrees with
    three of the five recorded damaged names in ``tests/snapshot_fixtures``, so filtering
    on it would silently stop reporting them.
    """
    stripped = clean_text(text)
    index = len(stripped)
    while index > 0 and stripped[index - 1] in B64_ALPHABET:
        index -= 1
    tail = stripped[index:]
    if not MIN_TAIL_LENGTH <= len(tail) <= MAX_TAIL_LENGTH:
        return ""
    if index == 0 or not is_cjk(stripped[index - 1]):
        return ""
    return tail


def is_damaged(text: str) -> bool:
    """True when `text` carries the UTF-7 truncation signature."""
    return bool(truncated_tail(text))


@dataclass(frozen=True)
class NameSet:
    """The ``names`` object for one feature, and what was wrong with it."""

    names: Mapping[str, str] = field(default_factory=dict)
    #: Language keys whose recorded value carries the truncation signature. Reported even
    #: when the value is published anyway -- the count is the whole point.
    damaged: tuple[str, ...] = ()
    #: Damaged languages left out because the caller asked for that.
    omitted: tuple[str, ...] = ()
    #: Values dropped because two source columns resolved to the same language key. One
    #: line each, naming the value that was lost: ``names`` is a mapping and the second
    #: write would otherwise overwrite the first with nothing anywhere recording it.
    collisions: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.names)


def build_names(
    entries: Iterable[tuple[str, Any]],
    *,
    skip_damaged: bool = False,
) -> NameSet:
    """Assemble ``label.names`` from ``(language, raw value)`` pairs.

    `language` is a BCP 47 subtag or :data:`AUTO`. Empty and whitespace-only values are
    dropped entirely rather than stored, for the reason in the module docstring.

    `skip_damaged` omits a value whose tail matches the truncation signature. It defaults
    off, and the caller is expected to have asked a human first: an absent name is honest
    but ``Name_en`` often survives where ``Name:ch`` did not, and dropping a whole record's
    Chinese name loses more than it protects.

    Two columns can resolve to one key -- an unmarked ``Name`` holding Chinese alongside a
    ``Name:ch``, or a ``Name_en`` alongside a ``NAME_ENG``. A mapping cannot hold both, so
    one value is lost; which one is chosen deliberately (a column that *declares* its
    language outranks one whose language was inferred from its content) and the loss is
    reported rather than left to the assignment order of a dictionary.
    """
    names: dict[str, str] = {}
    inferred: set[str] = set()
    damaged: list[str] = []
    omitted: list[str] = []
    collisions: list[str] = []

    for language, raw in entries:
        text = clean_text(raw)
        if not text:
            continue
        key = language
        guessed = key == AUTO
        if guessed:
            key = CHINESE if looks_chinese(text) else ENGLISH
        if is_damaged(text):
            damaged.append(key)
            if skip_damaged:
                omitted.append(key)
                continue
        if key in names:
            # Keep the declared language over the inferred one; otherwise keep the first.
            if guessed or key not in inferred:
                collisions.append(
                    f"two source columns both hold the {key!r} name; {text!r} was dropped "
                    f"in favour of {names[key]!r}"
                )
                continue
            collisions.append(
                f"two source columns both hold the {key!r} name; {names[key]!r} was "
                f"dropped in favour of {text!r}, which declares its language"
            )
        names[key] = text
        if guessed:
            inferred.add(key)
        else:
            inferred.discard(key)

    return NameSet(
        names=names,
        damaged=tuple(damaged),
        omitted=tuple(omitted),
        collisions=tuple(collisions),
    )
