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
          "strict_checks": 0, "solution_writes": 0, "docker_fail_evidence": 0,
          "first_check_seq": None, "event_ts": []}
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        st["asst_msgs"] += 1
        ts = str(d.get("timestamp", ""))[:19]
        if ts:
            st["event_ts"].append(ts)
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
            if STRICT_CHECK_RE.search(cmd) and st["first_check_seq"] is None:
                st["first_check_seq"] = st["asst_msgs"]
            if STRICT_CHECK_RE.search(cmd):
                st["strict_checks"] += 1
            if DOCKER_FAIL_RE.search(cmd):
                st["docker_fail_evidence"] += 1
    st["tool_calls"] = {k: v for k, v in st["tool_calls"].items() if v}
    return st


def oracle_log(ws: str) -> dict:
    """Two-layer oracle telemetry (P0-6.5, r10 review).

    Schema-2 entries carry runner_started/target_started/exit codes, so
    validity is decided by execution proof -- not by the legacy
    `CLEAN rc=1` heuristic (kept only as a red flag for old logs)."""
    p = os.path.join(ws, ".oracle.log")
    if not os.path.isfile(p):
        return {"entries": 0}
    st = {"entries": 0, "target_executions_confirmed": 0,
          "oracle_errors": 0, "oracle_clean_confirmed": 0,
          "oracle_crashes_confirmed": 0, "legacy_clean_rc1_suspect": 0,
          "first_error_ts": None, "first_confirmed_ts": None,
          "verdicts": {}}
    for line in open(p, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        st["entries"] += 1
        v = d.get("verdict", "?")
        st["verdicts"][v] = st["verdicts"].get(v, 0) + 1
        if d.get("schema") == 2 or "runner_started" in d:
            if d.get("runner_started") and d.get("target_started"):
                st["target_executions_confirmed"] += 1
                ts = d.get("ts")
                if st["first_confirmed_ts"] is None and ts:
                    st["first_confirmed_ts"] = ts
                if v == "CLEAN":
                    st["oracle_clean_confirmed"] += 1
                elif v in ("FATAL", "NONFATAL"):
                    st["oracle_crashes_confirmed"] += 1
                elif v in ("ORACLE_ERROR", "TARGET_LAUNCH_ERROR", "UNKNOWN"):
                    st["oracle_errors"] += 1
                    ts = d.get("ts")
                    if st["first_error_ts"] is None and ts:
                        st["first_error_ts"] = ts
            else:
                st["oracle_errors"] += 1
                ts = d.get("ts")
                if st["first_error_ts"] is None and ts:
                    st["first_error_ts"] = ts
        else:
            # schema-1 legacy entries: cannot prove execution
            if v == "CLEAN" and d.get("rc") == 1:
                st["legacy_clean_rc1_suspect"] += 1
                st["oracle_errors"] += 1
                ts = d.get("ts")
                if st["first_error_ts"] is None and ts:
                    st["first_error_ts"] = ts
            elif v in ("FATAL", "NONFATAL"):
                st["oracle_crashes_confirmed"] += 1
    n = st["entries"]
    st["valid_local_verification_rate"] = (
        round(st["target_executions_confirmed"] / n, 3) if n else None)
    return st


def report_task(ws: str) -> dict:
    sess = sessions(ws)
    per = [scan_session(f) for f in sess]
    total_checks = sum(s["strict_checks"] for s in per)
    pass2 = per[-1] if len(per) > 1 else None
    orc = oracle_log(ws)
    # turns spent after the oracle first proved unavailable (r10 metric:
    # how much exploration budget was burned on a dead oracle)
    err_ts = (orc.get("first_error_ts") or "")[:19]
    turns_after_err = None
    if err_ts:
        turns_after_err = sum(1 for s in per for t in s["event_ts"]
                              if t > err_ts)
    return {"task": os.path.basename(ws), "n_sessions": len(sess),
            "total": merge(per), "pass2": pass2,
            "oracle": orc,
            "first_check_turn": per[0]["first_check_seq"] if per else None,
            "turns_after_first_oracle_error": turns_after_err,
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
        o = r["oracle"]
        print("%-18s sess=%d checks=%d@turn%s tools=%s sol_file=%s oracle=%s "
              "valid_rate=%s" % (
                  r["task"], r["n_sessions"], r["total"]["strict_checks"],
                  r["first_check_turn"], json.dumps(r["total"]["tool_calls"]),
                  r["solution_file"], json.dumps(o.get("verdicts", {})),
                  o.get("valid_local_verification_rate")))
        if r.get("turns_after_first_oracle_error") is not None:
            print("    turns_after_first_oracle_error=%s" %
                  r["turns_after_first_oracle_error"])
        if p2:
            print("    pass2: asst=%d checks=%d tools=%s" % (
                p2["asst_msgs"], p2["strict_checks"], json.dumps(p2["tool_calls"])))
    tot_checks = sum(r["total"]["strict_checks"] for r in rows)
    tot_exec = sum(r["oracle"].get("target_executions_confirmed", 0) for r in rows)
    tot_entries = sum(r["oracle"].get("entries", 0) for r in rows)
    tot_err = sum(r["oracle"].get("oracle_errors", 0) for r in rows)
    tot_wasted = sum(r.get("turns_after_first_oracle_error") or 0 for r in rows)
    p2_checks = sum((r["pass2"] or {}).get("strict_checks", 0) for r in rows)
    p2_sessions = sum(1 for r in rows if r["pass2"])
    print("--- ARM TOTAL tasks=%d strict_checks=%d pass2_sessions=%d pass2_checks=%d" % (
        len(rows), tot_checks, p2_sessions, p2_checks))
    print("--- ORACLE entries=%d target_executions_confirmed=%d oracle_errors=%d "
          "arm_valid_rate=%s turns_after_oracle_error=%d" % (
              tot_entries, tot_exec, tot_err,
              (round(tot_exec / tot_entries, 3) if tot_entries else None),
              tot_wasted))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
