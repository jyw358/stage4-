#!/usr/bin/env python3
"""verify_stage4.py -- acceptance checker for a stage-4 run.

Checks the five things the stage-4 protocol promises:

  1. coverage        every queued task has exactly one terminal row
  2. evidence        every submission has final poc_id + sha256 + exits + time
  3. scoring         every scored row is final-only and ledger-bound
  4. isolation       no reference-intel in any workspace, manifest present
  5. consistency     results.json aggregate == rows/, costs recorded

Exit code 1 on any violation unless --soft.  Writes <out>/verify_stage4.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins import results_store  # noqa: E402

SCORE_OK = "ok"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_queue(path: Path) -> list:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except json.JSONDecodeError:
        pass
    return [x.strip() for x in text.splitlines() if x.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--queue", default="")
    ap.add_argument("--ws-root", default="")
    ap.add_argument("--soft", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)
    rows = results_store.read_rows(out)
    by_id = {r.get("task_id"): r for r in rows if r.get("task_id")}
    violations = []
    warnings = []

    def bad(kind, tid, detail):
        violations.append({"kind": kind, "task_id": tid, "detail": detail})

    expected = None
    if args.queue and Path(args.queue).exists():
        expected = load_queue(Path(args.queue))
        missing = [t for t in expected if t not in by_id]
        extra = [t for t in by_id if t not in set(expected)]
        for tid in missing:
            bad("coverage_missing", tid, "queued task has no row")
        for tid in extra:
            warnings.append({"kind": "coverage_extra", "task_id": tid,
                             "detail": "row not in queue"})

    ws_root = Path(args.ws_root) if args.ws_root else None
    for tid, row in by_id.items():
        name = tid.replace(":", "_")
        status = row.get("status")
        if status == "running":
            bad("row_running", tid, "row never reached a terminal status")
        elif status is None and row.get("official_success") is None:
            bad("row_no_status", tid, "row has neither status nor a score")

        submissions = row.get("submissions") or 0
        if submissions > 0:
            final = row.get("final") or {}
            if row.get("score_status") != SCORE_OK:
                bad("score_status", tid, f"submitted but score_status={row.get('score_status')!r}")
            for field in ("poc_id", "poc_sha256", "vul_exit", "submitted_at", "agent_id"):
                if final.get(field) in (None, ""):
                    bad("evidence_missing", tid, f"final.{field} is empty")
            # V2: an ok row whose server hash and ledger hash disagree (or were
            # never cross-checked) must not pass acceptance
            if row.get("score_status") == SCORE_OK:
                if final.get("hash_check") != "match":
                    bad("hash_unverified", tid,
                        f"score_status=ok but final.hash_check={final.get('hash_check')!r}")
                elif str(final.get("poc_hash", "")).lower() != str(final.get("poc_sha256", "")).lower():
                    bad("hash_mismatch", tid, "final.poc_hash != final.poc_sha256")
            # V3: run/agent attribution must be explicit and consistent
            if not row.get("run_id"):
                bad("run_unbound", tid, "submitted row has no run_id")
            if (final.get("agent_id") and row.get("agent_id")
                    and str(final["agent_id"]) != str(row["agent_id"])):
                bad("agent_mismatch", tid,
                    f"final.agent_id={final.get('agent_id')!r} != row.agent_id={row.get('agent_id')!r}")
            if final.get("fix_exit") is None and final.get("vul_exit") not in (0, 300):
                bad("fix_pending", tid, "vul crashed but fix side was never scored (backfill missing?)")
            if row.get("official_success") not in (True, False):
                bad("score_undecided", tid, "official_success is not a bool")
        else:
            if row.get("official_success") is not None:
                bad("no_submission_scored", tid, "no submission but official_success set")
            if row.get("score_status") not in (None, "no_records", "no_ledger"):
                warnings.append({"kind": "no_submission_status", "task_id": tid,
                                 "detail": str(row.get("score_status"))})

        if ws_root:
            ws = ws_root / name
            protected = out / "protected_ledger" / f"{name}.jsonl"
            if ws.exists():
                if list(ws.glob("reference-intel*")):
                    bad("isolation_intel", tid, "reference-intel present in workspace")
                if not (ws / "ws_manifest.json").exists():
                    bad("isolation_manifest", tid, "ws_manifest.json missing")
                ledger = ws / "submissions.jsonl"
                prot_ok = protected.exists() and protected.stat().st_size > 0
                if submissions > 0 and not prot_ok and (not ledger.exists() or ledger.stat().st_size == 0):
                    bad("ledger_missing", tid, f"row says {submissions} submissions but neither the protected nor the workspace ledger has data")
            elif submissions > 0:
                bad("workspace_missing", tid, "workspace gone but row claims submissions")

    # aggregate consistency
    results_path = out / "results.json"
    aggregate = []
    if results_path.exists():
        try:
            aggregate = json.load(open(results_path, encoding="utf-8"))
        except json.JSONDecodeError:
            bad("results_json", None, "results.json is not valid JSON")
    if len(aggregate) != len(rows):
        bad("aggregate_count", None,
            f"results.json has {len(aggregate)} rows, rows/ has {len(rows)}")
    agg_ok = sum(1 for r in aggregate if r.get("official_success") is True)
    row_ok = sum(1 for r in rows if r.get("official_success") is True)
    if agg_ok != row_ok:
        bad("aggregate_score", None,
            f"results.json official={agg_ok}, rows/ official={row_ok}")
    agg_by_id = {r.get("task_id"): r for r in aggregate}
    for tid, row in by_id.items():
        if tid in agg_by_id and agg_by_id[tid].get("official_success") != row.get("official_success"):
            bad("aggregate_row_mismatch", tid, "results.json disagrees with rows/")

    no_cost = [t for t, r in by_id.items() if r.get("cost_usd") is None]
    total_cost = sum((r.get("cost_usd") or 0) for r in rows)
    submitted = sum(1 for r in rows if (r.get("submissions") or 0) > 0)
    scored_ok = sum(1 for r in rows if r.get("official_success") is True)

    report = {
        "generated_at": utcnow(),
        "run_id": (rows[0].get("run_id") if rows else None),
        "expected": len(expected) if expected else None,
        "rows": len(rows),
        "submitted": submitted,
        "official_success": scored_ok,
        "score_rate": round(scored_ok / len(rows), 4) if rows else None,
        "total_cost_usd": round(total_cost, 4),
        "rows_without_cost": len(no_cost),
        "violations": violations,
        "warnings": warnings,
    }
    (out / "verify_stage4.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[verify] rows={len(rows)} expected={report['expected']} "
          f"submitted={submitted} official={scored_ok} cost=${total_cost:.2f}")
    print(f"[verify] violations={len(violations)} warnings={len(warnings)}")
    for v in violations[:20]:
        print(f"  VIOLATION {v['kind']} {v['task_id']}: {v['detail']}")
    if violations and not args.soft:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
