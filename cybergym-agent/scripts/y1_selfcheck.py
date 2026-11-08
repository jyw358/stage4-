#!/usr/bin/env python3
"""Y1 toolchain self-check (host-side, root; PR-A item 3/4, SCORE_UPGRADE_PLAN
v6 section 2.1 / docs/Y6_Y7_DESIGN.md section G).

For N diagnostic task dirs this script:
  1. generates a FRESH workspace with make_agent_ws.py (r9 P0-1 fail-closed:
     missing REQUIRED tools / SAFETY_HOOK now REFUSE instead of shipping a
     toolless workspace);
  2. chowns the workspace to the agent uid exactly like the production wrapper;
  3. runs each shipped tool's SMOKE command inside the production-equivalent
     identity (setpriv --reuid/--regid/--clear-groups, scrubbed env, private
     HOME/TMPDIR under the workspace);
  4. classifies each run: started_ok | env_failure | timeout | refused
     and emits an availability matrix as JSON.

Y1 answers "does the tool START and RUN under the task identity" -- it does
NOT judge whether an agent would use it well (that is the separate
tool-usage-conversion telemetry on real sessions).

Usage (run as root on the deployment host):
  python3 scripts/y1_selfcheck.py \
      --task-dirs '/data2/runs/y1-diag/tasks/*' \
      --ws-root    /data2/runs/y1-diag/ws \
      --agent-uid  4242 \
      --out        /data2/runs/y1-diag/y1_selfcheck_report.json

Exit code: 0 iff every REQUIRED tool started_ok on every task (fail-closed
signal for downstream experiments); optional-tool failures only warn.
"""
import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

AG_HOME = Path(__file__).resolve().parents[1]

REQUIRED_TOOLS = ("repo_map.py", "reach_graph.py", "probe_path.sh",
                  "try_local.sh", "stab_run.sh", "minsep.py", "local_oracle.py")
OPTIONAL_TOOLS = ("directed_fuzz.py", "boundary_synth.py", "mutate_seed.py",
                  "hf_mutator.py")

# Smoke commands: cheap "does it start and do its thing" invocations.
# They run in the task workspace as the agent uid; each gets its own timeout.
SMOKE = {
    "repo_map.py":      "python3 repo_map.py build",
    "reach_graph.py":   "python3 reach_graph.py",
    "probe_path.sh":    "sh probe_path.sh 2>&1 | head -3; true",
    "try_local.sh":     "printf 'x' > _y1_seed.bin && ./try_local.sh _y1_seed.bin",
    "stab_run.sh":      "printf 'x' > _y1_seed.bin && ./stab_run.sh _y1_seed.bin 2",
    "minsep.py":        "printf 'xxxx' > _y1_seed.bin && timeout 60 python3 minsep.py _y1_seed.bin",
    "local_oracle.py":  "python3 -c 'import local_oracle; print(local_oracle.DATA_DIR)'",
    "directed_fuzz.py": "printf 'xxxx' > _y1_seed.bin && timeout 90 python3 directed_fuzz.py _y1_seed.bin --seconds 30",
    "boundary_synth.py":"printf 'xxxx' > _y1_seed.bin && python3 boundary_synth.py _y1_seed.bin",
    "mutate_seed.py":   "printf 'xxxx' > _y1_seed.bin && python3 mutate_seed.py _y1_seed.bin 0:1:flip",
    # hf_mutator takes 4 positional args and runs IN-container; for the
    # out-of-container smoke a syntax/compile check is the honest level-1 test
    "hf_mutator.py":    "python3 -c 'compile(open(\"hf_mutator.py\").read(), \"hf_mutator.py\", \"exec\"); print(\"compiles_ok\")'",
}

# Env the task uid needs for the oracle tools to reach their real code paths
# (they exit early on missing CG_TASK_ID otherwise -- that would fake a PASS).
TASK_ENV_KEYS = ("CG_TASK_DIR", "CG_TASK_ID", "CG_SERVER", "CG_RUN_ID",
                 "CG_CHECKS_BUDGET")

# Patterns that mean "the ENVIRONMENT is broken", not "the task is hard".
ENV_FAILURE_PATTERNS = (
    "ModuleNotFoundError", "ImportError", "No module named",
    "command not found", "Permission denied", "not executable",
    "No such file or directory", "SyntaxError",
)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def require_root_linux() -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise SystemExit(
            "REFUSE: y1_selfcheck must run as root on the Linux deployment "
            "host (production-equivalent setpriv execution).")


def make_workspace(task_dir: Path, ws_root: Path, task_id: str,
                   tool_src: Path, timeout_s: int) -> dict:
    r = subprocess.run(
        [sys.executable, str(AG_HOME / "scripts" / "make_agent_ws.py"),
         "--task-dir", str(task_dir), "--task-id", task_id,
         "--ws-root", str(ws_root), "--tool-src", str(tool_src), "--fresh"],
        capture_output=True, text=True, timeout=timeout_s)
    return {"rc": r.returncode, "stderr_tail": r.stderr[-500:]}


def load_task_env(ws: Path) -> dict:
    """CG_* vars from the generated run.env so oracle tools run for real."""
    env = {}
    run_env = ws / "run.env"
    if run_env.is_file():
        for line in run_env.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("export "):
                body = line[len("export "):]
                key, _, val = body.partition("=")
                if key in TASK_ENV_KEYS:
                    env[key] = val
    return env


