"""Atomic per-task result store for stage-4.

Layout under <out_root>:
    rows/<task_name>.json   authoritative per-task record (one writer: the task)
    results.json            derived aggregate (compatibility view used by reports)
    .results.lock           advisory lock guarding results.json read-modify-write

Every write is a same-directory atomic replace.  Readers never observe a
partially written document, and two workers can never clobber each other's
rows the way the old unlocked read-modify-write of results.json could.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from plugins.locks import FileLock

TERMINAL = {"submitted", "no_submission", "submit_error", "worker_error",
            "gen_fail", "api_error"}


def task_name(tid: str) -> str:
    return tid.replace(":", "_")


def rows_dir(out) -> Path:
    return Path(out) / "rows"


def row_path(out, tid: str) -> Path:
    return rows_dir(out) / (task_name(tid) + ".json")


def _chown_root(path: Path) -> None:
    if hasattr(os, "chown") and hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            os.chown(path, 0, 0)
        except OSError:
            pass


def _atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        _chown_root(path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _load_results(out) -> list:
    rp = Path(out) / "results.json"
    if not rp.exists():
        return []
    try:
        data = json.load(open(rp, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [r for r in read_rows(out)]
    return data if isinstance(data, list) else []


def read_rows(out) -> list:
    """Rows from the per-task files (sorted by task name)."""
    out_rows = []
    for path in sorted(rows_dir(out).glob("*.json")):
        try:
            out_rows.append(json.load(open(path, encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out_rows


def read_row(out, tid: str):
    path = row_path(out, tid)
    if not path.exists():
        return None
    try:
        return json.load(open(path, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_row(out, tid: str, row: dict) -> None:
    """Write only the per-task file (callers rebuild results.json separately)."""
    _atomic_json(row_path(out, tid), row)


def merge_row(out, tid: str, patch: dict) -> dict:
    """Merge a patch into one row; atomically rewrites rows/ and results.json."""
    out = Path(out)
    lock_path = out / ".results.lock"
    with FileLock(str(lock_path)):
        rows = _load_results(out)
        current = None
        for row in rows:
            if row.get("task_id") == tid:
                current = row
                break
        if current is None:
            current = {"task_id": tid}
            rows.append(current)
        current.update(patch)
        current["task_id"] = tid
        _atomic_json(out / "results.json", rows)
        _atomic_json(row_path(out, tid), current)
    return current


def rebuild(out, order=None) -> list:
    """Rebuild results.json from rows/*.json (order = optional tid list)."""
    out = Path(out)
    by_id = {r["task_id"]: r for r in read_rows(out) if r.get("task_id")}
    legacy = {r["task_id"]: r for r in _load_results(out) if r.get("task_id")}
    # rows/ wins; keep legacy rows that never produced a row file
    for tid, row in legacy.items():
        by_id.setdefault(tid, row)
    if order:
        ordered = [by_id[t] for t in order if t in by_id]
        ordered += [by_id[t] for t in sorted(by_id) if t not in set(order)]
    else:
        ordered = [by_id[t] for t in sorted(by_id)]
    with FileLock(str(out / ".results.lock")):
        _atomic_json(out / "results.json", ordered)
    return ordered


def is_terminal(row) -> bool:
    if not row:
        return False
    if row.get("status") in TERMINAL:
        return True
    # legacy rows without a status field: a scored boolean is terminal
    return row.get("status") is None and row.get("official_success") is not None
