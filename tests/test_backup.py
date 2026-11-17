"""Backups are only good if they restore: create, restore-test, and detect a corrupted archive."""
import json
import tarfile

import pytest

from backend.core import backup, config, store


@pytest.fixture
def data(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(store, "DB_PATH", d / "aiops.db")
    monkeypatch.setattr(config, "DB_PATH", d / "aiops.db")
    monkeypatch.setattr(backup, "DATA", d)
    monkeypatch.setattr(backup, "LOCAL", d / "backups")
    store.init()
    store.open_incident("high_cpu", "Node-1", "h", "t", "fp", {})
    store.audit(None, "t", "e", {"x": 1})
    (d / "users.json").write_text(json.dumps({"admin": {"role": "admin"}}))
    (d / "gateway_keys.json").write_text(json.dumps({"k": {"hash": "x"}}))
    return d


def test_backup_restores_and_verifies(data):
    archive, manifest = backup.create()
    assert oct(archive.stat().st_mode)[-3:] == "600"
    r = backup.verify(archive)
    assert all(r["checks"].values()), r["checks"]
    assert manifest["counts"]["incidents"] == 1


def test_tampered_backup_fails_verification(data, tmp_path):
    archive, _ = backup.create()
    root = tmp_path / "x"
    with tarfile.open(archive) as t:
        t.extractall(root, filter="data")
    inner = next(root.iterdir())
    import sqlite3
    with sqlite3.connect(inner / "aiops.db") as c:
        c.execute("DROP TRIGGER audit_no_update")
        c.execute("UPDATE audit_events SET actor='mallory'")
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as t:
        t.add(inner, arcname=inner.name)
    r = backup.verify(bad)
    assert r["checks"]["audit hash chain verifies on the restored copy"] is False
