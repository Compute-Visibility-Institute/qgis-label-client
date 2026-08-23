"""``metadata.txt`` and the packaging contract.

The packaging mistakes QGIS reports unhelpfully are all checkable statically:

* the zip must contain exactly one top-level directory, named identically to the Python
  package -- zip the folder, not its contents;
* ``version=`` drives upgrade detection in the plugin manager;
* ``qgisMinimumVersion`` is what stops a 3.34 user installing a plugin that needs 3.44.
"""

from __future__ import annotations

import configparser

import pytest
from hygiene_rules import FORBIDDEN_STRINGS

PACKAGE = "qgis_label_client"


@pytest.fixture
def metadata(repo_root) -> configparser.SectionProxy:
    parser = configparser.ConfigParser()
    parser.read(repo_root / PACKAGE / "metadata.txt", encoding="utf-8")
    return parser["general"]


def test_minimum_version_is_the_pinned_ltr(metadata):
    assert metadata["qgisMinimumVersion"] == "3.44"


def test_qt6_support_is_declared(metadata):
    # The plugin repository shows this in its Qt6 Check tab. Declaring it from the first
    # release is what makes the October 2026 pin flip a non-event.
    assert metadata["supportsQt6"] == "True"


def test_maximum_version_admits_the_4x_line(metadata):
    major = int(metadata["qgisMaximumVersion"].split(".")[0])
    assert major >= 4


def test_required_fields_are_present(metadata):
    for key in ("name", "description", "version", "author", "email", "about", "repository"):
        assert metadata.get(key), f"metadata.txt is missing {key}"


def test_version_matches_the_package(repo_root, metadata):
    text = (repo_root / PACKAGE / "__init__.py").read_text(encoding="utf-8")
    assert f'__version__ = "{metadata["version"]}"' in text


def test_the_icon_referenced_by_metadata_exists(repo_root, metadata):
    assert (repo_root / PACKAGE / metadata["icon"]).is_file()


def test_metadata_carries_no_deployment_hostname(metadata):
    # The backend URL is a user setting. Nothing here may name a real deployment, and the
    # `about` text is the field most likely to acquire one by accident.
    blob = " ".join(metadata.values()).lower()
    assert not [needle for needle in FORBIDDEN_STRINGS if needle.lower() in blob]


def test_plugin_ci_points_at_the_package_directory(repo_root):
    text = (repo_root / ".qgis-plugin-ci").read_text(encoding="utf-8")
    # qgis-plugin-ci zips this directory as the single top-level entry, which is the
    # packaging rule QGIS enforces with an unhelpful error.
    assert f"plugin_path: {PACKAGE}" in text


def test_the_package_directory_matches_the_python_package(repo_root):
    assert (repo_root / PACKAGE / "__init__.py").is_file()
    assert (repo_root / PACKAGE / "metadata.txt").is_file()


def test_changelog_has_an_entry_for_the_current_version(repo_root, metadata):
    text = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert metadata["version"] in text
