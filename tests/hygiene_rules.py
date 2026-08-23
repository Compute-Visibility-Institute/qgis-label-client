"""The lists of things this public repository must not contain.

Kept in one module, separate from the tests that use them, for a small but real reason:
a test that greps the tree for a forbidden string will find that string in its own source
and fail on itself. Isolating the literals here means exactly one file has to be exempt
from the scan instead of an ever-growing list of them.
"""

from __future__ import annotations

#: Directories that are not part of the repository's content.
SKIP_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "site-packages",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "build",
    "dist",
    ".eggs",
}

#: Extensions that would mean licensed imagery, a vendor delivery, a project file
#: encoding backend hostnames, or a private key had been committed.
FORBIDDEN_SUFFIXES = {
    ".tif",
    ".tiff",
    ".jp2",
    ".ntf",
    ".img",
    ".imd",
    ".rpb",
    ".til",
    ".qgz",
    ".qgs",
    ".gpkg",
    ".shp",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
}

#: Substrings indicating a credential or an internal deployment hostname.
FORBIDDEN_STRINGS = (
    "-----BEGIN RSA PRIVATE KEY",
    "-----BEGIN PRIVATE KEY",
    "-----BEGIN OPENSSH PRIVATE KEY",
    '"private_key_id"',
    "computegov",
)

#: The one file exempt from the string scan: this one.
SELF = "hygiene_rules.py"

#: Domain vocabulary that must never be a string literal in the plugin.
#:
#: Class ids and attribute names live in the server's class registry -- `label_class` and
#: the JSON Schema it publishes -- and every one the plugin renders is read from there at
#: runtime. A literal here would reintroduce exactly the drift the schema design removes:
#: the web UI would show a newly added attribute the day it was added and QGIS would show
#: it whenever the plugin was next released.
#:
#: This is a deny list, so it proves less than the general property -- it cannot know
#: about a vocabulary term added after this file was written. It does catch the actual
#: regression, which is somebody reaching into `attrs` by name rather than through the
#: registry. Comments and docstrings are not scanned; the check compares whole string
#: constants, so prose that merely mentions a class is fine.
DOMAIN_VOCABULARY = frozenset(
    {
        # class ids
        "compound",
        "datacenter_building",
        "substation",
        "backup_generator",
        "cooling_unit",
        "powerline",
        "administrative",
        # attribute names
        "operator",
        "cooling_unit_count",
        "transformer_count",
        "commissioned_year",
        "area_m2",
        "building_designator",
        "building_use",
        "floors",
        "voltage_kv",
        "unit_count",
        "cooling_type",
        "admin_level",
    }
)
