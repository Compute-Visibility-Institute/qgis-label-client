"""Offline journals preserve edits, partition identity and uncertain save outcomes."""

import json
import os
from datetime import date, datetime, time, timezone
from uuid import uuid4

import pytest

from qgis_label_client.core.pending import JournalError, JournalStore, decode_value, encode_value


def document(**changes):
    return {
        "id": str(uuid4()),
        "backend": "https://api.example.org",
        "email": "analyst@example.org",
        "track": "default",
        "collection": "label_polygon",
        "project": "/projects/campus.qgz",
        "state": "pending",
        "operations": {"added": [{"name": "中国联通", "geometry": "00ab"}]},
        **changes,
    }


def test_journal_survives_new_store_and_preserves_partition_and_state(tmp_path):
    directory = tmp_path / "pending"
    stored = JournalStore(directory).save(document(state="uncertain"))
    restored = JournalStore(directory).load(stored["id"])
    assert restored == stored
    assert restored["schema_version"] == 1
    assert restored["state"] == "uncertain"
    assert JournalStore(directory).list() == [stored]
    if os.name != "nt":
        assert directory.stat().st_mode & 0o777 == 0o700
        assert (directory / f"{stored['id']}.json").stat().st_mode & 0o777 == 0o600


def test_confirmed_journal_removal_is_idempotent(tmp_path):
    store = JournalStore(tmp_path / "pending")
    stored = store.save(document())
    store.delete(stored["id"])
    store.delete(stored["id"])
    assert store.list() == []


def test_replace_failure_keeps_previous_durable_edits(tmp_path, monkeypatch):
    store = JournalStore(tmp_path / "pending")
    original = store.save(document())

    def fail_replace(*_args):
        raise OSError("disk refused replacement")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(JournalError, match="Could not save unpushed edits"):
        store.save({**original, "operations": {"added": ["new edit"]}})
    assert store.load(original["id"]) == original
    assert len(list(store.directory.iterdir())) == 1


@pytest.mark.parametrize("payload", ["{broken", "[]", '{"id":1,"id":2}'])
def test_corrupt_journals_are_reported_instead_of_disappearing(tmp_path, payload):
    store = JournalStore(tmp_path)
    path = tmp_path / f"{uuid4()}.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(JournalError, match="Could not recover unpushed edits"):
        store.list()
    assert path.read_text(encoding="utf-8") == payload


def test_unknown_version_and_filename_identity_are_refused(tmp_path):
    store = JournalStore(tmp_path)
    original = store.save(document())
    path = tmp_path / f"{original['id']}.json"
    path.write_text(json.dumps({**original, "schema_version": 99}), encoding="utf-8")
    with pytest.raises(JournalError, match="unsupported schema"):
        store.load(original["id"])
    path.write_text(json.dumps({**original, "id": str(uuid4())}), encoding="utf-8")
    with pytest.raises(JournalError, match="identity does not match"):
        store.load(original["id"])


@pytest.mark.parametrize("journal_id", ["../../credentials", "", "invalid", "../a.json"])
def test_identifiers_cannot_escape_private_directory(tmp_path, journal_id):
    store = JournalStore(tmp_path)
    with pytest.raises(JournalError, match="UUID"):
        store.load(journal_id)
    with pytest.raises(JournalError, match="UUID"):
        store.delete(journal_id)


@pytest.mark.parametrize(
    "changes",
    [
        {"email": ""},
        {"track": ""},
        {"collection": ""},
        {"project": None},
        {"state": "synced"},
        {"state": []},
        {"operations": None},
        {"access_token": "do-not-store"},
        {"backend": "https://user:secret@example.org"},
        {"backend": "https://api.example.org?token=secret"},
    ],
)
def test_invalid_partition_or_credentials_never_saved(tmp_path, changes):
    store = JournalStore(tmp_path / "pending")
    with pytest.raises(JournalError):
        store.save(document(**changes))
    assert not store.directory.exists()


