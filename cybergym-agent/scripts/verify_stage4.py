#!/usr/bin/env python3
"""verify_stage4.py -- acceptance checker for a stage-4 run.

Two DISTINCT acceptance dimensions (R2/B10):

  1. data integrity   every submission has final poc_id + sha256 + exits +
                      time; scores are final-only and hash-gated; the
                      aggregate agrees with rows/ field by field; the
                      authoritative (root-written, append-only) ledger exists
                      and parses; scored/ cross-checks match the row.
  2. execution coverage  every queued task ended in an ALLOWED terminal state.
                      gen_fail / worker_error / api_error are infrastructure
                      failures: they are explicit coverage violations, so a
                      "self-consistent" round with zero effective executions
                      can never pass with 0 violations.

Exit code 1 on any violation unless --soft.  Writes <out>/verify_stage4.json.

R2 changes (B10 + MAJORs):
  - coverage violations for infra-failure statuses (gen_fail et al.);
  - agent_id must be present on BOTH sides and strictly equal;
  - the protected ledger is REQUIRED for any row with submissions (a
    workspace-ledger-only score is a violation, not a fallback);
  - isolation audit is recursive and covers renamed intel spellings
    (reference_intel*, ref-intel*, ...);
  - aggregate consistency compares status/submissions/cost/run_id per row,
    not just the success count;
  - scored/<task>.json is cross-checked against the row;
  - a missing cost_usd is a violation (not a counter), and the total never
    silently treats missing costs as 0.

R3 changes (dedicated-user review):
  - control-plane binding: when <out>/control exists (i.e. the run was
    started by the R3 driver), every submitted row must have a root-owned
    <out>/control/<task>/ whose run_id and agent_id match the row -- the
    binding the driver established is the acceptance reference;
  - ledger_conflict: a <protected>.conflicts.jsonl with content means the
    root promoter saw the same poc_id with a DIFFERENT hash -- that is
    tamper evidence, not a warning.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins import results_store  # noqa: E402

SCORE_OK = "ok"

# execution outcomes that are data-valid end states for a formal pilot
ALLOWED_FINAL = {"submitted", "no_submission", "submit_error"}
# infrastructure failures: a formal pilot must have ZERO of these (B10)
INFRA_FAILURE = {"gen_fail", "worker_error", "api_error", "evidence_error"}

INTEL_PATTERNS = ("reference-intel*", "reference_intel*", "ref-intel*",
                  "ref_intel*")

AGG_FIELDS = ("status", "official_success", "submissions", "cost_usd",
              "score_status", "run_id")


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


def find_intel(ws: Path) -> list:
    hits = []
    for pat in INTEL_PATTERNS:
        hits.extend(p for p in ws.rglob(pat) if p not in hits)
    return sorted(hits)


def protected_ledger_state(out: Path, name: str):
    """(exists_and_nonempty, parses, detail) for the authoritative ledger."""
    prot = out / "protected_ledger" / f"{name}.jsonl"
    if not prot.exists() or prot.stat().st_size == 0:
        return False, False, str(prot)
    try:
        for line in prot.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                json.loads(line)
    except json.JSONDecodeError:
        return True, False, str(prot)
    return True, True, str(prot)


def control_binding_state(out: Path, name: str, row: dict):
    """R3: (violation_kind, detail) for the per-task control-plane binding.
    Only enforced when <out>/control exists (R3 driver layout)."""
    croot = out / "control"
    if not croot.is_dir():
        return None, "control plane absent (pre-R3 run layout)"
    cdir = croot / name
    if not cdir.is_dir():
        return "control_missing", f"{cdir} does not exist (binding not established)"
    try:
        if os.geteuid() == 0:  # best-effort trust check of the dir itself
            st = os.lstat(cdir)
            if stat.S_ISLNK(st.st_mode) or st.st_uid != 0:
                return "control_mismatch", f"{cdir} is a symlink or not root-owned"
    except (AttributeError, OSError):
        pass
    cvals = {}
    for key in ("run_id", "agent_id"):
        p = cdir / key
        if p.exists():
            try:
                cvals[key] = p.read_text(encoding="utf-8").strip()
            except OSError:
                return "control_mismatch", f"{p} unreadable"
    if not cvals:
        return "control_missing", f"{cdir} has no binding fields"
    if cvals.get("run_id") and row.get("run_id") and cvals["run_id"] != row["run_id"]:
        return "control_mismatch", (f"control run_id={cvals['run_id']!r} != "
                                    f"row run_id={row.get('run_id')!r}")
    if cvals.get("agent_id") and row.get("agent_id") \
            and cvals["agent_id"] != row["agent_id"]:
        return "control_mismatch", (f"control agent_id={cvals['agent_id']!r} != "
                                    f"row agent_id={row.get('agent_id')!r}")
    return None, ""


def control_field(out: Path, name: str, key: str) -> str:
    p = Path(out) / "control" / name / key
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def task_dir_privacy_state(out: Path, name: str, tasks_root=None):
    """R3-B1 acceptance: the prepared task dir must be 0700 and owned by the
    uid the control plane bound, otherwise uid_A can read uid_B's inputs."""
    if os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() != 0):
        return None, ""
    tdir = Path(tasks_root or (Path(out) / "tasks")) / name
    if not tdir.exists():
        return None, ""
    want = control_field(out, name, "agent_uid")
    if not want.isdigit():
        return "taskdir_unbound", f"{tdir}: control plane has no agent_uid"
    try:
        st = os.lstat(tdir)
    except OSError:
        return None, ""
    if stat.S_ISLNK(st.st_mode):
        return "taskdir_open", f"{tdir} is a symlink"
    if st.st_uid != int(want):
        return "taskdir_open", (f"{tdir} owned by uid={st.st_uid}, but the "
                                f"binding issued uid={want}")
    if st.st_mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        return "taskdir_open", (f"{tdir} mode={oct(st.st_mode)[-3:]} grants "
                                "group/other access (cross-task read window)")
    return None, ""