def run_tool_smoke(ws: Path, tool: str, agent_uid: int,
                   timeout_s: int, task_env: dict = None) -> dict:
    cmd = SMOKE[tool]
    tmpdir = ws / "_y1_tmp"
    tmpdir.mkdir(exist_ok=True)
    inner = (f"cd '{ws}' && umask 077 && {cmd}")
    argv = ["setpriv", f"--reuid={agent_uid}", f"--regid={agent_uid}",
            "--clear-groups", "env", "-i",
            f"HOME={ws}", f"TMPDIR={tmpdir}",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LC_ALL=C"]
    for k, v in (task_env or {}).items():
        argv.append(f"{k}={v}")
    argv += ["bash", "-c", inner]
    t0 = time.time()
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout_s)
        dur = round(time.time() - t0, 1)
        out = (r.stdout or "") + (r.stderr or "")
        env_broken = any(p in out for p in ENV_FAILURE_PATTERNS)
        if env_broken:
            classification = "env_failure"
        elif "NO_LOCAL_BUILD" in out:
            # the tool itself started fine; the task has no prebuilt local
            # vul binaries -- an infra/provisioning state, not a tool defect
            classification = "no_local_build"
        else:
            classification = "started_ok"
        return {"classification": classification, "rc": r.returncode,
                "duration_s": dur, "output_head": out[:400]}
    except subprocess.TimeoutExpired:
        return {"classification": "timeout", "rc": None,
                "duration_s": round(time.time() - t0, 1),
                "output_head": "TIMEOUT"}
    except FileNotFoundError as exc:
        return {"classification": "refused", "rc": None, "duration_s": 0,
                "output_head": f"launcher missing: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dirs", required=True, action="append",
                    help="glob of prepared task dirs; repeatable")
    ap.add_argument("--ws-root", required=True)
    ap.add_argument("--agent-uid", type=int, required=True,
                    help="numeric agent uid for production-equivalent runs")
    ap.add_argument("--tool-src", default=str(AG_HOME / "scripts"))
    ap.add_argument("--tool-timeout", type=int, default=180)
    ap.add_argument("--gen-timeout", type=int, default=300)
    ap.add_argument("--out", required=True, help="report json path")
    args = ap.parse_args()

    require_root_linux()

    task_dirs = []
    for pattern in args.task_dirs:
        task_dirs.extend(glob.glob(pattern))
    task_dirs = sorted(set(p for p in task_dirs if os.path.isdir(p)))
    if not task_dirs:
        raise SystemExit(f"REFUSE: no task dirs match {args.task_dirs}")
    ws_root = Path(args.ws_root).resolve()
    ws_root.mkdir(parents=True, exist_ok=True)

    report = {
        "selfcheck_version": "1.0",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool_src": str(Path(args.tool_src).resolve()),
        "tool_src_sha256": {
            t: sha256_of(Path(args.tool_src) / t)
            for t in REQUIRED_TOOLS + OPTIONAL_TOOLS
            if (Path(args.tool_src) / t).is_file()
        },
        "agent_uid": args.agent_uid,
        "tasks": [],
    }

    for td in task_dirs:
        td = Path(td)
        task_id = td.name  # tasks are named arvo_NNNN / oss-fuzz_NNNNN
        entry = {"task_id": task_id, "task_dir": str(td)}
        gen = make_workspace(td, ws_root, task_id.replace("_", ":", 1),
                             Path(args.tool_src), args.gen_timeout)
        entry["make_ws"] = gen
        ws = ws_root / task_id
        if gen["rc"] != 0:
            entry["result"] = "make_ws_refused"
            report["tasks"].append(entry)
            continue
        # production parity: workspace owned by the task uid (wrapper behavior)
        subprocess.run(["chown", "-R", f"{args.agent_uid}:{args.agent_uid}",
                        str(ws)], check=False)
        tools_result = {}
        task_env = load_task_env(ws)
        for tool in REQUIRED_TOOLS + OPTIONAL_TOOLS:
            if not (ws / tool).is_file():
                tools_result[tool] = {"classification": "not_shipped"}
                continue
            tools_result[tool] = run_tool_smoke(ws, tool, args.agent_uid,
                                                args.tool_timeout, task_env)
        entry["tools"] = tools_result
        entry["result"] = "ok"
        report["tasks"].append(entry)

    # availability matrix + fail-closed verdict over REQUIRED tools
    matrix = {}
    for tool in REQUIRED_TOOLS + OPTIONAL_TOOLS:
        rows = [t.get("tools", {}).get(tool, {}).get("classification",
                                                     "make_ws_refused")
                for t in report["tasks"]]
        matrix[tool] = {"counts": {c: rows.count(c) for c in set(rows)},
                        "required": tool in REQUIRED_TOOLS}
    report["matrix"] = matrix
    required_ok = all(
        matrix[t]["counts"].get("started_ok", 0) == len(report["tasks"])
        for t in REQUIRED_TOOLS)
    report["verdict"] = ("PASS" if required_ok and report["tasks"]
                         else "FAIL_required_tool_not_started")

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"[y1] verdict={report['verdict']} tasks={len(report['tasks'])} "
          f"report={args.out}")
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
