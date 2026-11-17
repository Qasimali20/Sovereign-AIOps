"""Backups with a restore test, kept on two nodes.

  python -m backend.core.backup            back up, restore-test, copy to Node-3, prune (run by aiops-backup.timer)
  python -m backend.core.backup --verify F restore-test an existing archive

A backup is only counted as good after it has been restored to a scratch directory and checked:
SQLite integrity_check on every database, the audit hash chain re-verified on the restored copy, and row
counts compared with the live database at backup time. Each run is recorded as the `backup-restore`
evaluation, so a failing backup shows on the dashboard like any other failing test."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from backend.core import config, evals, store

DATA = config.DB_PATH.parent
LOCAL = Path(os.environ.get("AIOPS_BACKUP_DIR", Path.home() / "aiops-backups"))   # outside the project; same name on Node-3
REMOTE_HOST, REMOTE_DIR = f"tmc@{config.NODE_BY_NAME['Node-3'].lan_ip}", "aiops-backups"
KEEP = 14
SKIP = {"backups", "api.log"}
COUNT_TABLES = ("incidents", "audit_events", "approval_requests", "execution_records", "evidence_records")


def _counts(db: Path) -> dict[str, int]:
    c = sqlite3.connect(db)
    try:
        have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in COUNT_TABLES if t in have}
    finally:
        c.close()


def create() -> tuple[Path, dict]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = Path(tempfile.mkdtemp(prefix="aiops-backup-"))
    try:
        for f in sorted(DATA.iterdir()):
            if f.name in SKIP or f.name.endswith(("-wal", "-shm")):
                continue
            if f.suffix == ".db":
                src, dst = sqlite3.connect(f), sqlite3.connect(work / f.name)
                with dst:
                    src.backup(dst)             # consistent snapshot while the platform keeps writing
                src.close(), dst.close()
            elif f.is_dir():
                shutil.copytree(f, work / f.name)
            else:
                shutil.copy2(f, work / f.name)
        manifest = {"created": time.time(), "host": "Node-2", "files": sorted(p.name for p in work.iterdir()),
                    "counts": _counts(work / config.DB_PATH.name)}
        (work / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
        LOCAL.mkdir(parents=True, exist_ok=True)
        LOCAL.chmod(0o700)
        out = LOCAL / f"aiops-{stamp}.tar.gz"
        with tarfile.open(out, "w:gz") as tar:
            tar.add(work, arcname=f"aiops-{stamp}")
        out.chmod(0o600)
        return out, manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def verify(archive: Path) -> dict:
    """Restore to scratch and check it. Returns named checks; all must be true."""
    scratch = Path(tempfile.mkdtemp(prefix="aiops-restore-"))
    try:
        with tarfile.open(archive) as tar:
            tar.extractall(scratch, filter="data")
        root = next(scratch.iterdir())
        manifest = json.loads((root / "MANIFEST.json").read_text())
        checks: dict[str, bool] = {}
        for db in sorted(root.glob("*.db")):
            c = sqlite3.connect(db)
            checks[f"{db.name} integrity_check"] = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            c.close()
        main = root / config.DB_PATH.name
        chain = store.verify_audit_chain(main)
        checks["audit hash chain verifies on the restored copy"] = chain["ok"]
        checks["row counts match the manifest"] = _counts(main) == manifest["counts"]
        for name in ("users.json", "gateway_keys.json"):
            checks[f"{name} present and parses"] = (root / name).exists() and bool(json.loads((root / name).read_text()))
        return {"checks": checks, "audit_events": chain.get("events"), "counts": manifest["counts"]}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def offsite(archive: Path) -> bool:
    try:
        subprocess.run(["ssh", "-o", "BatchMode=yes", REMOTE_HOST, f"mkdir -p {REMOTE_DIR} && chmod 700 {REMOTE_DIR}"],
                       check=True, timeout=30, capture_output=True)
        subprocess.run(["scp", "-q", "-p", "-o", "BatchMode=yes", str(archive), f"{REMOTE_HOST}:{REMOTE_DIR}/"],
                       check=True, timeout=300, capture_output=True)
        subprocess.run(["ssh", "-o", "BatchMode=yes", REMOTE_HOST,
                        f"cd {REMOTE_DIR} && chmod 600 *.tar.gz && ls -1t aiops-*.tar.gz | tail -n +{KEEP + 1} | xargs -r rm --"],
                       check=True, timeout=30, capture_output=True)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def prune() -> None:
    for old in sorted(LOCAL.glob("aiops-*.tar.gz"))[:-KEEP]:
        old.unlink()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", type=Path)
    args = ap.parse_args()
    if args.verify:
        r = verify(args.verify)
        print(json.dumps(r, indent=1))
        return 0 if all(r["checks"].values()) else 1
    t0 = time.time()
    archive, manifest = create()
    r = verify(archive)
    r["checks"]["copied to Node-3"] = offsite(archive)
    prune()
    ok = sum(r["checks"].values())
    evals.record("backup-restore", ok, len(r["checks"]), r["checks"],
                 {"archive": archive.name, "bytes": archive.stat().st_size, "seconds": round(time.time() - t0, 1),
                  "audit_events": r["audit_events"], "counts": r["counts"]})
    print(f"{archive.name}: {ok}/{len(r['checks'])} checks, {archive.stat().st_size / 1e6:.1f} MB")
    for k, v in r["checks"].items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    return 0 if ok == len(r["checks"]) else 1


if __name__ == "__main__":
    sys.exit(main())
