#!/usr/bin/env python3
"""Record one task's stage-4 row after a session (or a worker failure).

Reads the agcli JSON, the submission ledger and .checks, merges them into the
task's atomic row (rows/<task>.json + results.json), and prints a one-line
summary.  All accounting fields are merged here -- pass-2 is ADDED to pass-1
instead of overwriting it (the stage-3 bug where turns/checks went stale).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins import results_store  # noqa: E402


def _load(path):
    if not path:
        return {}
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _usage(j):
    u = j.get("usage") or {}
    tokens = {
        "input_tokens": u.get("input_tokens") or 0,
        "output_tokens": u.get("output_tokens") or 0,
        "cache_creation_input_tokens": u.get("cache_creation_input_tokens") or 0,
        "cache_read_input_tokens": u.get("cache_read_input_tokens") or 0,
    }
    # modelUsage is a per-model dict; keep a compact total for attribution
    mu = j.get("modelUsage") or {}
    per_model = {}
    for model, stat in mu.items():
        if isinstance(stat, dict):
            per_model[model] = {
                "input_tokens": stat.get("inputTokens") or 0,
                "output_tokens": stat.get("outputTokens") or 0,
                "cache_read_input_tokens": stat.get("cacheReadInputTokens") or 0,
                "cost_usd": stat.get("costUSD") or 0,
            }
    tokens["total"] = tokens["input_tokens"] + tokens["output_tokens"]
    return tokens, per_model


def _ledger(ws: Path):
    path = Path(os.environ.get("CG_SUBMISSION_LEDGER") or (ws / "submissions.jsonl"))
    entries = []
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except FileNotFoundError:
        pass
    return path, entries


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--run-id", default=os.environ.get("S4_RUN_ID") or "manual")
    ap.add_argument("--ws", required=True)
    ap.add_argument("--tdir", default="")
    ap.add_argument("--stage", choices=["pass1", "pass2", "error"], default="pass1")
    ap.add_argument("--json", default="")
    ap.add_argument("--agent-id", default="")
    ap.add_argument("--status", default="")
    ap.add_argument("--error", default="")
    ap.add_argument("--second-pass", action="store_true")
    args = ap.parse_args()

    ws = Path(args.ws)
    row = results_store.read_row(args.out, args.task_id) or {"task_id": args.task_id}
    if row.get("run_id") not in (None, args.run_id):
        # a new run must never inherit a previous run's score/accounting
        row = {"task_id": args.task_id}
    row.setdefault("run_id", args.run_id)
    row.setdefault("status", "running")
    row["run_id"] = args.run_id
    row["workspace"] = str(ws)
    if args.tdir:
        row["task_dir"] = args.tdir
    if args.agent_id:
        row["agent_id"] = args.agent_id
    if args.error:
        row["error"] = args.error

    j = _load(args.json)
    cost = j.get("total_cost_usd") or 0
    turns = int(j.get("num_turns") or 0)
    tokens, per_model = _usage(j)

    if args.stage == "pass1":
        row["result"] = (j.get("result") or "")[-400:]
        row["cost_usd"] = cost
        row["turns"] = turns
        row["model_calls"] = turns
        row["tokens"] = tokens
        row["model_usage"] = per_model
        row["finished_at"] = j.get("created_at") or row.get("finished_at")
        row["is_error"] = bool(j.get("is_error"))
    elif args.stage == "pass2":
        row["second_pass"] = True
        row["pass2_result"] = (j.get("result") or "")[-300:]
        row["cost_usd"] = (row.get("cost_usd") or 0) + cost
        row["turns"] = (row.get("turns") or 0) + turns
        row["model_calls"] = (row.get("model_calls") or 0) + turns
        prev = row.get("tokens") or {}
        merged = {k: (prev.get(k) or 0) + (tokens.get(k) or 0)
                  for k in set(prev) | set(tokens)}
        row["tokens"] = merged
        prev_mu = row.get("model_usage") or {}
        for model, stat in per_model.items():
            cur = prev_mu.setdefault(model, {})
            for k, v in stat.items():
                cur[k] = (cur.get(k) or 0) + (v or 0)
        row["model_usage"] = prev_mu
        row["result"] = (j.get("result") or row.get("result") or "")[-400:]

    checks = 0
    try:
        checks = int((ws / ".checks").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    row["checks_used"] = checks

    ledger_path, entries = _ledger(ws)
    row["submissions"] = len(entries)
    if entries:
        last = entries[-1]
        row["last_submission"] = {
            "poc_id": last.get("poc_id"),
            "poc_sha256": last.get("poc_sha256"),
            "poc_size": last.get("poc_size"),
            "exit_code": last.get("exit_code"),
            "ts": last.get("ts"),
            "seq": last.get("seq"),
        }
        row["ledger_path"] = str(ledger_path)
    row["needs_second_pass"] = bool(checks == 0 and not entries and not args.second_pass)

    if args.stage == "error":
        row["status"] = "worker_error"
    elif args.status:
        row["status"] = args.status
    elif entries:
        row["status"] = "submitted"
    elif checks > 0:
        row["status"] = "submit_error"
    else:
        row["status"] = "no_submission"

    results_store.merge_row(args.out, args.task_id, row)
    print(f"[row] {args.task_id} stage={args.stage} status={row['status']} "
          f"subs={row['submissions']} checks={checks} turns={row.get('turns')} "
          f"cost=${(row.get('cost_usd') or 0):.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
