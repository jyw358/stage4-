#!/usr/bin/env python3
"""stage4.py -- clean-run queue driver (stage-4).

One authoritative per-task path: this driver prepares the task dir, calls
run_agent_task.sh (fresh workspace + ledger + row), then scores the whole run
with score_all.py (final-only) and runs verify_stage4.py.

  python3 stage4.py --queue /data/cybergym/stage4/queue.json \
      --out /data/cybergym/stage4/r_engineer --workers 3 [--limit 50]

Resume: re-running with the same --out reuses <out>/run_id and skips tasks
whose row is execution-terminal for that run (a row that is terminal but
unscored goes to the scoring phase, it is never re-RUN: B9).  --retry-failed
re-runs tasks whose official_success is False (or that ended in
worker_error/gen_fail).

R2 fixes:
  B7  backfill/score/verify exit codes propagate: any failed phase makes the
      whole round exit non-zero; a missing phase script is a failure, not a
      warning.
  B9  terminal states split into execution vs scoring; scoring-incomplete
      rows are listed in batch_summary and must be resolved by scoring,
      never reported as completed tasks.
  B12 each task runs in its own session/process group; on timeout the whole
      tree is terminated (SIGTERM, then SIGKILL) before the row is written,
      so a timed-out attempt cannot overlap with a retry.
  MAJOR run-level exclusive lock on <out>/.run.lock; atomic run_meta.json /
      run_id / rows_index.json / batch_summary.json writes; archive names get
      a UUID suffix and archiving happens under the results lock; task-id ->
      file-name collisions are refused up front; the filesystem under --out
      must support local locking for multi-worker runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX only; the driver never runs on Windows in production
    import pwd
except ImportError:  # pragma: no cover
    pwd = None

AG = os.environ.get("S4_AGENT_HOME", "/home/naco/work/cybergym-agent")
REPO = os.environ.get("S4_REPO", "/home/naco/work/cybergym")
DATA = os.environ.get("S4_DATA", "/data/cybergym/cybergym_data/data")
SERVER = os.environ.get("S4_SERVER", "http://172.17.0.1:8666")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugins import results_store  # noqa: E402
from plugins.locks import FileLock, LockBusy, assert_local_lockable  # noqa: E402

EXECUTION_FAILURE_STATUSES = ("worker_error", "gen_fail")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def euid() -> int:
    return os.geteuid() if hasattr(os, "geteuid") else -1


def atomic_write(path: Path, text: str) -> None:
    """Same-directory atomic replace for driver-owned metadata files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def chown_naco(path) -> None:
    """gen_task and the workspace generator run as naco: the dirs they write
    into must be owned by naco even though this driver runs as root."""
    if pwd is None or euid() != 0:
        return
    try:
        pw = pwd.getpwnam("naco")
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except (KeyError, OSError):
        pass


def load_queue(path: Path) -> list:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
        if isinstance(data, dict) and "queue" in data:
            return [str(x).strip() for x in data["queue"] if str(x).strip()]
    except json.JSONDecodeError:
        pass
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def queue_digest(queue: list) -> str:
    return hashlib.sha256("\n".join(queue).encode()).hexdigest()[:16]


def prepare_task(tid: str, task_base: Path, run_id: str, log) -> tuple[bool, str]:
    """Prepare the task dir.  A pre-generated dir is only reused when it was
    created by THIS run: submit.sh carries a run-scoped agent_id, and reusing a
    stage-3/older dir would mix old server records into this run's final.

    R2 MAJOR: the run marker is root-owned 0644 and must still be root-owned
    on reuse -- the run/agent binding must not be re-writable by the untrusted
    execution account that owns the task dir."""
    tdir = task_base / tid.replace(":", "_")
    marker = tdir / ".stage4_run_id"
    if (tdir / "submit.sh").exists():
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == run_id:
            if os.name == "posix" and euid() == 0 and marker.stat().st_uid != 0:
                return False, (f"{marker} is not root-owned; run/agent binding "
                               "is not trustworthy (regenerate the task dir)")
            return True, ""
        return False, (f"{tdir} has submit.sh but no marker for run {run_id}; "
                       "refusing to reuse a pre-generated agent_id "
                       "(use a fresh --task-base)")
    tdir.mkdir(parents=True, exist_ok=True)
    chown_naco(tdir)
    cmd = [
        "sudo", "-u", "naco", "env", f"PYTHONPATH={REPO}/src",
        "/usr/bin/python3.12", "-m", "cybergym.task.gen_task",
        "--task-id", tid, "--out-dir", str(tdir),
        "--data-dir", DATA, "--server", SERVER,
        "--mask-map", f"{REPO}/mask_map.json", "--difficulty", "level1",
    ]
    log(f"[gen] {tid}")
    try:
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                              timeout=600)
    except subprocess.TimeoutExpired:
        return False, "gen_task timeout"
    if not (tdir / "submit.sh").exists():
        tail = (proc.stderr or proc.stdout or "")[-400:].replace("\n", " ")
        return False, f"gen_task failed rc={proc.returncode}: {tail}"
    marker.write_text(run_id + "\n", encoding="utf-8")
    if os.name == "posix" and euid() == 0:
        os.chown(marker, 0, 0)
        os.chmod(marker, 0o644)
    return True, ""


