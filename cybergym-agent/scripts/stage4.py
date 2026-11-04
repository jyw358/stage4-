#!/usr/bin/env python3
"""stage4.py -- clean-run queue driver (stage-4).

One authoritative per-task path: this driver prepares the task dir, calls
run_agent_task.sh (fresh workspace + ledger + row), then scores the whole run
with score_all.py (final-only) and runs verify_stage4.py.

  python3 stage4.py --queue /data/cybergym/stage4/queue.json \
      --out /data/cybergym/stage4/r_engineer --workers 3 [--limit 50]

Resume: re-running with the same --out reuses <out>/run_id and skips tasks
whose row is terminal for that run.  --retry-failed re-runs tasks whose
official_success is False (or that ended in worker_error/gen_fail).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
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


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def euid() -> int:
    return os.geteuid() if hasattr(os, "geteuid") else -1


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
    stage-3/older dir would mix old server records into this run's final."""
    tdir = task_base / tid.replace(":", "_")
    marker = tdir / ".stage4_run_id"
    if (tdir / "submit.sh").exists():
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == run_id:
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
    return True, ""


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
    """Keep the previous attempt's row, then clear it for the new attempt."""
    row = results_store.read_row(out, tid)
    if not row or row.get("run_id") not in (None, run_id):
        return
    arch = Path(out) / "rows" / "attempts"
    arch.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().replace(":", "").replace("-", "")
    (arch / f"{tid.replace(':', '_')}.{stamp}.json").write_text(
        json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    row.update({"run_id": run_id, "status": "running", "official_success": None,
                "score_status": "pending", "final": None,
                "attempt": int(row.get("attempt") or 0) + 1,
                "attempted_at": utcnow()})
    results_store.write_row(out, tid, row)


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
    task_base = Path(args.task_base or (out / "tasks"))
    ws_root = Path(args.ws_root or "/data/cybergym/stage4/r_engineer-ws")
    for d in (out, out / "rows", out / "logs", task_base, ws_root):
        d.mkdir(parents=True, exist_ok=True)
    chown_naco(task_base)
    chown_naco(ws_root)

    run_id = args.run_id
    run_id_file = out / "run_id"
    if not run_id:
        if run_id_file.exists():
            run_id = run_id_file.read_text(encoding="utf-8").strip()
        else:
            run_id = "s4-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
            run_id_file.write_text(run_id + "\n", encoding="utf-8")
    (out / "run_id").write_text(run_id + "\n", encoding="utf-8")

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
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "rows_index.json").write_text(json.dumps(queue), encoding="utf-8")

    todo = []
    for tid in queue:
        row = results_store.read_row(out, tid)
        if row and row.get("run_id") == run_id:
            if args.retry_failed:
                if row.get("official_success") is False or row.get("status") in ("worker_error", "gen_fail"):
                    todo.append(tid)
            elif results_store.is_terminal(row):
                continue
            else:
                todo.append(tid)   # crashed mid-run without a terminal status
        else:
            todo.append(tid)
    if args.limit:
        todo = todo[:args.limit]

    for tid in todo:
        archive_attempt(out, tid, run_id)

    print(f"[stage4] run_id={run_id} queue={len(queue)} todo={len(todo)} "
          f"workers={args.workers} out={out}")
    failures = []
    if args.dry_run:
        print("\n".join(todo[:200]))
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
                    if res.get("status") in ("worker_error", "gen_fail"):
                        failures.append(tid)
                except Exception as e:
                    print(f"[stage4] {tid}: EXC {type(e).__name__}: {e}")
                    failures.append(tid)

    results_store.rebuild(out, order=queue)
    if not args.no_backfill:
        backfill = os.environ.get("S4_BACKFILL", "/root/backfill_fix_side.sh")
        if Path(backfill).exists():
            print(f"[stage4] fix-side backfill: {backfill}")
            subprocess.run(["bash", backfill], check=False)
        else:
            print(f"[stage4] backfill not found at {backfill}; fix_exit will stay NULL")
    if not args.no_score:
        score_all = os.environ.get("S4_SCORE_ALL", "/root/score_all.sh")
        if Path(score_all).exists():
            subprocess.run(["bash", score_all, str(out), str(task_base), str(ws_root)],
                           check=False)
        else:
            print(f"[stage4] score_all not found at {score_all}; run it manually")
    if not args.skip_verify:
        verify = Path(AG) / "scripts" / "verify_stage4.py"
        if verify.exists():
            subprocess.run([sys.executable, str(verify), "--out", str(out),
                            "--queue", str(queue_path), "--ws-root", str(ws_root)],
                           check=False)
    summary = {"run_id": run_id, "finished_at": utcnow(), "todo": len(todo),
               "failures": failures}
    (out / "batch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[stage4] done run_id={run_id} failures={len(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
