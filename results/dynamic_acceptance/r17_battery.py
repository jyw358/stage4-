#!/usr/bin/env python3
"""r17 battery v2: rerun F1-F5 + L1-L5, plus
  L4b  DEFAULT-threshold convergence (the r17 P0 demand): real orphan,
       aged >= 15 min, cleaned by the NEXT tool call with default
       params, timestamped evidence; a YOUNG labeled sibling must
       SURVIVE the same sweep (never-touch-young proof);
  L4m  mechanism check with EXPLICIT min_age_min=0 (clearly labelled as
       an override -- this is what the r16 report failed to disclose);
  L6   focus-probe lifecycle: SIGKILL during probe leaves a LABELED
       orphan (dfzp-*) that the same convergence contract covers.
"""
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from unittest import mock

# harness fix (r17): launched via `nohup ... &` from non-interactive bash,
# SIGINT arrives as SIG_IGN (POSIX background-job inheritance) and python
# PRESERVES the ignore disposition -- which made L3 a false failure in v2.
# Restore the default handler so Popen children exec with SIG_DFL.
signal.signal(signal.SIGINT, signal.default_int_handler)

OUT = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
       "local_oracle": {}, "directed_fuzz": {}, "residue": {}, "pass": True}
R = pathlib.Path("/data2/r16_battery"); R.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, "/opt/cybergym-agent-r33/scripts")
import local_oracle as lo  # noqa: E402

vi = lo.side_info("arvo:1348", "vul") or lo.side_info("arvo:11033", "vul")
assert vi, "no prebuilt vul side"


def label_count(lbl):
    r = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={lbl}"],
                       capture_output=True, text=True, timeout=15)
    return len((r.stdout or "").split())


def rec(section, name, **kv):
    OUT[section][name] = kv
    ok = kv.pop("ok", True)
    print(("PASS " if ok else "FAIL ") + f"{section}/{name}: {kv}", flush=True)
    if not ok:
        OUT["pass"] = False


# ================= L4b FIRST (needs the 15-minute age window) ==============
ORPHAN = "dfz-r17-orphan-proof"
YOUNG = "dfz-r17-young-sibling"
subprocess.run(["docker", "rm", "-f", ORPHAN, YOUNG], capture_output=True)
t_create = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
subprocess.run(["docker", "run", "-d", "--rm",
                "--label", "cybergym.directed_fuzz=1",
                "--name", ORPHAN, lo.RUNNER, "sleep", "1800"],
               capture_output=True, timeout=60)
orph_id = subprocess.run(["docker", "ps", "-q", "--filter", f"name={ORPHAN}"],
                         capture_output=True, text=True).stdout.strip()
OUT["L4b_evidence"] = {"orphan_created_utc": t_create,
                       "orphan_name": ORPHAN, "orphan_id": orph_id,
                       "label": "cybergym.directed_fuzz=1"}
print(f"L4b orphan {orph_id} created at {t_create}; waiting 15.5 min",
      flush=True)
time.sleep(15.5 * 60)
# young labeled sibling created just before the sweep
subprocess.run(["docker", "run", "-d", "--rm",
                "--label", "cybergym.directed_fuzz=1",
                "--name", YOUNG, lo.RUNNER, "sleep", "600"],
               capture_output=True, timeout=60)
age_line = subprocess.run(
    ["docker", "ps", "--filter", f"name={ORPHAN}", "--format",
     "{{.RunningFor}}"], capture_output=True, text=True).stdout.strip()
OUT["L4b_evidence"]["orphan_age_at_sweep"] = age_line
OUT["L4b_evidence"]["sweep_call"] = ("directed_fuzz startup "
                                     "sweep_label(DEFAULT 15.0)")
# harness fix (r17): a directed_fuzz orphan is converged by the NEXT
# directed_fuzz invocation (per-label contract); run_side sweeps only
# its own label. Trigger via a real short df run.
WSb = pathlib.Path(tempfile.mkdtemp(prefix="r17l4b-"))
for f in ("directed_fuzz.py", "local_oracle.py"):
    shutil.copyfile(f"/opt/cybergym-agent-r33/scripts/{f}", WSb / f)