def ledger_conflicts(out: Path, name: str) -> bool:
    """R3: True when the promoter recorded a same-poc_id/different-hash
    conflict for this task."""
    cpath = out / "protected_ledger" / f"{name}.jsonl.conflicts.jsonl"
    try:
        return cpath.exists() and cpath.stat().st_size > 0
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--queue", default="")
    ap.add_argument("--ws-root", default="")
    ap.add_argument("--soft", action="store_true",
                    help="write the report but always exit 0")
    args = ap.parse_args()

    out = Path(args.out)
    try:
        rows = results_store.read_rows(out, strict=True)
    except results_store.ResultsIntegrityError as e:
        rows = results_store.read_rows(out)
        print(f"[verify] VIOLATION results_corrupt: {e}")
        (out / "verify_stage4.json").write_text(
            json.dumps({"generated_at": utcnow(), "violations":
                        [{"kind": "results_corrupt", "task_id": None,
                          "detail": str(e)}]}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return 1 if not args.soft else 0

    by_id = {r.get("task_id"): r for r in rows if r.get("task_id")}
    violations = []
    warnings = []
    if not (out / "control").is_dir():
        warnings.append({
            "kind": "control_plane_absent", "task_id": None,
            "detail": "<out>/control does not exist: this run was not started "
                      "by the R3 driver; per-task identity bindings were not "
                      "established and cannot be audited here"})

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

    # run attribution: all expected rows must belong to ONE run (B10)
    run_ids = {r.get("run_id") for r in by_id.values() if r.get("run_id")}
    if len(run_ids) > 1:
        bad("run_mixed", None, f"rows mix run_ids: {sorted(run_ids)}")

    ws_root = Path(args.ws_root) if args.ws_root else None
    for tid, row in by_id.items():
        name = tid.replace(":", "_")
        status = row.get("status")
        if status == "running":
            bad("row_running", tid, "row never reached a terminal status")
        elif status is None and row.get("official_success") is None:
            bad("row_no_status", tid, "row has neither status nor a score")

        # ---- B10: execution coverage --------------------------------------
        if status in INFRA_FAILURE:
            bad("coverage_infra", tid,
                f"status={status} is an infrastructure failure; a formal "
                "pilot must retry it, not average over it")

        submissions = row.get("submissions") or 0
        if status == "submitted" and submissions == 0:
            bad("status_inconsistent", tid,
                "status=submitted but submissions=0")
        if submissions > 0:
            final = row.get("final") or {}
            if row.get("score_status") != SCORE_OK:
                bad("score_status", tid,
                    f"submitted but score_status={row.get('score_status')!r}")
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
            # V3: run/agent attribution must be explicit and consistent.
            # R2 MAJOR: both sides must be present -- a missing row.agent_id
            # is a violation, not a skip.
            if not row.get("run_id"):
                bad("run_unbound", tid, "submitted row has no run_id")
            if not row.get("agent_id"):
                bad("agent_missing_row", tid, "row.agent_id is empty")
            elif not final.get("agent_id"):
                bad("agent_missing_final", tid, "final.agent_id is empty")
            elif str(final["agent_id"]) != str(row["agent_id"]):
                bad("agent_mismatch", tid,
                    f"final.agent_id={final.get('agent_id')!r} != "
                    f"row.agent_id={row.get('agent_id')!r}")
            if final.get("fix_exit") is None and final.get("vul_exit") not in (0, 300):
                bad("fix_pending", tid, "vul crashed but fix side was never scored (backfill missing?)")
            if row.get("official_success") not in (True, False):
                bad("score_undecided", tid, "official_success is not a bool")

            # ---- R2/B11: the authoritative ledger is REQUIRED -----------------
            prot_ok, prot_parses, prot_path = protected_ledger_state(out, name)
            if not prot_ok:
                bad("ledger_missing", tid,
                    f"protected ledger missing/empty ({prot_path}); the "
                    "agent-writable workspace ledger is NOT authoritative")
            elif not prot_parses:
                bad("ledger_corrupt", tid,
                    f"protected ledger has unparseable lines ({prot_path})")
            if ledger_conflicts(out, name):
                bad("ledger_conflict", tid,
                    "promoter recorded a same-poc_id/different-hash conflict "
                    "(tamper evidence; see protected_ledger/*.conflicts.jsonl)")
            if row.get("ledger_source") and row["ledger_source"] != "protected":
                bad("ledger_not_protected", tid,
                    f"scored from ledger_source={row['ledger_source']!r}; "
                    "only the root-written protected ledger is authoritative")

            # ---- R3: the control-plane binding is the acceptance reference ---
            ckind, cdetail = control_binding_state(out, name, row)
            if ckind:
                bad(ckind, tid, cdetail)
            elif (Path(out) / "control").is_dir():
                dkind, ddetail = task_dir_privacy_state(out, name)
                if dkind:
                    bad(dkind, tid, ddetail)
        else:
            if row.get("official_success") is not None:
                bad("no_submission_scored", tid, "no submission but official_success set")
            if row.get("score_status") not in (None, "no_records", "no_ledger"):
                warnings.append({"kind": "no_submission_status", "task_id": tid,
                                 "detail": str(row.get("score_status"))})

        # cost accounting: a missing cost is a violation (R2 MINOR), the
        # total never pretends missing == 0
        if row.get("cost_usd") is None:
            bad("cost_missing", tid, "row has no cost_usd")

        # scored/ cross-check (B10 blind spot)
        scored = out / "scored" / f"{name}.json"
        if scored.exists():
            try:
                sj = json.load(open(scored, encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                bad("scored_corrupt", tid, f"{scored} is unreadable")
            else:
                for field in ("official_success", "score_status"):
                    if sj.get(field) != row.get(field):
                        bad("scored_mismatch", tid,
                            f"{field}: scored/={sj.get(field)!r} row={row.get(field)!r}")
                spoc = (sj.get("final") or {}).get("poc_id")
                rpoc = (row.get("final") or {}).get("poc_id")
                if spoc != rpoc:
                    bad("scored_mismatch", tid,
                        f"final.poc_id: scored/={spoc!r} row={rpoc!r}")

        if ws_root:
            ws = ws_root / name
            if ws.exists():
                intel = find_intel(ws)
                if intel:
                    bad("isolation_intel", tid,
                        "intel present in workspace: "
                        + ", ".join(str(p.relative_to(ws)) for p in intel[:5]))
                if not (ws / "ws_manifest.json").exists():
                    bad("isolation_manifest", tid, "ws_manifest.json missing")
            elif submissions > 0:
                bad("workspace_missing", tid, "workspace gone but row claims submissions")

    # aggregate consistency: row COUNT, success COUNT and per-row FIELDS
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
        ar = agg_by_id.get(tid)
        if ar is None:
            continue
        diffs = [f for f in AGG_FIELDS if ar.get(f) != row.get(f)]
        if diffs:
            bad("aggregate_row_mismatch", tid,
                f"results.json differs from rows/ in {diffs}")

    known_cost = [r.get("cost_usd") for r in rows if r.get("cost_usd") is not None]
    total_cost = sum(known_cost)
    rows_without_cost = sum(1 for r in rows if r.get("cost_usd") is None)
    submitted = sum(1 for r in rows if (r.get("submissions") or 0) > 0)
    scored_ok = sum(1 for r in rows if r.get("official_success") is True)
    infra_failed = sum(1 for r in rows if r.get("status") in INFRA_FAILURE)
    allowed_final = sum(1 for r in rows if r.get("status") in ALLOWED_FINAL)

    report = {
        "generated_at": utcnow(),
        "run_id": (rows[0].get("run_id") if rows else None),
        "expected": len(expected) if expected else None,
        "rows": len(rows),
        "coverage": {
            "allowed_final": allowed_final,       # data-valid end states
            "submitted": submitted,
            "scored_ok": scored_ok,
            "infra_failed": infra_failed,         # gen_fail/worker_error/api_error
        },
        "submitted": submitted,
        "official_success": scored_ok,
        "score_rate": round(scored_ok / len(rows), 4) if rows else None,
        "total_cost_usd": round(total_cost, 4),
        "rows_with_cost": len(known_cost),
        "rows_without_cost": rows_without_cost,
        "violations": violations,
        "warnings": warnings,
    }
    (out / "verify_stage4.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[verify] rows={len(rows)} expected={report['expected']} "
          f"submitted={submitted} official={scored_ok} "
          f"infra_failed={infra_failed} cost=${total_cost:.2f}"
          + (f" (+{rows_without_cost} rows without cost)" if rows_without_cost else ""))
    print(f"[verify] violations={len(violations)} warnings={len(warnings)}")
    for v in violations[:20]:
        print(f"  VIOLATION {v['kind']} {v['task_id']}: {v['detail']}")
    if violations and not args.soft:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
