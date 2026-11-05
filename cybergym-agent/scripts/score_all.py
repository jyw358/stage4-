#!/usr/bin/env python3
"""Host-side scoring for a stage-4 run (final submission only).

For every row in <out>/results.json (or rows/*.json):
  - query the private /query-poc route for this task and run-scoped agent_id,
  - require the submission ledger written by check.py (protected copy when
    available; the workspace ledger otherwise),
  - pick the FINAL submission by strictly parsed server created_at only,
  - require the server-side poc_hash to match the ledger's poc_sha256,
  - persist poc_id / poc_sha256 / vul_exit / fix_exit / submitted_at / agent_id
    back into the row, plus a per-task evidence file under <out>/scored/.

Fail-closed: any task whose final submission cannot be proven becomes
official_success=None with an explicit score_status, never True.

Exit codes: 0 = every row decided or legitimately undecidable (fix_pending /
no_records); 1 = at least one integrity failure (no_ledger, ledger_mismatch,
ledger_corrupt, order_unknown, ambiguous, timestamp_invalid, hash_mismatch,
hash_unverified, agent_missing); 2 = infrastructure error (query_error,
no_task_dir).  There is deliberately NO flag to score without a ledger.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins import evidence, results_store  # noqa: E402
from plugins.cybergym_runner import CyberGymRunner, parse_submit_sh  # noqa: E402

INFRA_STATUSES = {"query_error", "no_task_dir"}
BENIGN_STATUSES = {evidence.STATUS_OK, evidence.STATUS_FIX_PENDING,
                   evidence.STATUS_NO_RECORDS}


def api_key() -> str:
    """Resolve the server key without ever returning an empty value.

    The stage-3 host file (/home/naco/work/cybergym/.env) can carry an empty
    CYBERGYM_API_KEY; an empty key makes the private route answer 404, which
    would be misread as "no record" instead of an infrastructure failure.
    """
    for key in (os.environ.get("CYBERGYM_API_KEY"),
                _key_from_file("/root/.cybergym_env"),
                _key_from_file("/home/naco/work/cybergym/.env")):
        if key and key.strip():
            return key.strip()
    raise RuntimeError("CYBERGYM_API_KEY is empty/missing "
                       "(env, /root/.cybergym_env, /home/naco/work/cybergym/.env)")


def _key_from_file(path: str) -> str | None:
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("CYBERGYM_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def query_records(runner, tid: str) -> list:
    try:
        return runner.query_poc(original_task_id=tid) or []
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="run root containing results.json")
    ap.add_argument("--task-base", default="/data/cybergym/stage4/r_engineer/tasks")
    ap.add_argument("--ws-root", default="/data/cybergym/stage4/r_engineer-ws")
    ap.add_argument("--server", default="http://172.17.0.1:8666")
    ap.add_argument("--run-id", default="", help="only score rows from this run")
    ap.add_argument("--only-unscored", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    out = Path(args.out)
    try:
        # R2 MAJOR: a corrupt row file must abort scoring loudly, not shrink
        # the scored set silently
        rows = results_store.read_rows(out, strict=True)
    except results_store.ResultsIntegrityError as e:
        print(f"[scored] ABORT: {e}", file=sys.stderr)
        (out / "scoring_report.json").write_text(
            json.dumps({"generated_at": evidence.utcnow(),
                        "run_id": args.run_id or None, "tasks": 0,
                        "aborted": str(e), "infra_errors": 1},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return 2
    if not rows:
        rows = json.load(open(out / "results.json", encoding="utf-8"))
    if args.run_id:
        # strict run attribution: rows without a run_id belong to no run and
        # must never be folded into this run's score
        rows = [r for r in rows if r.get("run_id") == args.run_id]
    rows.sort(key=lambda r: r.get("task_id", ""))
    if args.limit:
        rows = rows[:args.limit]

    try:
        key = api_key()
    except RuntimeError as e:
        print(f"[scored] ABORT: {e}", file=sys.stderr)
        (out / "scoring_report.json").write_text(
            json.dumps({"generated_at": evidence.utcnow(),
                        "run_id": args.run_id or None, "tasks": len(rows),
                        "aborted": str(e), "infra_errors": 1},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return 2
    counts = {}
    n_success = 0
    n_decided = 0
    for row in rows:
        tid = row["task_id"]
        name = tid.replace(":", "_")
        if args.only_unscored and row.get("score_status") == evidence.STATUS_OK:
            continue
        tdir = Path(args.task_base) / name
        ws = Path(row.get("workspace") or (Path(args.ws_root) / name))
        patch = {"task_id": tid, "scored_at": evidence.utcnow()}
        result = {"status": "error", "success": None, "final": None,
                  "reason": "", "ledger_entries": 0}

        if not (tdir / "submit.sh").exists():
            result = {"status": "no_task_dir", "success": None, "final": None,
                      "reason": f"missing {tdir / 'submit.sh'}", "ledger_entries": 0}
        else:
            try:
                agent_id = row.get("agent_id") or parse_submit_sh(tdir).get("agent_id", "")
                runner = CyberGymRunner(args.server, tdir, api_key=key)
                recs = query_records(runner, tid)
                hist = sorted((out / "ledger_history").glob(f"{name}.*.jsonl"))
                prot = out / "protected_ledger" / f"{name}.jsonl"
                if prot.exists() and prot.stat().st_size > 0:
                    ledger_source = "protected"
                    ledger_paths = [prot]
                else:
                    ledger_source = "workspace"
                    ledger_paths = ([ws / "submissions.jsonl"] + hist) if hist else None
                result = evidence.bind_evidence(
                    recs, agent_id, ledger_path=ws / "submissions.jsonl",
                    require_ledger=True, ledger_paths=ledger_paths,
                    run_id=row.get("run_id") or None)
                result["ledger_source"] = ledger_source
                patch["agent_id"] = agent_id
                patch["ledger_source"] = ledger_source
            except Exception as e:  # never silently swallow a scoring failure
                result = {"status": "query_error", "success": None, "final": None,
                          "reason": f"{type(e).__name__}: {e}", "ledger_entries": 0}

        patch.update({
            "official_success": result.get("success"),
            "score_status": result.get("status"),
            "score_reason": result.get("reason"),
            "final": result.get("final"),
            "submissions": result.get("n_ours"),
            "ledger_entries": result.get("ledger_entries"),
            "scored_run_id": row.get("run_id"),
        })
        if result.get("status") == evidence.STATUS_OK and result.get("success") is not None:
            n_decided += 1
            n_success += 1 if result.get("success") else 0
        counts[result.get("status")] = counts.get(result.get("status"), 0) + 1
        results_store.merge_row(out, tid, patch)
        scored_dir = out / "scored"
        scored_dir.mkdir(exist_ok=True)
        (scored_dir / f"{name}.json").write_text(
            json.dumps({"task_id": tid, **patch}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    n_infra = sum(counts.get(s, 0) for s in INFRA_STATUSES)
    n_integrity = sum(n for s, n in counts.items() if s not in INFRA_STATUSES
                      and s not in BENIGN_STATUSES)
    report = {
        "generated_at": evidence.utcnow(),
        "run_id": args.run_id or None,
        "tasks": len(rows),
        "official_success": n_success,
        "decided": n_decided,
        "by_status": counts,
        "integrity_failures": n_integrity,
        "infra_errors": n_infra,
        "ledger_required": True,
    }
    (out / "scoring_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    order = None
    try:
        order = json.load(open(out / "rows_index.json", encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    results_store.rebuild(out, order=order)
    print(f"[scored] official_success={n_success}/{len(rows)} "
          f"decided={n_decided} by_status={counts} "
          f"integrity_fail={n_integrity} infra_err={n_infra}")
    if n_infra:
        return 2
    if n_integrity:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