(WSb / "seed0").write_bytes(b"x" * 64)
ORACLE_LOG = str(R / "l4b_oracle.log")
ENVb = {**os.environ, "CG_TASK_ID": "arvo:1348", "ORACLE_LOG": ORACLE_LOG}
pL4b = subprocess.run(
    [sys.executable, str(WSb / "directed_fuzz.py"),
     "seed0", "--seconds", "30"],
    cwd=WSb, env=ENVb, capture_output=True, text=True, timeout=300)
time.sleep(3)
orph_gone = label_count("cybergym.directed_fuzz=1")  # young sibling remains
young_alive = subprocess.run(
    ["docker", "ps", "-q", "--filter", f"name={YOUNG}"],
    capture_output=True, text=True).stdout.strip()
sweep_logged = ""
try:
    for ln in open(ORACLE_LOG):
        if '"tag": "sweep"' in ln or '"tag":"sweep"' in ln:
            sweep_logged = ln.strip()[:400]
except OSError:
    pass
OUT["L4b_evidence"]["sweep_log_line"] = sweep_logged
OUT["L4b_evidence"]["young_sibling_alive_after_sweep"] = bool(young_alive)
OUT["L4b_evidence"]["df_trigger_rc"] = pL4b.returncode
subprocess.run(["docker", "rm", "-f", YOUNG, ORPHAN], capture_output=True)
shutil.rmtree(WSb, ignore_errors=True)
rec("directed_fuzz", "L4b_default_15min_convergence",
    df_trigger_rc=pL4b.returncode, orphan_age=age_line,
    young_survived=bool(young_alive),
    remaining_labeled=orph_gone,
    ok=("cleanup_verified" in sweep_logged and bool(young_alive)
        and orph_gone == 1))   # only the young sibling remains

# ================= F1-F5 ====================================================
seed = pathlib.Path(tempfile.mkdtemp()) / "in"
seed.write_bytes(b"x")
r1 = lo.run_side(vi, [str(seed)], tag="r17_F1")
rec("local_oracle", "F1_normal_exit", verdict=r1["verdict"],
    ok=r1["verdict"] in ("CLEAN", "UNKNOWN"))

real_run = lo.subprocess.run


def fake_run_f2(cmd, *a, **k):
    if cmd[:2] == ["docker", "run"]:
        real_run(["docker", "run", "-d", "--rm",
                  "--label", "cybergym.local_oracle=1",
                  "--name", cmd[cmd.index("--name") + 1],
                  lo.RUNNER, "sleep", "120"],
                 capture_output=True, timeout=30)
        raise lo.subprocess.TimeoutExpired(cmd, 1)
    return real_run(cmd, *a, **k)


with mock.patch.object(lo.subprocess, "run", side_effect=fake_run_f2):
    r2 = lo.run_side(vi, [str(seed)], tag="r17_F2")
time.sleep(3)
res2 = label_count("cybergym.local_oracle=1")
rec("local_oracle", "F2_outer_timeout", verdict=r2["verdict"],
    kind=r2.get("runner_error_kind"), cleanup=r2.get("cleanup"),
    residue=res2, ok=(r2["verdict"] == "ORACLE_ERROR"
                      and r2.get("cleanup") == "cleanup_verified"
                      and res2 == 0))

os.environ["DOCKER_HOST"] = "tcp://127.0.0.1:1"
try:
    r3 = lo.run_side(vi, [str(seed)], tag="r17_F3")
finally:
    os.environ.pop("DOCKER_HOST", None)
rec("local_oracle", "F3_daemon_unreachable", verdict=r3["verdict"],
    kind=r3.get("runner_error_kind"),
    ok=(r3["verdict"] == "ORACLE_ERROR"))

with mock.patch.object(lo, "_container_gone", return_value=None):
    c4, _ = lo._post_abnormal_cleanup("ghost")