def kill_task_tree(proc: subprocess.Popen) -> None:
    """B12: terminate the WHOLE process group of a task (the wrapper starts a
    session, so sudo/agcli/timeout descendants are all inside it), then
    escalate to SIGKILL after a grace period.  Only when the tree is confirmed
    gone may the driver archive/retry the task."""
    if os.name == "posix":
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=15)
                return
            except subprocess.TimeoutExpired:
                continue
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - last resort
            pass
    else:  # Windows local tests: no process groups, kill the direct child
        proc.kill()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass


def run_one(tid: str, args, task_base: Path, run_id: str) -> dict:
    name = tid.replace(":", "_")
    log_dir = Path(args.out) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}.log"
    tdir = task_base / name
    with open(log_path, "a", encoding="utf-8") as logfh:
        def log(msg):
            logfh.write(msg.rstrip() + "\n")
            logfh.flush()

        ok, err = prepare_task(tid, task_base, run_id, log)
        if not ok:
            results_store.merge_row(args.out, tid, {
                "task_id": tid, "run_id": run_id, "status": "gen_fail",
                "error": err, "finished_at": utcnow(),
                "official_success": None,
            })
            return {"task_id": tid, "status": "gen_fail", "error": err}

        log(f"[run] {tid} -> {tdir}")
        cmd = ["bash", f"{AG}/scripts/run_agent_task.sh", tid, str(tdir), str(args.out)]
        env = dict(os.environ, S4_RUN_ID=run_id, S4_WSROOT=str(args.ws_root),
                   S4_PASS1_TURNS=str(args.pass1_turns),
                   S4_PASS2_TURNS=str(args.pass2_turns))
        rc = None
        if os.name == "posix":
            # B12: own session per task -> the whole tree is killable on timeout
            proc = subprocess.Popen(cmd, stdout=logfh, stderr=subprocess.STDOUT,
                                    env=env, start_new_session=True)
            try:
                rc = proc.wait(timeout=args.task_timeout)
            except subprocess.TimeoutExpired:
                rc = 124
                log(f"[timeout] {tid} after {args.task_timeout}s; killing the process group")
                kill_task_tree(proc)
                log(f"[timeout] {tid} process group terminated")
        else:  # Windows local tests
            try:
                proc = subprocess.run(cmd, stdout=logfh, stderr=subprocess.STDOUT,
                                      env=env, timeout=args.task_timeout)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                rc = 124
                log(f"[timeout] {tid} after {args.task_timeout}s")
        row = results_store.read_row(args.out, tid)
        if not row:
            results_store.merge_row(args.out, tid, {
                "task_id": tid, "run_id": run_id, "status": "worker_error",
                "error": f"run_agent_task rc={rc}, no row written",
                "finished_at": utcnow(), "official_success": None,
            })
            row = results_store.read_row(args.out, tid)
        log(f"[done] {tid} rc={rc} status={row.get('status')} "
            f"subs={row.get('submissions')}")
        return {"task_id": tid, "status": row.get("status"), "rc": rc}


