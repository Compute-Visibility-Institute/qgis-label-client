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
