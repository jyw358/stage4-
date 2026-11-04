#!/usr/bin/env python3
"""Host-side scoring for a stage-4 run (final submission only).

For every row in <out>/results.json (or rows/*.json):
  - query the private /query-poc route for this task and run-scoped agent_id,
  - require the submission ledger written by check.py,
  - pick the FINAL submission and apply the official predicate to it only,
  - persist poc_id / poc_sha256 / vul_exit / fix_exit / submitted_at / agent_id
    back into the row, plus a per-task evidence file under <out>/scored/.

Fail-closed: any task whose final submission cannot be proven becomes
official_success=None with an explicit score_status, never True.
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


def api_key() -> str:
    key = os.environ.get("CYBERGYM_API_KEY")
    if key:
        return key
    text = open("/home/naco/work/cybergym/.env", encoding="utf-8").read()
    return [l.split("=", 1)[1].strip() for l in text.splitlines()
            if l.startswith("CYBERGYM_API_KEY=")][0]


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
    ap.add_argument("--allow-missing-ledger", action="store_true",
                    help="legacy mode: score without a ledger (marked legacy)")
    ap.add_argument("--only-unscored", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    out = Path(args.out)
    rows = results_store.read_rows(out)
    if not rows:
        rows = json.load(open(out / "results.json", encoding="utf-8"))
    if args.run_id:
        rows = [r for r in rows if r.get("run_id") in (None, args.run_id)]
    rows.sort(key=lambda r: r.get("task_id", ""))
    if args.limit:
        rows = rows[:args.limit]

    key = api_key()
    want_ledger = not args.allow_missing_ledger
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
                result = evidence.bind_evidence(
                    recs, agent_id, ledger_path=ws / "submissions.jsonl",
                    require_ledger=want_ledger,
                    ledger_paths=([ws / "submissions.jsonl"] + hist) if hist else None)
                patch["agent_id"] = agent_id
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

    report = {
        "generated_at": evidence.utcnow(),
        "run_id": args.run_id or None,
        "tasks": len(rows),
        "official_success": n_success,
        "decided": n_decided,
        "by_status": counts,
        "ledger_required": want_ledger,
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
          f"decided={n_decided} by_status={counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