def archive_attempt(out, tid: str, run_id: str) -> None:
    """Keep the previous attempt's row, then clear it for the new attempt.

    R2 MAJOR: the whole read-archive-clear sequence runs under the results
    lock, and the archive name carries a UUID suffix so two rapid attempts
    can never collide on the second-resolution timestamp."""
    out = Path(out)
    with FileLock(str(out / ".results.lock")):
        row = results_store.read_row(out, tid)
        if not row or row.get("run_id") not in (None, run_id):
            return
        arch = out / "rows" / "attempts"
        arch.mkdir(parents=True, exist_ok=True)
        stamp = (utcnow().replace(":", "").replace("-", "")
                 + "-" + uuid.uuid4().hex[:8])
        (arch / f"{tid.replace(':', '_')}.{stamp}.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        row.update({"run_id": run_id, "status": "running", "official_success": None,
                    "score_status": "pending", "final": None,
                    "attempt": int(row.get("attempt") or 0) + 1,
                    "attempted_at": utcnow()})
        results_store.write_row(out, tid, row)


def select_todo(queue: list, out, run_id: str, retry_failed: bool = False):
    """B9: split the queue into (todo, scoring_recovery).

    todo               tasks whose AGENT phase must (re)run
    scoring_recovery   execution-terminal rows without a scoring verdict --
                       they go to the scoring phase, never back to the agent
    """
    todo, scoring_recovery = [], []
    for tid in queue:
        row = results_store.read_row(out, tid)
        if not row or row.get("run_id") != run_id:
            todo.append(tid)
            continue
        retry = (retry_failed and (
            row.get("official_success") is False
            or row.get("status") in EXECUTION_FAILURE_STATUSES))
        if retry or not results_store.is_execution_terminal(row):
            todo.append(tid)   # crashed mid-run without a terminal status
        elif not results_store.is_scoring_terminal(row):
            scoring_recovery.append(tid)
    return todo, scoring_recovery


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", required=True)
    ap.add_argument("--out", default="/data/cybergym/stage4/r_engineer")
    ap.add_argument("--task-base", default="")
    ap.add_argument("--ws-root", default="")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--no-score", action="store_true")
    ap.add_argument("--no-backfill", action="store_true")
    ap.add_argument("--skip-verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pass1-turns", type=int, default=45)
    ap.add_argument("--pass2-turns", type=int, default=15)
    ap.add_argument("--task-timeout", type=int, default=7200)
    args = ap.parse_args()

    if euid() != 0 and not args.dry_run:
        raise SystemExit("stage4.py must run as root (it starts the root wrapper)")
    out = Path(args.out)
    queue_path = Path(args.queue)
    if not queue_path.exists():
        raise SystemExit(f"queue not found: {queue_path}")
    queue = load_queue(queue_path)
    if not queue:
        raise SystemExit("queue is empty")
    try:
        results_store.assert_unique_task_ids(queue)   # R2 MAJOR: file-name collisions
    except results_store.ResultsIntegrityError as e:
        raise SystemExit(str(e))
    task_base = Path(args.task_base or (out / "tasks"))
    ws_root = Path(args.ws_root or "/data/cybergym/stage4/r_engineer-ws")
    for d in (out, out / "rows", out / "logs", task_base, ws_root):
        d.mkdir(parents=True, exist_ok=True)
    chown_naco(task_base)
    chown_naco(ws_root)

    # R2 MAJOR: multi-worker locking needs a local filesystem
    lockable, fstype = assert_local_lockable(out, multi_worker=args.workers > 1)
    if not lockable:
        raise SystemExit(f"--out is on {fstype}; flock semantics are not "
                         f"equivalent to a local disk -- refuse to run "
                         f"{args.workers} workers (use a local --out)")

    # R2 MAJOR: one driver per --out; a second concurrent driver would race
    # on todo selection, archiving and phase execution
    try:
        run_lock = FileLock(str(out / ".run.lock"), timeout=0)
        run_lock.__enter__()
    except LockBusy:
        raise SystemExit(f"another stage4 driver holds {out}/.run.lock; "
                         "refusing to run concurrently")

    try:
        return _main_locked(args, out, queue_path, queue, task_base, ws_root)
    finally:
        run_lock.__exit__(None, None, None)