def test_roundtrip_typed_attributes_including_marker_collision():
    value = {
        "nullable": None,
        "boolean": True,
        "integer": 9007199254740993,
        "number": 1.25,
        "name": "国富瑞天津数据中心",
        "date": date(2026, 9, 22),
        "datetime": datetime(2026, 9, 22, 12, 15, 44, 123456, tzinfo=timezone.utc),
        "time": time(1, 30, fold=1),
        "bytes": b"\x00\xff",
        "tuple": (1, "name"),
        "array": [{"nested": [None, 2]}],
        "literal_tag": {"__cvi_type__": "date", "value": "not a date"},
    }
    encoded = json.loads(json.dumps(encode_value(value), allow_nan=False))
    restored = decode_value(encoded)
    assert restored == value
    assert restored["time"].fold == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object(), {1: "not text"}])
def test_unrepresentable_values_fail_without_string_coercion(value):
    with pytest.raises(JournalError):
        encode_value(value)


@pytest.mark.parametrize(
    "encoded",
    [
        {"__cvi_type__": "future-type", "value": "x"},
        {"__cvi_type__": "date", "value": "bad-date"},
        {"__cvi_type__": "bytes", "value": "not base64!"},
        {"__cvi_type__": "time", "value": "01:30", "fold": 2},
        {"__cvi_type__": "date", "value": "2026-09-22", "extra": "unexpected"},
        float("nan"),
    ],
)
def test_unknown_or_corrupt_typed_values_are_not_restored(encoded):
    with pytest.raises(JournalError):
        decode_value(encoded)


def test_symbolic_links_never_read_or_overwrite_another_file(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Host does not support symbolic links")
    with pytest.raises(JournalError, match="symbolic link"):
        JournalStore(linked).list()
    store = JournalStore(real)
    item = document()
    outside = tmp_path / "outside.json"
    outside.write_text("unchanged", encoding="utf-8")
    (real / f"{item['id']}.json").symlink_to(outside)
    for action in (
        lambda: store.save(item),
        lambda: store.load(item["id"]),
        lambda: store.delete(item["id"]),
    ):
        with pytest.raises(JournalError, match="symbolic link"):
            action()
    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_second_automatic_commit_cannot_claim_same_journal(tmp_path):
    first, second = JournalStore(tmp_path), JournalStore(tmp_path)
    item = first.save(document())
    lock = tmp_path / f"{item['id']}.lock"
    with first.claim(item["id"], item) as claimed:
        assert claimed == item
        assert lock.exists()
        if os.name != "nt":
            assert lock.stat().st_mode & 0o777 == 0o600
        with pytest.raises(JournalError, match="already claimed"), second.claim(item["id"], item):
            pytest.fail("Second automatic commit must never start")
        assert lock.exists(), "Refused claimant must not release the first session's lock"
    assert not lock.exists()
    assert first.load(item["id"]) == item


def test_changed_disk_state_stops_claim_and_releases_lock(tmp_path):
    store = JournalStore(tmp_path)
    old = store.save(document())
    changed = store.save({**old, "state": "uncertain"})
    with (
        pytest.raises(JournalError, match="changed after the reconnect check"),
        store.claim(old["id"], old),
    ):
        pytest.fail("Changed journal must not be committed automatically")
    assert not (tmp_path / f"{old['id']}.lock").exists()
    assert store.load(old["id"]) == changed


def test_claim_releases_lock_when_save_raises_and_keeps_uncertain_data(tmp_path):
    store = JournalStore(tmp_path)
    item = store.save(document())
    with pytest.raises(RuntimeError, match="native save failed"), store.claim(item["id"], item):
        store.save({**item, "state": "uncertain"})
        raise RuntimeError("native save failed")
    assert not (tmp_path / f"{item['id']}.lock").exists()
    assert store.load(item["id"])["state"] == "uncertain"


def test_missing_journal_cannot_be_claimed(tmp_path):
    store = JournalStore(tmp_path)
    item = store.save(document())
    store.delete(item["id"])
    with pytest.raises(JournalError, match="Could not recover"), store.claim(item["id"], item):
        pytest.fail("Deleted journal cannot be replayed")
    assert not (tmp_path / f"{item['id']}.lock").exists()


def test_interrupted_claim_is_never_automatically_stolen(tmp_path):
    store = JournalStore(tmp_path)
    item = store.save(document())
    lock = tmp_path / f"{item['id']}.lock"
    lock.write_text("interrupted save", encoding="utf-8")
    with (
        pytest.raises(JournalError, match="interrupted save needs review"),
        store.claim(item["id"], item),
    ):
        pytest.fail("An interrupted save needs deliberate review")
    assert lock.read_text(encoding="utf-8") == "interrupted save"
    assert store.load(item["id"]) == item
