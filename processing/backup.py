"""
Nightly backup of Postgres + Qdrant — prerequisite A0 of the "more autonomy"
plan.

Until 29 Sep there was none: no Time Machine destination, no dump job. The
only copies were a few `pre-merge-*.sql` files taken by hand before bulk
merges. That was survivable while every destructive action had a person in
front of it. It stops being survivable the moment a job merges records
unattended, so auto-merge (processing/auto_merge.py) refuses to run unless
`latest_backup_age_hours()` says a fresh backup exists.

Layout: backups/nightly/YYYY-MM-DD/
    postgres.sql.gz           pg_dump, run inside the vc_postgres container
    qdrant_<collection>.snapshot
Only `backups/nightly/` is rotated (last KEEP_DAYS). The hand-taken
`backups/pre-*.sql` files are never touched. `backups/` is gitignored.

No local pg_dump exists on this machine, so the dump runs via
`docker exec vc_postgres pg_dump` — the API's launchd PATH includes
/usr/local/bin, where docker lives.
"""
import gzip
import logging
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent / "backups" / "nightly"
KEEP_DAYS = 14
_PG_CONTAINER = "vc_postgres"
_MIN_DUMP_BYTES = 100_000     # a real dump of this DB is tens of MB; a tiny one is a failure


def _dump_postgres(out: Path) -> int:
    """Stream pg_dump through gzip. Returns bytes written; raises on failure."""
    cmd = ["docker", "exec", _PG_CONTAINER, "pg_dump", "-U", "scout", "-d", "vc_scouting",
           "--no-owner", "--clean", "--if-exists"]
    with gzip.open(out, "wb") as gz:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        shutil.copyfileobj(proc.stdout, gz)
        _, err = proc.communicate(timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump exited {proc.returncode}: {err.decode()[:300]}")
    size = out.stat().st_size
    if size < _MIN_DUMP_BYTES:
        raise RuntimeError(f"pg_dump produced only {size} bytes — refusing to call that a backup")
    # Prove the gzip is complete, not truncated mid-stream.
    with gzip.open(out, "rb") as gz:
        while gz.read(1 << 20):
            pass
    return size


def _snapshot_qdrant(out_dir: Path) -> dict:
    """One snapshot per collection, downloaded, then deleted server-side so
    snapshots don't accumulate inside the container volume."""
    import httpx
    from config import settings

    base = f"http://{settings.qdrant_host}:{settings.qdrant_port}"
    sizes = {}
    with httpx.Client(timeout=300) as c:
        names = [x["name"] for x in c.get(f"{base}/collections").json()["result"]["collections"]]
        for name in names:
            snap = c.post(f"{base}/collections/{name}/snapshots").json()["result"]["name"]
            dest = out_dir / f"qdrant_{name}.snapshot"
            with c.stream("GET", f"{base}/collections/{name}/snapshots/{snap}") as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
            c.delete(f"{base}/collections/{name}/snapshots/{snap}")
            sizes[name] = dest.stat().st_size
    return sizes


def _rotate() -> int:
    cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).date()
    removed = 0
    for d in ROOT.iterdir() if ROOT.exists() else []:
        try:
            day = datetime.strptime(d.name, "%Y-%m-%d").date()
        except ValueError:
            continue                       # not ours — never delete it
        if day < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def run_backup() -> dict:
    """
    Take tonight's backup. Never raises — returns a stats dict with "error"
    set on failure, so the scheduler can record it and the auto-merge gate
    (which checks for a *successful* backup) simply stays closed.
    """
    day_dir = ROOT / datetime.now().strftime("%Y-%m-%d")
    tmp = day_dir.with_name(day_dir.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    stats = {}
    try:
        stats["postgres_bytes"] = _dump_postgres(tmp / "postgres.sql.gz")
        stats["qdrant_bytes"] = _snapshot_qdrant(tmp)
        # Only a complete backup gets the dated name that the freshness
        # check looks for — a half-finished one stays ".partial".
        shutil.rmtree(day_dir, ignore_errors=True)
        tmp.rename(day_dir)
        stats["path"] = str(day_dir)
        stats["rotated"] = _rotate()
    except Exception as exc:
        logger.error(f"[Backup] failed: {type(exc).__name__}: {exc}")
        stats["error"] = f"{type(exc).__name__}: {exc}"
    return stats


def latest_backup_age_hours():
    """Hours since the newest COMPLETE backup, or None if there is none."""
    if not ROOT.exists():
        return None
    complete = [d for d in ROOT.iterdir()
                if d.is_dir() and not d.name.endswith(".partial")
                and (d / "postgres.sql.gz").exists()]
    if not complete:
        return None
    newest = max(complete, key=lambda d: (d / "postgres.sql.gz").stat().st_mtime)
    age = datetime.now().timestamp() - (newest / "postgres.sql.gz").stat().st_mtime
    return age / 3600
