#!/usr/bin/env python3
"""h1_prompt_audit.py -- runtime unique-variable audit for H1 (r23/r24).

Post-run verification that the pass2 prompts ACTUALLY DELIVERED match
the pre-registered arm configuration, with BIDIRECTIONAL coverage:

  expected set E = tasks with <NAME>.pass2.json (the authoritative
  pass2 session-end record written by the production pipeline -- a
  session that ran, even on the fallback prompt, leaves this file).
  For every task in E, exactly one of:
    (a) verified:   triage.json exists, category known, and the
        delivered .p2prompt satisfies the category hash spec;
    (b) fallback:   H1_WARN recorded in <NAME>.triage.stdout (wrapper
        fell back to the inline legacy prompt) -- counted, listed, and
        NEVER counted as arm application;
    (c) violation:  anything else (session without provable prompt
        provenance) -> rc 1. Zero triage files with nonzero sessions
        therefore CANNOT pass.

Hash spec (r24 P1-C, hashed object = FULL rendered prompt text incl.
task-injected candidate/generator names and the budget digit):
  static categories  D, GENERIC  (no task data injected):
      sha(.p2prompt) must equal the arm manifest sha_map entry.
  dynamic categories A1, A2, B, C (task-specific names embedded):
      sha(.p2prompt) must equal prompt_sha256 recorded in the
      root-owned authoritative <NAME>.triage.json (written by
      pass2_triage.py at prompt-render time); a missing/absent
      authoritative hash is a violation.

Reverse direction: triage.json without a pass2 session is reported as
a warning (triage ran, session never started -- no delivery happened).

Inputs: --manifest H1_MANIFEST.json (sha_map_legacy / sha_map_v4)
        --logroot /root/agent_logs (default)
        --arm NAME:STRATEGY:RUNID   (repeatable)
Output: --out audit.json
"""
import argparse
import glob
import hashlib
import json
import os
import sys

STRATEGY_KEY = {"legacy": "sha_map_legacy", "construct": "sha_map_v4"}
STATIC_CATEGORIES = ("D", "GENERIC")
DYNAMIC_CATEGORIES = ("A1", "A2", "B", "C")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""


def audit_arm(logroot, runid, strategy, allowed):
    res = {"run_id": runid, "strategy": strategy,
           "verified": [], "fallback": [], "violations": [], "warnings": []}
    d = os.path.join(logroot, runid)
    if not os.path.isdir(d):
        res["violations"].append({"kind": "logdir_missing", "detail": d})
        return res
    sessions = {os.path.basename(p)[:-len(".pass2.json")]
                for p in glob.glob(os.path.join(d, "*.pass2.json"))}
    triaged = {os.path.basename(p)[:-len(".triage.json")]
               for p in glob.glob(os.path.join(d, "*.triage.json"))}
    # forward: every session must have provable prompt provenance
    for name in sorted(sessions):
        stdout = read_text(os.path.join(d, f"{name}.triage.stdout"))
        fell_back = "H1_WARN" in stdout
        tj = os.path.join(d, f"{name}.triage.json")
        pf = os.path.join(d, f"{name}.p2prompt")
        if fell_back:
            cat = None
            if os.path.isfile(tj):
                try:
                    cat = json.load(open(tj, encoding="utf-8")).get("category")
                except Exception:
                    cat = None
            res["fallback"].append({"task": name, "category": cat})
            continue
        if not os.path.isfile(tj):
            res["violations"].append(
                {"task": name, "kind": "pass2_without_triage_record"})
            continue
        try:
            rec = json.load(open(tj, encoding="utf-8"))
            cat = rec.get("category")
        except Exception as e:
            res["violations"].append(
                {"task": name, "kind": "triage_json_unreadable",
                 "detail": str(e)})
            continue
        if not (os.path.isfile(pf) and os.path.getsize(pf) > 0):
            res["violations"].append(
                {"task": name, "category": cat,
                 "kind": "prompt_missing_without_fallback"})
            continue
        sha = sha256_of(pf)
        if cat in STATIC_CATEGORIES:
            if cat not in allowed:
                res["violations"].append(
                    {"task": name, "category": cat,
                     "kind": "unknown_category"})
            elif sha != allowed[cat]:
                res["violations"].append(
                    {"task": name, "category": cat,
                     "kind": "prompt_sha_mismatch",
                     "detail": f"got {sha[:16]} want "
                               f"{allowed[cat][:16]} (static)"})
            else:
                res["verified"].append({"task": name, "category": cat})
        elif cat in DYNAMIC_CATEGORIES:
            rec_sha = rec.get("prompt_sha256")
            if not rec_sha:
                res["violations"].append(
                    {"task": name, "category": cat,
                     "kind": "authoritative_prompt_sha_missing"})
            elif sha != rec_sha:
                res["violations"].append(
                    {"task": name, "category": cat,
                     "kind": "prompt_sha_mismatch",
                     "detail": f"got {sha[:16]} want {rec_sha[:16]} "
                               f"(authoritative record)"})
            else:
                res["verified"].append({"task": name, "category": cat})
        else:
            res["violations"].append(
                {"task": name, "category": cat, "kind": "unknown_category"})
    # reverse: triage ran but no session started (no delivery) -> warning
    for name in sorted(triaged - sessions):
        res["warnings"].append(
            {"task": name, "kind": "triage_without_pass2_session"})
    res["sessions"] = len(sessions)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--logroot", default="/root/agent_logs")
    ap.add_argument("--arm", action="append", required=True,
                    help="NAME:STRATEGY:RUNID")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    mf = json.load(open(a.manifest, encoding="utf-8"))
    out = {"manifest": a.manifest, "arms": {}}
    n_viol = 0
    for spec in a.arm:
        name, strategy, runid = spec.split(":", 2)
        if strategy not in STRATEGY_KEY:
            print(f"AUDIT_REFUSE: unknown strategy {strategy}")
            return 1
        allowed = mf[STRATEGY_KEY[strategy]]
        out["arms"][name] = audit_arm(a.logroot, runid, strategy, allowed)
        n_viol += len(out["arms"][name]["violations"])
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    for name, r in out["arms"].items():
        print(f"[prompt_audit] arm={name} sessions={r.get('sessions')} "
              f"verified={len(r['verified'])} fallback={len(r['fallback'])} "
              f"violations={len(r['violations'])} "
              f"warnings={len(r['warnings'])}")
    print(f"[prompt_audit] violations={n_viol}")
    return 1 if n_viol else 0


if __name__ == "__main__":
    sys.exit(main())
