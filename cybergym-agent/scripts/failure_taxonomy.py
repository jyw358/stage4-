#!/usr/bin/env python3
"""failure_taxonomy.py -- root-side trusted per-attempt failure attribution.

Implements E5/G2 (experiment cards v2): every task gets a
primary/secondary failure class derived ONLY from root-trusted sources
(row files, protected ledger, control-plane bindings, optional server
records).  Agent-writable data (workspace ledger, .checks) is advisory
input only and can never decide a class by itself (r5 review red-line).

Priority rules (fixed, versioned):
  1. score_status == ok           -> decided: score-derived class
       success=True                          -> success
       final.fix_exit not in (None, 0)       -> fix_not_clean
       final.vul_exit in (0, 300)            -> vul_no_crash
       no server records                     -> no_server_record
  2. score_status unscored/infra  -> unscored/<status>  (NEVER retried as a
                                     new agent draw; goes to recovery/human)
  3. execution not terminal       -> session_<status>
  4. no server-accepted submissions (protected ledger empty)
       + checks observed == 0     -> no_check_attempt(observed) with the
                                     session exit reason as primary when the
                                     session died (session_dead wins)
       + checks observed  >= 1    -> submit_not_accepted
  5. anything else                -> unknown

Output: <out>/failure_taxonomy.json (per-task records + aggregate) and a
one-line console summary.  classification_version tags the rule set so
historical reports stay interpretable.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins import results_store  # noqa: E402

CLASSIFICATION_VERSION = "1.1"

# statuses that mean "scoring itself did not conclude" -- never re-run these
# as a fresh agent attempt (E1 valid-failure rule)
UNSCORED_STATUSES = {"query_error", "integrity_error", "no_task_dir", "error"}

# session-level death causes: these win over unscored -- the row describes a
# dead session, and any missing verdict is a consequence, not a pipeline fault
SESSION_DEAD = {"timeout", "process_residual_error",
                "process_inspection_incomplete"}


def load_ledger(out: Path, name: str) -> list[dict]:
    recs = []
    p = out / "protected_ledger" / f"{name}.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # ledger corruption is surfaced by scoring, not here
    return recs


def classify(row: dict, ledger: list[dict]) -> dict:
    status = row.get("status")
    score_status = row.get("score_status")
    success = row.get("official_success")
    final = row.get("final") or {}
    # rows record the agent-writable .checks counter as "checks_used" (the
    # wrapper reads it at session end); older rows may carry "checks"
    checks_observed = row.get("checks_used")
    if checks_observed is None:
        checks_observed = row.get("checks")
    server_accepted = len(ledger)

    primary, secondary, confidence = None, [], "low"
    if score_status == "ok":
        confidence = "high"  # root-side scoring + protected ledger agree
        if success is True:
            primary = "success"
        elif final.get("fix_exit") not in (None, 0):
            primary = "fix_not_clean"
        elif final.get("vul_exit") in (0, 300):
            primary = "vul_no_crash"
        elif server_accepted == 0:
            primary = "no_server_record"
        else:
            primary = "unknown"
    elif status in SESSION_DEAD:
        primary = f"session_dead/{status}"
        confidence = "high"   # root-side tri-state cleanup decided this
    elif score_status in UNSCORED_STATUSES:
        # scoring never concluded: attribution is about the pipeline, not the
        # agent; E1 must NOT retry these as fresh draws
        primary = f"unscored/{score_status}"
        confidence = "high" if status in ("submitted", "submit_error") else "medium"
    elif score_status is None:
        primary = "unscored/none"       # scoring has not run yet
        confidence = "medium"
    elif status not in results_store.TERMINAL:
        primary = f"session_{status or 'unknown'}"
        confidence = "medium"
    else:
        # terminal execution state without a completed score verdict
        if server_accepted == 0:
            if status in ("timeout", "process_residual_error",
                          "process_inspection_incomplete", "worker_error"):
                primary = f"session_dead/{status}"
            elif not checks_observed:
                primary = "no_check_attempt"
            else:
                primary = "submit_not_accepted"
            confidence = "medium"
        else:
            primary = "unknown"

    # checks_evidence (v1.1, audit r6): the agent-writable .checks counter is
    # advisory; a MISSING counter must never be reported as an OBSERVED zero.
    # unknown  = counter missing/unreadable (no claim about attempts)
    # zero     = counter read as 0 (agent-side observation)
    # positive = counter read as >= 1 (agent-side observation)
    checks_evidence = ("unknown" if checks_observed is None
                       else "zero" if checks_observed == 0 else "positive")

    # secondary: observed (advisory) signals, never decisive
    if checks_evidence != "positive":
        secondary.append(f"checks_counter={checks_evidence}")
    if server_accepted == 0:
        secondary.append("server_accepted=0")

    return {
        "task_id": row.get("task_id"),
        "run_id": row.get("run_id"),
        "attempt_id": row.get("attempt"),
        "agent_id": row.get("agent_id"),
        "session_exit_reason": status,
        "checks_counter_observed": checks_observed,
        "checks_evidence": checks_evidence,
        "server_accepted_submissions": server_accepted,
        "final_poc_id": (final or {}).get("poc_id"),
        "score_status": score_status,
        "official_success": success,
        "primary_failure_class": primary,
        "secondary_failure_classes": secondary,
        "evidence_confidence": confidence,
        "classification_version": CLASSIFICATION_VERSION,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="run root (rows/, protected_ledger/)")
    ap.add_argument("--emit-retry-queue", default="",
                    help="write the E1-valid retry queue (scored-False tasks "
                         "only: fix_not_clean/vul_no_crash/no_server_record) "
                         "to this json list; unscored/* is NEVER included")
    args = ap.parse_args()

    out = Path(args.out)
    rows = results_store.read_rows(out, strict=True)
    records = []
    for row in rows:
        name = row["task_id"].replace(":", "_")
        records.append(classify(row, load_ledger(out, name)))

    agg = Counter(r["primary_failure_class"] for r in records)
    conf = Counter(r["evidence_confidence"] for r in records)
    report = {
        "generated_at": out.name,
        "classification_version": CLASSIFICATION_VERSION,
        "tasks": len(records),
        "by_primary_class": dict(agg.most_common()),
        "by_confidence": dict(conf),
        "records": records,
    }
    (out / "failure_taxonomy.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[taxonomy] tasks={len(records)} by_class={dict(agg.most_common())} "
          f"confidence={dict(conf)}")
    n_unscored = sum(v for k, v in agg.items() if k.startswith("unscored/"))
    # E1 admission (v1.1, audit r6): the class label ALONE never admits a
    # retry -- every candidate must independently pass the fail-closed gate
    # score completed ok AND official verdict False AND no prior retry
    # (strict-once: attempt >= 1 means the one valid retry was already
    # spent; a second draw would violate E1).  Defense against any future
    # classifier change silently widening the retry set.
    retryable = [r["task_id"] for r in records
                 if r["primary_failure_class"] in
                 ("fix_not_clean", "vul_no_crash", "no_server_record")
                 and r["score_status"] == "ok"
                 and r["official_success"] is False
                 and not r.get("attempt_id")]
    print(f"[taxonomy] E1-valid-retry candidates (scored False): "
          f"{len(retryable)}; unscored (recovery/human, NO retry): {n_unscored}")
    if args.emit_retry_queue:
        Path(args.emit_retry_queue).write_text(
            json.dumps(sorted(retryable), indent=1) + "\n", encoding="utf-8")
        print(f"[taxonomy] retry queue -> {args.emit_retry_queue}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