rec("local_oracle", "F4_inspection_failure", cleanup=c4,
    ok=c4 == "inspection_incomplete")

with mock.patch.object(lo, "_container_gone", return_value=False), \
        mock.patch.object(lo, "_kill_and_verify",
                          return_value=("cleanup_failed", 1)):
    c5, _ = lo._post_abnormal_cleanup("ghostb")
rec("local_oracle", "F5_kill_failure", cleanup=c5, ok=c5 == "cleanup_failed")

# ================= directed_fuzz L1-L5 ======================================
WS = pathlib.Path(tempfile.mkdtemp(prefix="r17df-"))
for f in ("directed_fuzz.py", "local_oracle.py"):
    shutil.copyfile(f"/opt/cybergym-agent-r33/scripts/{f}", WS / f)
(WS / "seed0").write_bytes(b"x" * 64)
ENV = {**os.environ, "CG_TASK_ID": "arvo:1348", "ORACLE_LOG": str(WS / ".o")}


def df_run(argv, timeout=400):
    return subprocess.run([sys.executable, str(WS / "directed_fuzz.py")]
                          + argv, cwd=WS, env=ENV, capture_output=True,
                          text=True, timeout=timeout)


p1 = df_run(["seed0", "--seconds", "30"])
time.sleep(2)
rec("directed_fuzz", "L1_normal", rc=p1.returncode,
    residue=label_count("cybergym.directed_fuzz=1"),
    ok=(p1.returncode in (0, 1)
        and label_count("cybergym.directed_fuzz=1") == 0))

harness = r'''
import subprocess, sys
sys.path.insert(0, ".")
import directed_fuzz as df
real = subprocess.run
def inject(cmd, *a, **k):
    if cmd[:2] == ["docker", "run"] and any(
            isinstance(c, str) and "dfz-" in c for c in cmd):
        cname = cmd[cmd.index("--name") + 1]
        real(["docker", "run", "-d", "--rm",
              "--label", "cybergym.directed_fuzz=1",
              "--name", cname, df.RUNNER, "sleep", "120"],
             capture_output=True, timeout=30)
        raise subprocess.TimeoutExpired(cmd, 25)
    return real(cmd, *a, **k)
df.subprocess.run = inject
sys.argv = ["directed_fuzz.py", "seed0", "--seconds", "90"]
sys.exit(df.main() or 0)
'''
(WS / "_l2.py").write_text(harness)
p2 = subprocess.run([sys.executable, "_l2.py"], cwd=WS, env=ENV,
                    capture_output=True, text=True, timeout=400)
out2 = p2.stdout + p2.stderr
time.sleep(3)
ok2 = ("RESULT: ORACLE_ERROR" in out2 and p2.returncode == 7
       and label_count("cybergym.directed_fuzz=1") == 0)
rec("directed_fuzz", "L2_timeout_verified_kill", rc=p2.returncode,
    residue=label_count("cybergym.directed_fuzz=1"), ok=ok2)

