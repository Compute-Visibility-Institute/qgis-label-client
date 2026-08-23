"""Constraints this repository has to satisfy because it is public.

Three of them are not style preferences, and each has a failure mode measured in lawyers
or incidents rather than in bugs:

* **No credentials.** The plugin source is published; anything secret in it is public.
* **No licensed imagery.** The imagery is Maxar Limited Rights Data. "Just add a small
  sample raster for the tests" is the reflex that breaks this, so the test forbids the
  file extensions rather than trusting the reflex.
* **No deployment hostnames.** Backend URLs are user settings with placeholder defaults.

The remaining checks enforce the dual-Qt5/Qt6 rules and the "use the QGIS network stack"
rule, both of which are cheap to keep and expensive to retrofit.

The literals being searched for live in :mod:`hygiene_rules`, so that this file does not
trip its own scan.
"""

from __future__ import annotations

import re

import pytest
from hygiene_rules import FORBIDDEN_STRINGS, FORBIDDEN_SUFFIXES, SELF, SKIP_DIRECTORIES

PACKAGE = "qgis_label_client"


def _source_files(repo_root):
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if set(path.relative_to(repo_root).parts) & SKIP_DIRECTORIES:
            continue
        yield path


def _python_files(repo_root):
    return sorted((repo_root / PACKAGE).rglob("*.py"))


def test_no_licensed_imagery_or_project_files_are_committed(repo_root):
    offenders = [
        str(path.relative_to(repo_root))
        for path in _source_files(repo_root)
        if path.suffix.lower() in FORBIDDEN_SUFFIXES
    ]
    assert offenders == [], (
        f"These file types must never appear in the public plugin repository: {offenders}"
    )


def test_no_credentials_or_deployment_hostnames_anywhere(repo_root):
    offenders: list[str] = []
    for path in _source_files(repo_root):
        if path.name == SELF:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lowered = text.lower()
        offenders += [
            f"{path.relative_to(repo_root)}: {needle!r}"
            for needle in FORBIDDEN_STRINGS
            if needle.lower() in lowered
        ]
    assert offenders == [], offenders


def test_qt_is_imported_only_through_the_qgis_shim(repo_root):
    # Importing PyQt5 or PyQt6 directly is what makes the October 2026 flip to QGIS 4.2 a
    # migration instead of a config change.
    pattern = re.compile(r"^\s*(from|import)\s+PyQt[56]\b", re.MULTILINE)
    offenders = [
        str(path.relative_to(repo_root))
        for path in _python_files(repo_root)
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], offenders


def test_qt_enums_are_accessed_scoped(repo_root):
    # Qt.RightDockWidgetArea works on Qt5 and fails on Qt6;
    # Qt.DockWidgetArea.RightDockWidgetArea works on both. Requiring a second level after
    # 'Qt.' catches the unscoped form generically, with no curated list to maintain.
    unscoped = re.compile(r"\bQt\.[A-Z]\w+(?!\s*\.)\b")
    offenders: list[str] = []
    for path in _python_files(repo_root):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = unscoped.search(line)
            if match:
                offenders.append(f"{path.relative_to(repo_root)}:{number}: {match.group(0)}")
    assert offenders == [], offenders


def test_no_network_library_bypasses_the_qgis_stack(repo_root):
    # requests/urllib would miss the user's proxy config, SSL exceptions and the
    # authentication database, which is where the token lives.
    pattern = re.compile(
        r"^\s*import\s+(requests|httpx)\b|^\s*from\s+(requests|httpx|urllib\.request)\b",
        re.MULTILINE,
    )
    offenders = [
        str(path.relative_to(repo_root))
        for path in _python_files(repo_root)
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], offenders


def test_the_pure_core_imports_no_qgis(repo_root):
    # The boundary that makes CI possible without QGIS, and that keeps the logic worth
    # testing hardest in a place where it can be tested at all.
    pattern = re.compile(r"^\s*(from|import)\s+qgis\b", re.MULTILINE)
    offenders = [
        str(path.relative_to(repo_root))
        for path in sorted((repo_root / PACKAGE / "core").rglob("*.py"))
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], offenders


def test_the_licence_is_gpl_v2(repo_root):
    text = (repo_root / "LICENSE").read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "Version 2, June 1991" in text


def test_the_package_declares_its_licence(repo_root):
    text = (repo_root / PACKAGE / "__init__.py").read_text(encoding="utf-8")
    assert "SPDX-License-Identifier: GPL-2.0-or-later" in text


@pytest.mark.parametrize(
    "relative",
    [
        "LICENSE",
        "README.md",
        "CHANGELOG.md",
        ".qgis-plugin-ci",
        "pyproject.toml",
        f"{PACKAGE}/metadata.txt",
        f"{PACKAGE}/__init__.py",
        ".github/workflows/release.yml",
        ".github/workflows/test.yml",
    ],
)
def test_required_files_exist(repo_root, relative):
    assert (repo_root / relative).is_file(), f"missing {relative}"
