#!/usr/bin/env python3
"""directed_fuzz.py -- BOUNDED directed fuzzing of the local vul build (S2.4).

libFuzzer native targeting: seeds from YOUR best partial input, optional
-focus_function (pick one from reach_graph.py output), optional dictionary.
HARD bounds everywhere (no runaway fuzzers):

  --seconds N     wall budget, default 300, max 1800
  --fork K        parallel jobs, default 4, max 8, hard-killed at seconds+90
  artifacts       fz/artifacts/ (crash-* / timeout-* / oom-*)
  exit            triages each artifact through the local oracle verdict
                  classes and prints the next action; everything logged to
                  .oracle.log like the other tools.

  python3 directed_fuzz.py <seed_file>... [--focus FUNC] [--dict FILE]
                           [--seconds 300] [--fork 4]

Typical loop: reach_graph -> pick focus func -> directed_fuzz with your best
partial input as seed -> triage artifacts with try_local.sh / minsep.py.
"""
import argparse
import os
import pathlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_oracle import (run_side, side_info, _oracle_log, engine_of,
                         DATA_DIR, RUNNER, focus_ok, dict_name_ok)

TID = os.environ.get("CG_TASK_ID", "")


def hard_kill_containerized(cmd_proc, deadline, docker_name_hint="fz"):
    """Ensure the docker container dies with us (no orphan fork= fuzzers)."""
    pass  # handled via timeout + explicit docker kill below


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("seeds", nargs="+")
    ap.add_argument("--focus", default=None)
    ap.add_argument("--dict", default=None)
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--fork", type=int, default=1)
    ap.add_argument("--out", default="fz")
    a = ap.parse_args()
    a.seconds = min(max(a.seconds, 30), 1800)
    a.fork = min(max(a.fork, 1), 8)

    # r14-review C4: agent-supplied identifiers are validated against a
    # strict allowlist grammar BEFORE any shell command is constructed.
    if a.focus and not focus_ok(a.focus):
        print(f"REFUSE: --focus {a.focus!r} is not a valid symbol name "
              f"(letters, digits, underscores, dots only)")
        return 2
    if a.dict:
        dname = pathlib.Path(a.dict).name
        if not dict_name_ok(dname):
            print(f"REFUSE: dictionary filename {dname!r} is not a safe "
                  f"basename (letters, digits, dot, dash, underscore only)")
            return 2

    if not TID:
        print("CG_TASK_ID not set"); return 2
    vi = side_info(TID, "vul")
    if not vi:
        print("NO_LOCAL_BUILD"); return 4
    bin_dir, target, asan, _ = vi

    out = pathlib.Path(a.out)
    corp = out / "corpus"; arts = out / "artifacts"
    for p in (corp, arts):
        p.mkdir(parents=True, exist_ok=True)
    for i, s in enumerate(a.seeds[:6]):
        src = pathlib.Path(s)
        if src.is_file():
            shutil.copyfile(src, corp / f"seed{i}")

    engine = engine_of(bin_dir)

    # 3-second probe: most prebuilt ARVO binaries lack the symbolization
    # -focus_function needs; probe first, degrade gracefully instead of
    # burning the whole budget on ERROR loops.
    # r17 P1: the probe container shares the tool's lifecycle discipline --
    # unique name + tool label + verified cleanup on timeout/interrupt, so
    # a SIGKILLed probe is swept by the next tool call like any orphan.
    if engine == "libfuzzer" and a.focus:
        from local_oracle import _kill_and_verify
        probe_name = f"dfzp-{os.getpid()}-{int(time.time())}"
        probe = ["docker", "run", "--rm", "--name", probe_name,
                 "--label", "cybergym.directed_fuzz=1",
                 "-v", f"{bin_dir / 'out'}:/out:ro",
                 "-v", f"{bin_dir / 'libs'}:/out-libs:ro",
                 "-v", f"{corp.resolve()}:/corpus",
                 RUNNER, "bash", "-c",
                 f"env LD_LIBRARY_PATH=/out-libs ASAN_OPTIONS={asan} "
                 f"/out/{target} -focus_function={a.focus} -runs=1 "
                 f"-max_total_time=3 /corpus 2>&1 | tail -3"]
        try:
            pr = subprocess.run(probe, capture_output=True, text=True,
                                timeout=60)
        except subprocess.TimeoutExpired:
            _kill_and_verify(probe_name)
            print("note: focus probe timed out; running WITHOUT focus")
            a.focus = None
        except BaseException:
            _kill_and_verify(probe_name)
            raise
        if "Failed to set focus" in (pr.stdout or ""):
            print(f"note: -focus_function not supported by this binary "
                  f"(no symbolization); running WITHOUT focus -- direct the "
                  f"search with your seed + dictionary instead")
            a.focus = None

    # r16: converge containers orphaned by crashed predecessors (labels +
    # age filter never touch an active sibling); r17: results audited
    from local_oracle import sweep_label
    _sw = sweep_label("cybergym.directed_fuzz=1")
    if _sw:
        _oracle_log({"tag": "sweep", "label": "cybergym.directed_fuzz=1",
                     "swept": _sw})

    print(f"directed fuzz: engine={engine} focus={a.focus or '-'} "
          f"seconds={a.seconds} fork={a.fork} seeds={len(list(corp.glob('seed*')))}")
    t0 = time.time()
    if a.dict and pathlib.Path(a.dict).is_file():
        args_extra = [f"-dict=/poc/{pathlib.Path(a.dict).name}"]
        dict_dir_mount = ["-v", f"{pathlib.Path(a.dict).resolve().parent}:/poc:ro"]
    else:
        args_extra, dict_dir_mount = [], []
    if engine == "honggfuzz":
        # no honggfuzz binary in the runner image: container-side mini
        # mutator (bounded, dictionary-aware) drives the file-feeding target
        if a.focus:
            print("note: honggfuzz targets have no --focus; ignoring --focus")
        MUT = (Path(__file__).parent / "hf_mutator.py").read_text()
        (corp / "hf_mutator.py").write_text(MUT)
        dictname = pathlib.Path(a.dict).name if a.dict and pathlib.Path(a.dict).is_file() else ""
        inner = (f"env LD_LIBRARY_PATH=/out-libs python3 /corpus/hf_mutator.py "
                 f"/out/{target} /corpus /artifacts {a.seconds} "
                 f"{'/poc/' + dictname if dictname else ''}")
    else:
        args = [f"/out/{target}"]
        if a.focus:
            args.append(f"-focus_function={a.focus}")
        args += args_extra                      # r14 C4: -dict AFTER the
        #                                          binary (prepending made
        #                                          bash try to execute
        #                                          "-dict=..." -> rc 127)
        args += [f"-fork={a.fork}", "-ignore_crashes=1", "-ignore_timeouts=1",
                 "-ignore_ooms=1", "-max_len=1048576", "-rss_limit_mb=2560",
                 "-timeout=8", f"-max_total_time={a.seconds}",
                 f"-artifact_prefix=/artifacts/", "/corpus"]
        inner = " ".join(args)
    cname = f"dfz-{os.getpid()}-{int(time.time())}"
    cmd = ["docker", "run", "--rm", "--name", cname,
           "--label", "cybergym.directed_fuzz=1",
           "-v", f"{bin_dir / 'out'}:/out:ro",
           "-v", f"{bin_dir / 'libs'}:/out-libs:ro",
           *dict_dir_mount,
           "-v", f"{corp.resolve()}:/corpus",
           "-v", f"{arts.resolve()}:/artifacts",
           RUNNER, "bash", "-c",
           f"echo __ORACLE_RUNNER_V2__; env LD_LIBRARY_PATH=/out-libs "
           f"ASAN_OPTIONS={asan} {inner}"]
    try:
        r = subprocess.run(cmd, capture_output=True,
                           timeout=a.seconds + 90)
        stdout_text = (r.stdout or b"").decode("utf-8", errors="replace")
        stderr_text = (r.stderr or b"").decode("utf-8", errors="replace")
        full = stdout_text + "\n" + stderr_text
        rc, tail = r.returncode, full[-1200:]
    except subprocess.TimeoutExpired:
        # r15: verified cleanup (was a bare unchecked `docker kill`)
        from local_oracle import _kill_and_verify
        cleanup, kill_rc = _kill_and_verify(cname)
        stdout_text, full, rc = "", "", 124
        tail = (f"HARD WALL CLOCK KILL (container terminated, "
                f"cleanup={cleanup}, kill_rc={kill_rc})")
    except BaseException:
        # r16: caller interrupt must not orphan the fuzz container
        from local_oracle import _kill_and_verify
        cleanup, _krc = _kill_and_verify(cname)
        print(f"INTERRUPTED: verified container cleanup -> {cleanup}")
        raise
    if stdout_text.splitlines()[:1] != ["__ORACLE_RUNNER_V2__"]:
        # P0-6: the fuzz RUNNER never started (docker permission/daemon) --
        # fail closed; "no artifacts" here means NOTHING, do not interpret it.
        _oracle_log({"tag": "directed_fuzz", "side": "vul", "target": target,
                     "files": len(list(corp.glob('seed*'))), "perturb": None,
                     "timeout": a.seconds, "verdict": "ORACLE_ERROR",
                     "runner_started": False, "target_started": False,
                     "runner_exit_code": rc, "target_exit_code": None,
                     "runner_error_kind": "other", "sanitizer_detected": False,
                     "tail": tail[-300:], "wall_s": round(time.time() - t0, 1)})
        print("RESULT: ORACLE_ERROR (fuzz runner failed -- docker unavailable);")
        print("local fuzzing is UNAVAILABLE, not empty. Do NOT conclude")
        print("'no crash found'; treat this session as having no local signal.")
        return 7
    _oracle_log({"tag": "directed_fuzz", "side": "vul", "target": target,
                 "files": len(list(corp.glob('seed*'))), "perturb": None,
                 "timeout": a.seconds, "verdict": f"DONE rc={rc}",
                 "rc": rc, "sig": (a.focus or "")[:120],
                 "tail": "", "wall_s": round(time.time() - t0, 1)})
    if rc in (126, 127):
        # r14 C4: the runner started but the TARGET/fuzzer could not launch
        # -- this is not "no crashes found"
        print(f"RESULT: VERIFICATION-UNAVAILABLE (target launch failed rc={rc});")
        print("the fuzzer never started. Do NOT conclude 'no crash found'.")
        return 7
    m = re.search(r"stat::number_of_executed_units:\s*(\d+)", tail)
    print(f"container rc={rc} wall={time.time()-t0:.0f}s "
          f"execs={m.group(1) if m else '?'} " \
                    f"corpus={len([x for x in corp.iterdir() if x.suffix != '.py'])}")
    if rc != 0:
        print("--- container output tail ---")
        print(tail[-800:])

    # r14 C4: only artifacts from THIS run (mtime >= run start) count
    artifacts = sorted(x for x in arts.glob("*") if x.stat().st_mtime >= t0
                       and x.suffix != ".log")
    if not artifacts:
        if rc != 0:
            # abnormal campaign end without artifacts: no conclusion either
            print("RESULT: VERIFICATION-UNAVAILABLE (campaign rc!=0, no")
            print("artifacts); do NOT conclude 'no crash found'.")
            return 7
        print("no artifacts -- widen the seed, adjust --focus, or raise --seconds")
        return 0
    print(f"\n{len(artifacts)} artifacts; triaging (oracle verdicts):")
    interesting = []
    for art in artifacts[:12]:
        v = run_side(vi, [str(art)], tag="dfuzz_triage")
        mark = "FATAL" if v["verdict"] == "FATAL" else v["verdict"]
        print(f"  {mark:<8} {art.name}  {v['sig'][:70]}")
        if v["verdict"] in ("FATAL", "NONFATAL"):
            interesting.append(art)
    if interesting:
        print(f"\nnext: ./try_local.sh {interesting[0]}  (differential gate), "
              f"then python3 minsep.py {interesting[0]}")
        return 1
    print("\nno fatal artifacts; keep iterating")
    return 0


if __name__ == "__main__":
    sys.exit(main())
