"""Atomic per-task result store for stage-4.

Layout under <out_root>:
    rows/<task_name>.json   authoritative per-task record (one writer: the task)
    results.json            derived aggregate (compatibility view used by reports)
    .results.lock           advisory lock guarding results.json read-modify-write

Every write is a same-directory atomic replace.  Readers never observe a
partially written document, and two workers can never clobber each other's
rows the way the old unlocked read-modify-write of results.json could.

R2 review fixes (B8/B9):
  - rows/ is the ONLY data source.  rebuild() no longer merges legacy rows
    from results.json, so a stale aggregate can never resurrect an old
    success whose per-task file is gone.
  - merge_row() writes the authoritative per-task file FIRST and the derived
    aggregate second; a crash between the two leaves the aggregate merely
    stale (fixed by the next rebuild), never ahead of the authority.
  - terminal states are split: ``is_execution_terminal`` (the agent phase is
    done) vs ``is_scoring_terminal`` (a scoring verdict exists).  A row that
    is execution-terminal but scoring-incomplete must be resumed by scoring,
    never skipped as "done".
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from plugins.locks import FileLock

TERMINAL = {"submitted", "no_submission", "submit_error", "worker_error",
            "gen_fail", "api_error",
            # R3.5: an UNPROVEN process boundary must never be re-run by a
            # resumed run (it would otherwise look like "crashed mid-run").
            # Unlike agent_launch_error/evidence_setup_error, these two are
            # also absent from stage4.EXECUTION_FAILURE_STATUSES, so even
            # --retry-failed leaves them alone until an operator inspects the
            # host and clears the status by hand.
            "process_residual_error", "process_inspection_incomplete"}

# score_status values that mean scoring has NOT produced a verdict yet:
# None/pending = never scored, query_error/no_task_dir = infra failure worth
# a re-score.  Everything else (ok, fix_pending, no_records, no_ledger,
# ledger_mismatch, hash_mismatch, ...) is a completed, fail-closed verdict.
SCORE_INCOMPLETE = {None, "pending", "query_error", "no_task_dir"}


class ResultsIntegrityError(RuntimeError):
    """A per-task row file is unreadable or corrupt; never silently skipped."""


def task_name(tid: str) -> str:
    return tid.replace(":", "_")


def assert_unique_task_ids(tids) -> None:
    """Refuse queues whose task ids collide under the ':' -> '_' file mapping.

    'a:b' and 'a_b' would map to the same rows/<name>.json; that must be
    caught before the run starts, not discovered as two tasks clobbering
    one file (R2 MAJOR).
    """
    seen = {}
    for tid in tids:
        name = task_name(tid)
        if name in seen and seen[name] != tid:
            raise ResultsIntegrityError(
                f"task ids {seen[name]!r} and {tid!r} both map to rows/{name}.json")
        seen[name] = tid


def rows_dir(out) -> Path:
    return Path(out) / "rows"


def row_path(out, tid: str) -> Path:
    return rows_dir(out) / (task_name(tid) + ".json")


def _chown_root(path: Path) -> None:
    if hasattr(os, "chown") and hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            os.chown(path, 0, 0)
        except OSError as e:
            # an unexpected permission setup must be visible, not silent (MINOR)
            print(f"[results_store] WARN: chown root failed for {path}: {e}",
                  file=sys.stderr)


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
        return []
    return data if isinstance(data, list) else []


def read_rows(out, strict: bool = False) -> list:
    """Rows from the per-task files (sorted by task name).

    With ``strict=True`` a corrupt row file raises ResultsIntegrityError
    instead of being silently skipped -- a missing row must never quietly
    shrink the denominator of a report (R2 MAJOR).
    """
    out_rows = []
    corrupt = []
    for path in sorted(rows_dir(out).glob("*.json")):
        try:
            out_rows.append(json.load(open(path, encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            corrupt.append(f"{path.name}: {type(e).__name__}")
    if corrupt and strict:
        raise ResultsIntegrityError(
            "corrupt row files: " + "; ".join(corrupt))
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
    """Merge a patch into one row; atomically rewrites rows/ and results.json.

    The authoritative per-task file is written FIRST; the derived aggregate
    second.  A crash between the two can only leave the aggregate stale
    (the next rebuild fixes it); it can never make the aggregate claim a
    row the authority does not have (B8).
    """
    out = Path(out)
    lock_path = out / ".results.lock"
    with FileLock(str(lock_path)):
        current = read_row(out, tid)          # rows/ is the only authority
        if current is None:
            current = {"task_id": tid}
        elif patch.get("run_id") and current.get("run_id") not in (None, patch["run_id"]):
            # R3.3 (real-task smoke): a NEW run must not inherit the previous
            # run's accounting.  record_task_row already resets its in-memory
            # row when the run_id changes, but merging the patch with the STALE
            # file on disk put the old session's turns/cost/result back into the
            # new run's row (the row then reported a foreign session's outcome).
            current = {"task_id": tid}
        current.update(patch)
        current["task_id"] = tid
        _atomic_json(row_path(out, tid), current)          # authority first
        _atomic_json(out / "results.json", _aggregate(out))  # derived second
    return current


def _aggregate(out) -> list:
    """The aggregate is always re-derived from rows/ (never edited in place)."""
    by_id = {r["task_id"]: r for r in read_rows(out) if r.get("task_id")}
    return [by_id[t] for t in sorted(by_id)]


def rebuild(out, order=None) -> list:
    """Rebuild results.json from rows/*.json ONLY (order = optional tid list).

    R2/B8: legacy rows living only in results.json are deliberately dropped --
    rows that were cleared for a re-run must stay cleared; resurrecting an
    old success from a stale aggregate is exactly the bug this prevents.
    """
    out = Path(out)
    by_id = {r["task_id"]: r for r in read_rows(out) if r.get("task_id")}
    if order:
        ordered = [by_id[t] for t in order if t in by_id]
        ordered += [by_id[t] for t in sorted(by_id) if t not in set(order)]
    else:
        ordered = [by_id[t] for t in sorted(by_id)]
    with FileLock(str(out / ".results.lock")):
        _atomic_json(out / "results.json", ordered)
    return ordered


def is_execution_terminal(row) -> bool:
    """The agent phase reached a terminal status (scored or not)."""
    if not row:
        return False
    if row.get("status") in TERMINAL:
        return True
    # legacy rows without a status field: a scored boolean is terminal
    return row.get("status") is None and row.get("official_success") is not None


def is_scoring_terminal(row) -> bool:
    """A scoring verdict exists (even a fail-closed one).  B9: a row that is
    execution-terminal but NOT scoring-terminal must be resumed by scoring,
    never reported as a completed task."""
    if not is_execution_terminal(row):
        return False
    return row.get("score_status") not in SCORE_INCOMPLETE


def is_terminal(row) -> bool:
    """Back-compat alias: execution terminality (used to skip re-RUNNING a task).

    Callers that need "fully done" must use is_scoring_terminal instead.
    """
    return is_execution_terminal(row)
