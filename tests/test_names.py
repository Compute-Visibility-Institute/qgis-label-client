"""Detecting the UTF-7 truncation, and assembling ``label.names``.

The rule being protected: publishing a name that has lost its final character makes the
damage authoritative in a system that will outlive the shapefiles. The detector therefore
has to fire on the real damaged values -- which are in
:mod:`snapshot_fixtures`, taken from the analysis rather than invented -- and stay silent
on the shapes most likely to look like damage and not be it.
"""

from __future__ import annotations

import pytest
from snapshot_fixtures import DAMAGED_NAMES, INTACT_NAMES

from qgis_label_client.core.names import (
    AUTO,
    build_names,
    is_cjk,
    is_damaged,
    looks_chinese,
    truncated_tail,
)


@pytest.mark.parametrize("stored,english", DAMAGED_NAMES)
def test_the_real_damaged_names_are_detected(stored, english):
    # Every one of these is a Name:ch from the snapshot whose Name_en proves the loss.
    assert is_damaged(stored), f"{stored} should be flagged; {english} shows what is missing"


@pytest.mark.parametrize(
    "stored,tail",
    [
        ("云枢智能云乌兰察布数据中X8", "X8"),
        ("云启乌兰察布智算中fw", "fw"),
        ("寰宇科技乌兰察布云智算中心一号基XM", "XM"),
    ],
)
def test_the_tail_reported_is_the_leftover_base64(stored, tail):
    # The trailing garbage differs per record because the base64 offset depends on the
    # preceding content -- the signature of a cut run, not a consistent substitution.
    assert truncated_tail(stored) == tail


@pytest.mark.parametrize("text", INTACT_NAMES)
def test_intact_and_look_alike_names_are_not_flagged(text):
    assert not is_damaged(text)


def test_a_single_trailing_letter_after_cjk_is_not_damage():
    # 恒通数据中心-B is a real building designator. The truncation leaves the surviving
    # high 12 bits of one character, which is two base64 characters, never one.
    assert truncated_tail("恒通数据中心B") == ""
    assert truncated_tail("恒通数据中心BX") != ""


def test_a_long_ascii_run_after_cjk_is_not_damage():
    # Six characters is a word, not a residue.
    assert not is_damaged("数据中心Center")


def test_base64_characters_not_preceded_by_cjk_are_never_damage():
    assert not is_damaged("Data Center")
    assert not is_damaged("B-2")


def test_the_slash_and_plus_forms_are_recognised():
    # RFC 2152 modified base64 includes '+' and '/'; two of the observed tails use them.
    assert is_damaged("数据中U/")
    assert is_damaged("数据中U+")


def test_cjk_detection_covers_the_ranges_that_matter():
    assert is_cjk("中") and is_cjk("园")
    assert not is_cjk("B") and not is_cjk("-")
    assert looks_chinese("Yunhui 数据中心")
    assert not looks_chinese("Yunhui Data Center")


# --- assembling names ------------------------------------------------------


def test_named_languages_are_kept_as_given():
    result = build_names([("zh", "云汇数据中心"), ("en", "Yunhui Data Center")])
    assert result.names == {"zh": "云汇数据中心", "en": "Yunhui Data Center"}
    assert result.damaged == ()


def test_an_unmarked_name_column_is_filed_by_content():
    # Substation.Name has no language marker anywhere. Guessing from content beats
    # silently filing Chinese under "en", which would be a claim about the data.
    assert build_names([(AUTO, "变电站一号")]).names == {"zh": "变电站一号"}
    assert build_names([(AUTO, "Ulanqab Substation")]).names == {"en": "Ulanqab Substation"}


@pytest.mark.parametrize("empty", [None, "", "   ", "\t\n"])
def test_empty_values_are_omitted_not_stored(empty):
    # jsonb ? 'zh' should mean "somebody recorded a Chinese name". An empty string is a
    # claim that the name is the empty string.
    assert build_names([("zh", empty)]).names == {}


def test_damaged_names_are_counted_but_published_by_default():
    result = build_names([("zh", "云枢智能云乌兰察布数据中X8"), ("en", "Yunshu Data Center")])
    assert result.damaged == ("zh",)
    assert result.omitted == ()
    # Losing the name entirely is worse than a name missing one character, and the
    # English name often survives where the Chinese one did not.
    assert result.names["zh"] == "云枢智能云乌兰察布数据中X8"
    assert result.names["en"] == "Yunshu Data Center"


def test_skipping_damaged_names_omits_only_the_damaged_language():
    result = build_names(
        [("zh", "云枢智能云乌兰察布数据中X8"), ("en", "Yunshu Data Center")],
        skip_damaged=True,
    )
    assert result.names == {"en": "Yunshu Data Center"}
    assert result.damaged == ("zh",)
    assert result.omitted == ("zh",)


def test_a_nameset_is_falsy_when_it_holds_nothing():
    assert not build_names([("zh", "")])
    assert build_names([("zh", "园区")])