p3 = subprocess.Popen([sys.executable, str(WS / "directed_fuzz.py"),
                       "seed0", "--seconds", "120"], cwd=WS, env=ENV,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
time.sleep(10)
p3.send_signal(signal.SIGINT)
try:
    p3.wait(timeout=90)
except subprocess.TimeoutExpired:
    p3.kill(); p3.wait()
o3 = (p3.stdout.read() or "") + (p3.stderr.read() or "")
time.sleep(3)
res3 = label_count("cybergym.directed_fuzz=1")
rec("directed_fuzz", "L3_sigint_guard", rc=p3.returncode,
    saw_cleanup=("cleanup" in o3), residue=res3,
    ok=(res3 == 0 and "cleanup" in o3))

# L4m: mechanism with EXPLICIT override (disclosed, not the default path)
p4 = subprocess.Popen([sys.executable, str(WS / "directed_fuzz.py"),
                       "seed0", "--seconds", "120"], cwd=WS, env=ENV,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(10)
p4.kill(); p4.wait()
orphan4 = label_count("cybergym.directed_fuzz=1")
swept = lo.sweep_label("cybergym.directed_fuzz=1", min_age_min=0)  # OVERRIDE
time.sleep(3)
res4 = label_count("cybergym.directed_fuzz=1")
rec("directed_fuzz", "L4m_mechanism_explicit_override", orphan=orphan4,
    sweep=swept, residue=res4,
    ok=(orphan4 >= 1 and res4 == 0
        and all(s.get("cleanup") == "cleanup_verified" for s in swept)))

p5 = subprocess.Popen([sys.executable, str(WS / "directed_fuzz.py"),
                       "seed0", "--seconds", "120"], cwd=WS, env=ENV,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
time.sleep(10)
r5c = subprocess.run(["docker", "ps", "-q", "--filter",
                      "label=cybergym.directed_fuzz=1"],
                     capture_output=True, text=True, timeout=15)
for cid in (r5c.stdout or "").split():
    subprocess.run(["docker", "kill", cid], capture_output=True, timeout=30)
try:
    p5.wait(timeout=120)
except subprocess.TimeoutExpired:
    p5.kill(); p5.wait()
time.sleep(3)
rec("directed_fuzz", "L5_external_container_kill", rc=p5.returncode,
    residue=label_count("cybergym.directed_fuzz=1"),
    ok=(label_count("cybergym.directed_fuzz=1") == 0))

# L6: focus-probe lifecycle -- SIGKILL during probe leaves a LABELED
# dfzp- orphan; mechanism + default-threshold convergence identical (L4b)
probe_harness = r'''
import subprocess, sys, time
sys.path.insert(0, ".")
import directed_fuzz as df
real = subprocess.run
def hang_probe(cmd, *a, **k):
    if cmd[:2] == ["docker", "run"] and any(
            isinstance(c, str) and "dfzp-" in c for c in cmd):
        cname = cmd[cmd.index("--name") + 1]
        real(["docker", "run", "-d", "--rm",
              "--label", "cybergym.directed_fuzz=1",
              "--name", cname, df.RUNNER, "sleep", "300"],
             capture_output=True, timeout=30)
        time.sleep(600)          # simulates a probe that never returns
    return real(cmd, *a, **k)
df.subprocess.run = hang_probe
sys.argv = ["directed_fuzz.py", "seed0", "--focus", "some_func",
            "--seconds", "60"]
df.main()
'''
(WS / "_l6.py").write_text(probe_harness)
p6 = subprocess.Popen([sys.executable, "_l6.py"], cwd=WS, env=ENV,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(8)
p6.kill(); p6.wait()
l6_orphans = subprocess.run(
    ["docker", "ps", "-q", "--filter", "name=dfzp-"],
    capture_output=True, text=True).stdout.split()
l6_labeled = [c for c in l6_orphans]
swept6 = lo.sweep_label("cybergym.directed_fuzz=1", min_age_min=0)  # OVERRIDE
time.sleep(3)
rec("directed_fuzz", "L6_probe_sigkill_labeled_orphan",
    orphans=len(l6_labeled), sweep=swept6,
    residue=label_count("cybergym.directed_fuzz=1"),
    ok=(len(l6_labeled) >= 1
        and label_count("cybergym.directed_fuzz=1") == 0
        and all(s.get("cleanup") == "cleanup_verified" for s in swept6)))

OUT["residue"] = {
    "local_oracle_labeled": label_count("cybergym.local_oracle=1"),
    "directed_fuzz_labeled": label_count("cybergym.directed_fuzz=1"),
}
OUT["residue_ok"] = (OUT["residue"]["local_oracle_labeled"] == 0
                     and OUT["residue"]["directed_fuzz_labeled"] == 0)
if not OUT["residue_ok"]:
    OUT["pass"] = False
(R / "battery_v2.json").write_text(json.dumps(OUT, indent=2))
print("BATTERY_V2_" + ("PASS" if OUT["pass"] else "FAIL"), flush=True)
shutil.rmtree(WS, ignore_errors=True)
sys.exit(0 if OUT["pass"] else 1)