def _main_locked(args, out: Path, queue_path: Path, queue: list,
                 task_base: Path, ws_root: Path) -> int:
    run_id = args.run_id
    run_id_file = out / "run_id"
    if not run_id:
        if run_id_file.exists():
            run_id = run_id_file.read_text(encoding="utf-8").strip()
        else:
            run_id = "s4-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    atomic_write(run_id_file, run_id + "\n")

    meta_path = out / "run_meta.json"
    meta = {}
    if meta_path.exists():
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except json.JSONDecodeError:
            meta = {}
    meta.setdefault("started_at", utcnow())
    meta.update({"run_id": run_id, "queue": str(queue_path), "queue_size": len(queue),
                 "queue_sha256": queue_digest(queue), "host": socket.gethostname(),
                 "workers": args.workers, "task_base": str(task_base),
                 "ws_root": str(ws_root)})
    atomic_write(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))
    atomic_write(out / "rows_index.json", json.dumps(queue))

    todo, scoring_recovery = select_todo(queue, out, run_id, args.retry_failed)
    if args.limit:
        todo = todo[:args.limit]

    for tid in todo:
        archive_attempt(out, tid, run_id)

    print(f"[stage4] run_id={run_id} queue={len(queue)} todo={len(todo)} "
          f"scoring_recovery={len(scoring_recovery)} "
          f"workers={args.workers} out={out}")

    execution_failures = []
    if args.dry_run:
        print("TODO:\n" + "\n".join(todo[:200]))
        if scoring_recovery:
            print("SCORING-RECOVERY (agent NOT re-run):\n"
                  + "\n".join(scoring_recovery[:200]))
        return 0
    if not todo:
        print("[stage4] nothing to do")
    else:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futs = {pool.submit(run_one, tid, args, task_base, run_id): tid
                    for tid in todo}
            for fut in as_completed(futs):
                tid = futs[fut]
                try:
                    res = fut.result()
                    print(f"[stage4] {tid}: {res.get('status')}")
                    if res.get("status") in EXECUTION_FAILURE_STATUSES:
                        execution_failures.append(tid)
                except Exception as e:
                    print(f"[stage4] {tid}: EXC {type(e).__name__}: {e}")
                    execution_failures.append(tid)

    results_store.rebuild(out, order=queue)

    # ---- B7: every phase's exit code decides the round's outcome ----------
    # A missing phase script is a FAILED phase, not a warning: a round whose
    # fix-side backfill never ran must not be published as scored.
    phase_rc: dict = {}

    def run_phase(name: str, cmd: list) -> None:
        print(f"[stage4] {name}: {' '.join(str(c) for c in cmd)}")
        r = subprocess.run(cmd, check=False)
        phase_rc[name] = r.returncode
        if r.returncode != 0:
            print(f"[stage4] {name} FAILED rc={r.returncode}")

    if not args.no_backfill:
        backfill = os.environ.get("S4_BACKFILL", "/root/backfill_fix_side.sh")
        if Path(backfill).exists():
            run_phase("backfill", ["bash", backfill])
        else:
            print(f"[stage4] backfill script not found at {backfill}")
            phase_rc["backfill"] = 127
    if not args.no_score:
        score_all = os.environ.get("S4_SCORE_ALL", "/root/score_all.sh")
        if Path(score_all).exists():
            run_phase("score", ["bash", score_all, str(out), str(task_base), str(ws_root)])
        else:
            print(f"[stage4] score_all script not found at {score_all}")
            phase_rc["score"] = 127
    if not args.skip_verify:
        verify = Path(AG) / "scripts" / "verify_stage4.py"
        if verify.exists():
            run_phase("verify", [sys.executable, str(verify), "--out", str(out),
                                 "--queue", str(queue_path),
                                 "--ws-root", str(ws_root)])
        else:
            print(f"[stage4] verify script not found at {verify}")
            phase_rc["verify"] = 127

    phase_failures = sorted(k for k, v in phase_rc.items() if v != 0)

    # rows still without a scoring verdict after the scoring phase ran
    still_unscored = []
    for tid in queue:
        row = results_store.read_row(out, tid)
        if (row is not None and row.get("run_id") == run_id
                and results_store.is_execution_terminal(row)
                and not results_store.is_scoring_terminal(row)):
            still_unscored.append(tid)

    summary = {
        "run_id": run_id, "finished_at": utcnow(), "todo": len(todo),
        "execution_failures": sorted(execution_failures),
        "scoring_pending": sorted(still_unscored),
        "phase_exit_codes": phase_rc,
        "phase_failures": phase_failures,
    }
    atomic_write(out / "batch_summary.json",
                 json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[stage4] done run_id={run_id} execution_failures={len(execution_failures)} "
          f"scoring_pending={len(still_unscored)} phase_failures={phase_failures or 'none'}")
    if execution_failures or still_unscored or phase_failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
