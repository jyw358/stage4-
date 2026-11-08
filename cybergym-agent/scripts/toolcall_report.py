#!/usr/bin/env python3
"""toolcall_report.py -- paired-baseline tool-call conversion telemetry (P0-3 follow-up).

Measures, per agent workspace, what the session ACTUALLY did -- as opposed to
what it mentioned:
  * real tool invocations  : Bash tool_use blocks whose command names a shipped
                             tool (try_local.sh, probe_path.sh, ...)
  * strict check.py runs   : `python3 check.py ...` invocations (the only path
                             that consumes the official 25-check budget and
                             counts as a submission attempt)
  * SOLUTION.md            : Write events / on-disk presence
  * per-session split      : pass1 vs pass2 (pass2 = forced-submit salvage
                             session; latest transcript by mtime)
  * oracle verdicts        : .oracle.log verdict/rc distribution -- an all-
                             `CLEAN`-rc=1 distribution is the signature of
                             docker permission failures misclassified as
                             "no trigger" (root cause of the 2026-11-08
                             paired-baseline regression)

Read-only. Point it at a workspaces root:
    python3 toolcall_report.py /data2/arms/pair/after-ws2 [--json]
"""
from __future__ import annotations
import argparse, glob, json, os, re, sys

TOOLS = ("repo_map.py", "reach_graph.py", "probe_path.sh", "try_local.sh",
         "stab_run.sh", "minsep.py", "directed_fuzz.py", "boundary_synth.py",
         "mutate_seed.py", "hf_mutator.py")
STRICT_CHECK_RE = re.compile(r"python3?\s+(?:-u\s+)?check\.py")
DOCKER_FAIL_RE = re.compile(
    r"permission denied while trying to connect to the Docker API"
    r"|Cannot connect to the Docker daemon"
    r"|Is the docker daemon running\?", re.I)


def sessions(ws: str) -> list[str]:
    files = []
    for f in glob.glob(os.path.join(ws, ".claude-config", "projects", "*", "*.jsonl")):
        files.append((os.path.getmtime(f), f))
    return [f for _, f in sorted(files)]


def scan_session(path: str) -> dict:
    st = {"asst_msgs": 0, "tool_calls": {t: 0 for t in TOOLS},
          "strict_checks": 0, "solution_writes": 0, "docker_fail_evidence": 0}
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        st["asst_msgs"] += 1
        for c in (d.get("message", {}).get("content") or []):
            if c.get("type") != "tool_use":
                continue
            name, inp = c.get("name", ""), c.get("input") or {}
            if name == "Write" and "SOLUTION.md" in str(inp.get("file_path", "")):
                st["solution_writes"] += 1
            if name != "Bash":
                continue
            cmd = str(inp.get("command", ""))
            for t in TOOLS:
                if t in cmd:
                    st["tool_calls"][t] += 1
            if STRICT_CHECK_RE.search(cmd):
                st["strict_checks"] += 1
            if DOCKER_FAIL_RE.search(cmd):
                st["docker_fail_evidence"] += 1
    st["tool_calls"] = {k: v for k, v in st["tool_calls"].items() if v}
    return st


def oracle_log(ws: str) -> dict:
    p = os.path.join(ws, ".oracle.log")
    if not os.path.isfile(p):
        return {"entries": 0}
    verdicts: dict[str, int] = {}
    rc_clean = 0
    n = 0
    for line in open(p, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        n += 1
        verdicts[d.get("verdict", "?")] = verdicts.get(d.get("verdict", "?"), 0) + 1
        if d.get("verdict") == "CLEAN" and d.get("rc") == 1:
            rc_clean += 1
    return {"entries": n, "verdicts": verdicts,
            "suspicious_clean_rc1": rc_clean,
            "note": "all-CLEAN rc=1 == docker permission failure misread"}


def report_task(ws: str) -> dict:
    sess = sessions(ws)
    per = [scan_session(f) for f in sess]
    total_checks = sum(s["strict_checks"] for s in per)
    pass2 = per[-1] if len(per) > 1 else None
    return {"task": os.path.basename(ws), "n_sessions": len(sess),
            "total": merge(per), "pass2": pass2,
            "oracle": oracle_log(ws),
            "solution_file": os.path.exists(os.path.join(ws, "SOLUTION.md")),
            "submitted_hint": total_checks > 0}


def merge(per: list[dict]) -> dict:
    out = {"asst_msgs": 0, "tool_calls": {}, "strict_checks": 0,
           "solution_writes": 0, "docker_fail_evidence": 0}
    for s in per:
        out["asst_msgs"] += s["asst_msgs"]
        out["strict_checks"] += s["strict_checks"]
        out["solution_writes"] += s["solution_writes"]
        out["docker_fail_evidence"] += s["docker_fail_evidence"]
        for k, v in s["tool_calls"].items():
            out["tool_calls"][k] = out["tool_calls"].get(k, 0) + v
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ws_root")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = []
    for ws in sorted(glob.glob(os.path.join(args.ws_root, "*"))):
        if os.path.isdir(ws):
            rows.append(report_task(ws))
    if args.json:
        json.dump(rows, sys.stdout, indent=1, ensure_ascii=False)
        print()
        return 0
    for r in rows:
        p2 = r["pass2"]
        print("%-18s sess=%d checks=%d tools=%s sol_file=%s oracle=%s" % (
            r["task"], r["n_sessions"], r["total"]["strict_checks"],
            json.dumps(r["total"]["tool_calls"]), r["solution_file"],
            json.dumps(r["oracle"].get("verdicts", {})) +
            (" SUSPECT" if r["oracle"].get("suspicious_clean_rc1") else "")))
        if p2:
            print("    pass2: asst=%d checks=%d tools=%s" % (
                p2["asst_msgs"], p2["strict_checks"], json.dumps(p2["tool_calls"])))
    tot_checks = sum(r["total"]["strict_checks"] for r in rows)
    tot_clean1 = sum(r["oracle"].get("suspicious_clean_rc1", 0) for r in rows)
    p2_checks = sum((r["pass2"] or {}).get("strict_checks", 0) for r in rows)
    p2_sessions = sum(1 for r in rows if r["pass2"])
    print("--- ARM TOTAL tasks=%d strict_checks=%d pass2_sessions=%d pass2_checks=%d "
          "oracle_suspect_clean_rc1=%d" % (
              len(rows), tot_checks, p2_sessions, p2_checks, tot_clean1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
