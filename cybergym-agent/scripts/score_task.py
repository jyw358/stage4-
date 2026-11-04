#!/usr/bin/env python3
"""Score one task from the authoritative server records (final submission only).

Stage-4: the ledger written by check.py and the server's poc_records must
agree; success is computed from the FINAL submission, never any-of.  The row
is written through plugins.results_store (file lock + atomic replace), so
concurrent workers cannot lose each other's rows.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins import evidence, results_store  # noqa: E402
from plugins.cybergym_runner import CyberGymRunner, parse_submit_sh  # noqa: E402

tid = sys.argv[1]
out_dir = sys.argv[2]
stage = sys.argv[3] if len(sys.argv) > 3 else "pass1"
tdir = sys.argv[4] if len(sys.argv) > 4 else f"/data/cybergym/stage4/r_engineer/tasks/{tid.replace(':', '_')}"
# stage-4: no downgrade path -- the ledger is always required.  Legacy data
# without a ledger must be scored by a separate, explicitly-marked tool.
require_ledger = True

ws = os.environ.get("CG_WS_OVERRIDE") or f"/data/cybergym/stage4/r_engineer-ws/{tid.replace(':', '_')}"
checks = 0
try:
    checks = int(open(f"{ws}/.checks").read())
except Exception:
    pass

agent_id = ""
try:
    agent_id = parse_submit_sh(tdir).get("agent_id", "")
except Exception:
    pass

old_row = results_store.read_row(out_dir, tid) or {}
run_id = old_row.get("run_id")

result = {"status": "query_error", "success": None, "final": None,
          "ledger_entries": 0, "reason": "query failed"}
try:
    setenv = open("/home/naco/work/cybergym/.env").read()
    key = [l.split("=", 1)[1].strip() for l in setenv.splitlines()
           if l.startswith("CYBERGYM_API_KEY=")][0]
    runner = CyberGymRunner("http://172.17.0.1:8666", tdir, api_key=key)
    recs = runner.query_poc(original_task_id=tid)
    hist = sorted(Path(out_dir, "ledger_history").glob(
        f"{tid.replace(':', '_')}.*.jsonl"))
    prot = Path(out_dir, "protected_ledger", f"{tid.replace(':', '_')}.jsonl")
    if prot.exists() and prot.stat().st_size > 0:
        ledger_paths = [str(prot)]
    elif hist:
        ledger_paths = [os.path.join(ws, "submissions.jsonl")] + [str(p) for p in hist]
    else:
        ledger_paths = None
    result = evidence.bind_evidence(
        recs, agent_id, ledger_path=os.path.join(ws, "submissions.jsonl"),
        require_ledger=require_ledger, ledger_paths=ledger_paths,
        run_id=run_id)
except Exception as e:
    result["reason"] = f"{type(e).__name__}: {e}"

success = result.get("success")
patch = {
    "task_id": tid,
    "official_success": success,
    "score_status": result.get("status"),
    "score_reason": result.get("reason"),
    "final": result.get("final"),
    "submissions": result.get("n_ours"),
    "ledger_entries": result.get("ledger_entries"),
    "checks_used": checks,
    "agent_id": agent_id or None,
    "scored_at": evidence.utcnow(),
    "stages": {stage: success},
}
old = old_row
if old.get("stages"):
    patch["stages"] = {**old["stages"], **patch["stages"]}
patch["second_pass"] = bool(old.get("second_pass")) or stage == "pass2"
patch["retried"] = bool(old.get("retried")) or stage == "retry"
results_store.merge_row(out_dir, tid, patch)
print(f"[score:{stage}] {tid}: success={success} status={result.get('status')} "
      f"checks={checks} final={((result.get('final') or {}).get('poc_id'))}")
