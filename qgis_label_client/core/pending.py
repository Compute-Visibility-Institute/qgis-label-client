"""Private, durable journals for edits which have not been confirmed by the server.

This module stores data, never credentials or a provider URI. The QGIS controller
owns snapshots, account checks, conflict detection and the decision to replay.
An uncertain save is deliberately a different state from an unattempted edit.
"""

from __future__ import annotations

import base64
import json
import math
import os
import tempfile
from contextlib import contextmanager, suppress
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from .errors import LabelClientError

SCHEMA_VERSION = 1
STATES = frozenset({"pending", "uncertain", "conflict"})
_TAG = "__cvi_type__"


class JournalError(LabelClientError):
    """An edit journal could not be persisted or recovered without losing data."""


def encode_value(value: Any) -> Any:
    """Encode supported Python values losslessly; Qt values need an adapter first.

    Mappings are wrapped too, so a label attribute with our tag as its own key
    cannot be mistaken for a typed value. Unsupported values fail rather than
    becoming a lossy ``str(value)`` representation.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JournalError("An unpushed attribute contains a non-finite number.")
        return value
    if isinstance(value, datetime):
        return {_TAG: "datetime", "value": value.isoformat(), "fold": value.fold}
    if isinstance(value, date):
        return {_TAG: "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {_TAG: "time", "value": value.isoformat(), "fold": value.fold}
    if isinstance(value, bytes):
        return {_TAG: "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return {
            _TAG: "tuple" if isinstance(value, tuple) else "list",
            "value": [encode_value(item) for item in value],
        }
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise JournalError("An unpushed attribute has a non-text mapping key.")
        return {_TAG: "mapping", "value": {key: encode_value(item) for key, item in value.items()}}
    raise JournalError(f"Cannot preserve unpushed attribute type {type(value).__name__}.")


def decode_value(value: Any) -> Any:
    """Reverse :func:`encode_value`, rejecting corrupt or unknown tagged values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if not isinstance(value, dict):
        raise JournalError("An unpushed attribute has an invalid encoded value.")
    kind, raw = value.get(_TAG), value.get("value")
    try:
        if kind in {"datetime", "time"} and isinstance(raw, str):
            if set(value) != {_TAG, "value", "fold"} or type(value["fold"]) is not int:
                raise ValueError("invalid fold")
            parsed = datetime.fromisoformat(raw) if kind == "datetime" else time.fromisoformat(raw)
            return parsed.replace(fold=value["fold"])
        if set(value) != {_TAG, "value"}:
            raise ValueError("unexpected fields")
        if kind == "date" and isinstance(raw, str):
            return date.fromisoformat(raw)
        if kind == "bytes" and isinstance(raw, str):
            return base64.b64decode(raw, validate=True)
        if kind in {"list", "tuple"} and isinstance(raw, list):
            items = [decode_value(item) for item in raw]
            return tuple(items) if kind == "tuple" else items
        if kind == "mapping" and isinstance(raw, dict):
            if not all(isinstance(key, str) for key in raw):
                raise ValueError("non-text key")
            return {key: decode_value(item) for key, item in raw.items()}
    except (ValueError, TypeError) as exc:
        raise JournalError(f"An unpushed attribute has invalid {kind!r} data.") from exc
    raise JournalError(f"An unpushed attribute has unknown type {kind!r}.")


def _identifier(value: object) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("not a canonical UUID")
    except (ValueError, AttributeError) as exc:
        raise JournalError("An edit journal needs a canonical UUID identifier.") from exc
    return value


def _json_value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise JournalError(f"Journal contains a value which is not JSON data: {type(value).__name__}.")


def _validate(document: Any) -> dict:
    if not isinstance(document, dict):
        raise JournalError("Edit journal is not a JSON object.")
    if (
        type(document.get("schema_version")) is not int
        or document["schema_version"] != SCHEMA_VERSION
    ):
        raise JournalError("Edit journal uses an unsupported schema version; keep the file.")
    _identifier(document.get("id"))
    for key in ("backend", "email", "track", "collection"):
        if not isinstance(document.get(key), str) or not document[key].strip():
            raise JournalError(f"Edit journal is missing its {key} partition.")
    if not isinstance(document.get("project"), str):
        raise JournalError("Edit journal is missing its project partition.")
    try:
        url = urlsplit(document["backend"])
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("backend is not a plain server URL")
    except ValueError as exc:
        raise JournalError(
            "Journal backend must be a server URL without credentials or query."
        ) from exc
    if not isinstance(document.get("state"), str) or document["state"] not in STATES:
        raise JournalError("Edit journal has an unknown save state.")
    if not isinstance(document.get("operations"), (list, dict)):
        raise JournalError("Edit journal is missing its operations.")
    forbidden = {
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "authorization",
        "source_uri",
    }
    if forbidden.intersection(key.lower() for key in document):
        raise JournalError("Credentials and provider URIs must not be stored in an edit journal.")
    _json_value(document)
    return document


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JournalError(f"Edit journal contains duplicate JSON key {key!r}.")
        result[key] = value
    return result


