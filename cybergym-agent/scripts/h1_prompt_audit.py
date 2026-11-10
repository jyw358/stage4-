#!/usr/bin/env python3
"""h1_prompt_audit.py -- runtime unique-variable audit for H1 (r23).

Post-run verification that the pass2 prompts ACTUALLY DELIVERED to the
model match the pre-registered arm configuration:
  - every emitted .p2prompt must hash to the manifest sha for its
    triage category under that arm's strategy (A1/A2/B/C/GENERIC shared;
    D = arm-specific),
  - triage failures that fell back to the legacy wrapper prompt are
    COUNTED and recorded (H1_WARN in <NAME>.triage.stdout) -- they are
    never counted as successful arm application and are listed per task
    for exclusion from the D-strategy analysis,
  - any prompt whose sha is in NO allowed set, or a missing prompt
    without a recorded fallback, is an integrity violation (rc 1).

Inputs: --manifest H1_MANIFEST.json (sha_map_legacy / sha_map_v4)
        --logroot /root/agent_logs (default)
        --arm NAME:STRATEGY:RUNID   (repeatable)
Output: --out audit.json  (also printed summary line)
"""
import argparse
import glob
import hashlib
import json
import os
import sys

STRATEGY_KEY = {"legacy": "sha_map_legacy", "construct": "sha_map_v4"}


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def audit_arm(logroot, runid, strategy, allowed):
    res = {"run_id": runid, "strategy": strategy,
           "verified": [], "fallback": [], "violations": []}
    d = os.path.join(logroot, runid)
    if not os.path.isdir(d):
        res["violations"].append({"kind": "logdir_missing", "detail": d})
        return res
    for tj in sorted(glob.glob(os.path.join(d, "*.triage.json"))):
        name = os.path.basename(tj)[:-len(".triage.json")]
        try:
            rec = json.load(open(tj, encoding="utf-8"))
            cat = rec.get("category")
        except Exception as e:
            res["violations"].append(
                {"task": name, "kind": "triage_json_unreadable",
                 "detail": str(e)})
            continue
        if cat not in allowed:
            res["violations"].append(
                {"task": name, "kind": "unknown_category",
                 "detail": str(cat)})
            continue
        pf = os.path.join(d, f"{name}.p2prompt")
        stdout_f = os.path.join(d, f"{name}.triage.stdout")
        warn = ""
        if os.path.isfile(stdout_f):
            try:
                warn = open(stdout_f, encoding="utf-8",
                            errors="replace").read()
            except Exception:
                warn = ""
        fell_back = "H1_WARN" in warn
        if os.path.isfile(pf) and os.path.getsize(pf) > 0:
            sha = sha256_of(pf)
            if sha == allowed[cat]:
                res["verified"].append(
                    {"task": name, "category": cat,
                     "fallback_recorded": fell_back})
            else:
                res["violations"].append(
                    {"task": name, "category": cat,
                     "kind": "prompt_sha_mismatch",
                     "detail": f"got {sha[:16]} want "
                               f"{allowed[cat][:16]}"})
        elif fell_back:
            # triage failed -> wrapper used the inline legacy prompt;
            # recorded, NOT counted as arm application (r23 section 4)
            res["fallback"].append({"task": name, "category": cat})
        else:
            res["violations"].append(
                {"task": name, "category": cat,
                 "kind": "prompt_missing_without_fallback"})
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
        print(f"[prompt_audit] arm={name} verified={len(r['verified'])} "
              f"fallback={len(r['fallback'])} "
              f"violations={len(r['violations'])}")
    print(f"[prompt_audit] violations={n_viol}")
    return 1 if n_viol else 0


if __name__ == "__main__":
    sys.exit(main())