class JournalStore:
    """One atomic JSON file per UUID, in a private QGIS profile directory.

    ``list`` refuses malformed journals visibly; it never silently drops an
    unreadable record from a list that a person might interpret as 'all synced'.
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def _prepare(self) -> None:
        if self.directory.is_symlink():
            raise JournalError(
                f"Edit journal directory cannot be a symbolic link: {self.directory}"
            )
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)

    def _path(self, journal_id: str) -> Path:
        return self.directory / f"{_identifier(journal_id)}.json"

    def _sync_directory(self) -> None:
        # fsync on a directory persists the rename/removal itself on Unix. Windows
        # does not expose directory descriptors through os.open.
        if os.name == "nt":
            return
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def save(self, document: dict) -> dict:
        prepared = dict(document)
        prepared.setdefault("schema_version", SCHEMA_VERSION)
        _validate(prepared)
        path = self._path(prepared["id"])
        temporary = None
        try:
            self._prepare()
            if path.is_symlink():
                raise JournalError(f"Edit journal cannot be a symbolic link: {path}")
            descriptor, name = tempfile.mkstemp(
                prefix=".pending-", suffix=".tmp", dir=self.directory
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(prepared, stream, ensure_ascii=False, allow_nan=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            self._sync_directory()
            return prepared
        except (OSError, ValueError, TypeError) as exc:
            raise JournalError(f"Could not save unpushed edits to {path}: {exc}") from exc
        finally:
            if temporary is not None:
                # Retain the original error if private temporary cleanup also fails.
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def load(self, journal_id: str) -> dict:
        path = self._path(journal_id)
        try:
            self._prepare()
            if path.is_symlink():
                raise JournalError(f"Edit journal cannot be a symbolic link: {path}")
            path.chmod(0o600)
            with path.open(encoding="utf-8") as stream:
                document = _validate(json.load(stream, object_pairs_hook=_unique_object))
            if document["id"] != journal_id:
                raise JournalError("Journal identity does not match its filename.")
            return document
        except (OSError, ValueError, TypeError, JournalError) as exc:
            raise JournalError(f"Could not recover unpushed edits from {path}: {exc}") from exc

    def list(self) -> list[dict]:
        try:
            self._prepare()
            return [self.load(path.stem) for path in sorted(self.directory.glob("*.json"))]
        except OSError as exc:
            raise JournalError(f"Could not list unpushed edits in {self.directory}: {exc}") from exc

    def delete(self, journal_id: str) -> None:
        path = self._path(journal_id)
        try:
            self._prepare()
            if path.is_symlink():
                raise JournalError(f"Edit journal cannot be a symbolic link: {path}")
            path.unlink(missing_ok=True)
            self._sync_directory()
        except OSError as exc:
            raise JournalError(f"Could not remove confirmed edit journal {path}: {exc}") from exc

    @contextmanager
    def claim(self, journal_id: str, expected: dict):
        """Exclusively claim an unchanged journal for one automatic commit.

        Network preflight belongs before this short critical section. The caller
        must persist ``uncertain`` before its native commit while holding the
        claim. A lock left by an interrupted process is never stolen or expired:
        its save may already have reached the server, so it requires review.
        """
        path = self._path(journal_id)
        lock = path.with_suffix(".lock")
        claimed = False
        try:
            try:
                self._prepare()
                descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                claimed = True
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(f"Automatic save claim for {journal_id}\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                self._sync_directory()
            except FileExistsError as exc:
                raise JournalError(
                    f"Unpushed edits are already claimed for saving: {lock}. "
                    "Another QGIS session may be saving, or an interrupted save needs review."
                ) from exc
            except OSError as exc:
                raise JournalError(f"Could not claim unpushed edits at {lock}: {exc}") from exc
            current = self.load(journal_id)
            if current != expected:
                raise JournalError(
                    f"Unpushed edits changed after the reconnect check: {path}. "
                    "Automatic save was stopped; review the current recovery copy."
                )
            yield current
        finally:
            if claimed:
                try:
                    lock.unlink()
                    self._sync_directory()
                except OSError as exc:
                    raise JournalError(
                        f"Could not release automatic save claim {lock}: {exc}. "
                        "Check the server result before clearing this lock."
                    ) from exc
