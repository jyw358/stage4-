#!/usr/bin/env python3
"""Shared local docker oracle for CyberGym task workspaces (Stage-1 toolset).

Used by try_local.sh (differential gate), minsep.py (ddmin + fix gate) and
stab_run.sh (stability probe). Verdicts are aligned with the official server:

  * inner timeout 10 s  (server DEFAULT_CMD_TIMEOUT)
  * 137/124 -> TIMEOUT  (server maps SIGKILL to 300 "not crashed")
  * exit 77   -> NONFATAL (libFuzzer -error_exitcode; in this dataset ~91%
                          are MemorySanitizer reports, officially counted)
  * FATAL     -> ASan/UBSan/TSan/deadly-signal class (exit 1 path)

P0-6 (2026-11-08, r10 audit): classification is now EXECUTION-STATE-FIRST and
fail-closed. The inner script emits machine sentinels that PROVE the container
and the target actually ran:

    __ORACLE_RUNNER_V2__           printed by bash inside the container
    __ORACLE_TARGET_EXIT_V2__<rc>  $? of the target/timeout command

Only a run whose sentinels prove `runner_started AND target_started` may be
classified CLEAN/CRASH-class. Docker CLI failures (permission denied on the
socket, daemon down, image missing), container-level failures and anything
unexplained return ORACLE_ERROR / TARGET_LAUNCH_ERROR / UNKNOWN -- NEVER CLEAN.
String matching on docker stderr is used only to fill the diagnostic
`runner_error_kind`; the verdict itself is gated by the sentinels, so error
text in any language/casing cannot be misread as "input did not crash".
Result dicts are machine-readable (schema 2): see run_side().

Fix-side differential runs are BUDGETED (default 5 per task) via a counter
file `.diffgate` in the workspace -- see gate_take().
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

DATA_DIR = pathlib.Path("/data/cybergym/cybergym-server-data")
# env override exists for DIAGNOSTICS ONLY (y1 scenario 'image unavailable');
# production runs never set it and get the pinned runner image.
RUNNER = os.environ.get("ORACLE_RUNNER", "cybergym/oss-fuzz-base-runner:latest")

TIMEOUT = int(os.environ.get("TRY_LOCAL_TIMEOUT", "10"))    # server alignment
DIFF_MAX = int(os.environ.get("TRY_LOCAL_DIFF_MAX", "5"))   # fix-side cap/task
GATE_FILE = ".diffgate"

_TARGET_RE = re.compile(r"/out/(\S+)\s+/tmp/poc")
_ASAN_RE = re.compile(r"export ASAN_OPTIONS=(\S+)")
FATAL_RE = re.compile(
    r"ERROR: (AddressSanitizer|UndefinedBehaviorSanitizer|ThreadSanitizer|LeakSanitizer|libFuzzer):"
    r"|SUMMARY: (Address|Undefined|Thread|Leak)Sanitizer")
NONFATAL_RE = re.compile(r"MemorySanitizer")
LIBFUZZ_TIMEOUT_RE = re.compile(r"ERROR: libFuzzer: (timeout|out-of-memory)")


def side_info(tid_full: str, side: str):
    """Resolve (bin_dir, target, asan_opts, kind) for the VUL side; None if absent.

    2026-10-09 D4 strip (user decision): side!='vul' now returns None -- the
    stage3 contract forbids fix-side access (mission L29)."""
    if side != "vul":
        return None
    kind, _, tid = tid_full.partition(":")
    if kind == "oss-fuzz":
        d = DATA_DIR / "oss-fuzz" / tid / side
        meta = d / "metadata.json"
        if not (d / "out").is_dir() or not meta.is_file():
            return None
        m = json.loads(meta.read_text(errors="replace"))
        return d, m.get("fuzz_target"), "detect_leaks=0", "oss-fuzz"
    d = DATA_DIR / "arvo" / tid / side
    if not (d / "out").is_dir():
        return None
    text = (d / "arvo").read_text(encoding="utf-8", errors="ignore")
    mt = _TARGET_RE.search(text)
    if not mt:
        return None
    ma = _ASAN_RE.search(text)
    return d, mt.group(1), (ma.group(1) if ma else "detect_leaks=0"), "arvo"


def engine_of(bin_dir) -> str:
    """"libfuzzer" | "honggfuzz" from the task's arvo runner script."""
    try:
        txt = (pathlib.Path(bin_dir) / "arvo").read_text(errors="replace")
        return "honggfuzz" if "FUZZING_ENGINE=honggfuzz" in txt else "libfuzzer"
    except Exception:
        return "libfuzzer"


def classify(rc: int, out: str) -> str:
    """Legacy shim (schema 1) -- DO NOT use for new code (P0-6).

    Kept only so ancient callers importing `classify` do not break; it cannot
    distinguish "did not run" from "ran clean" and therefore must not gate
    decisions. New code calls classify_exec().
    """
    if rc in (124, 137) or LIBFUZZ_TIMEOUT_RE.search(out):
        return "TIMEOUT"
    if FATAL_RE.search(out):
        return "FATAL"
    if NONFATAL_RE.search(out) or rc == 77:
        return "NONFATAL"
    return "CLEAN"


# ---- P0-6: execution-state-first classification --------------------------

RUNNER_SENTINEL = "__ORACLE_RUNNER_V2__"
_TARGET_EXIT_RE = re.compile(r"__ORACLE_TARGET_EXIT_V2__(-?\d+)")

# diagnostic only (verdict is gated by sentinels, never by these strings)
_ERR_KIND_RES = (
    (re.compile(r"permission denied", re.I), "docker_permission"),
    (re.compile(r"cannot connect to the docker daemon|is the docker daemon running",
                re.I), "daemon_unreachable"),
    (re.compile(r"unable to find image|manifest unknown|pull access denied",
                re.I), "image_unavailable"),
    (re.compile(r"no such file or directory.*docker\.sock|cannot find.*docker\.sock",
                re.I), "socket_missing"),
)


def runner_error_kind(out: str) -> str:
    for rx, kind in _ERR_KIND_RES:
        if rx.search(out):
            return kind
    return "other"


def classify_exec(runner_started: bool, target_exit, out: str) -> str:
    """State-first verdict. `target_exit` is the sentinel-parsed exit code of
    the target command (None if the sentinel never appeared). Unknown states
    never fall through to CLEAN."""
    if not runner_started:
        return "ORACLE_ERROR"
    if target_exit is None:
        return "ORACLE_ERROR"          # bash ran but the post-target echo was
        #                                  lost (whole-container kill etc.)
    if target_exit in (126, 127):
        return "TARGET_LAUNCH_ERROR"   # bash/loader could not start the target
    if target_exit in (124, 137) or LIBFUZZ_TIMEOUT_RE.search(out):
        return "TIMEOUT"
    if FATAL_RE.search(out):
        return "FATAL"
    if NONFATAL_RE.search(out) or target_exit == 77:
        return "NONFATAL"
    if target_exit == 0:
        return "CLEAN"
    return "UNKNOWN"


_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{4,}")


def signature(out: str) -> str:
    """First sanitizer SUMMARY/ERROR line, addresses normalised (ASLR-proof)."""
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("==") and ("ERROR:" in s or "WARNING:" in s) or "SUMMARY:" in s:
            s = re.sub(r"==\d+==", "", s)
            return _ADDR_RE.sub("0xADDR", s)[:200]
    return ""


def _oracle_log(rec):
    """Persist every local oracle run for post-hoc review (JSONL, append)."""
    import datetime
    try:
        path = pathlib.Path(os.environ.get("ORACLE_LOG", ".oracle.log"))
        with open(path, "a") as f:
            rec = {"ts": datetime.datetime.utcnow().isoformat() + "Z", **rec}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def run_side(info, files, timeout: int = None, perturb=None, tag=""):
    _bd = str(info[0]) if isinstance(info, (tuple, list)) and info else str(info)
    if "/fix/" in _bd.replace("//", "/") or _bd.rstrip("/").endswith("/fix"):
        return {"verdict": "FIX_SIDE_BLOCKED", "rc": -1, "sig": "",
                "tail": "fix-side local runs disabled (stage3 contract: NO fix-side access; D4 strip 10-09)"}
    """Run inputs once against one side. Returns a schema-2 dict:

      verdict            ORACLE_ERROR | TARGET_LAUNCH_ERROR | FATAL | NONFATAL
                         | TIMEOUT | CLEAN | UNKNOWN  (fail-closed: unknown
                         execution states NEVER become CLEAN)
      runner_started     container bash provably ran (sentinel seen)
      target_started     the post-target echo provably ran
      runner_exit_code   docker CLI exit code (diagnostic; on a successful
                         container run this equals the inner bash exit code)
      target_exit_code   $? of `timeout ... /out/<target>` parsed from the
                         sentinel (None if never produced)
      runner_error_kind  docker_permission|daemon_unreachable|image_unavailable|
                         socket_missing|runner_hang|target_incomplete|other
      sanitizer_detected bool
      sig, tail          as before

    `files` -- list of LOCAL paths (mounted /poc/in0..); `perturb` -- optional
    MALLOC_PERTURB_ byte value (glibc exploration probe; disclosed, local only).
    Every invocation is appended to ./.oracle.log for retrospective analysis.
    """
    timeout = timeout or TIMEOUT
    bin_dir, target, asan, _kind = info
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="loracle-"))
    try:
        mounted = []
        for i, f in enumerate(files[:8]):
            p = pathlib.Path(f)
            if p.is_file():
                shutil.copyfile(p, tmp / f"in{i}")
                mounted.append(f"/poc/in{i}")
        if not mounted:
            return {"verdict": "NO_INPUT", "rc": -1, "sig": "", "tail": "no readable files"}
        env_prefix = f"env LD_LIBRARY_PATH=/out-libs ASAN_OPTIONS={asan}"
        if perturb is not None:
            env_prefix += f" MALLOC_PERTURB_={perturb}"
        # P0-6: sentinels prove execution; the target's own exit code is echoed
        # explicitly so it can never be confused with the docker CLI's rc.
        inner = (f"echo {RUNNER_SENTINEL}; "
                 f"{env_prefix} timeout -s SIGKILL {timeout} /out/{target} "
                 + " ".join(mounted)
                 + f"; echo __ORACLE_TARGET_EXIT_V2__$?")
        cmd = ["docker", "run", "--rm",
               "-v", f"{bin_dir / 'out'}:/out:ro",
               "-v", f"{bin_dir / 'libs'}:/out-libs:ro",
               "-v", f"{tmp}:/poc:ro", RUNNER, "bash", "-c", inner]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 150)
            rc, out = r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            # the RUNNER was unresponsive (docker CLI/container hang) -- this is
            # a runner failure, not a target timeout (P0-6: never "TIMEOUT").
            v = {"schema": 2, "verdict": "ORACLE_ERROR",
                 "runner_started": False, "target_started": False,
                 "runner_exit_code": None, "target_exit_code": None,
                 "runner_error_kind": "runner_hang", "sanitizer_detected": False,
                 "sig": "", "tail": "container unresponsive (outer kill)"}
            _oracle_log({"tag": tag or "run_side", "side": info[0].name,
                         "target": info[1], "files": len(mounted), "perturb": perturb,
                         "timeout": timeout, **{k: v[k] for k in
                         ("verdict", "runner_started", "target_started",
                          "runner_exit_code", "target_exit_code",
                          "runner_error_kind", "sanitizer_detected")}})
            return v
        runner_started = RUNNER_SENTINEL in out
        m = _TARGET_EXIT_RE.search(out)
        target_started = m is not None
        target_exit = int(m.group(1)) if m else None
        verdict = classify_exec(runner_started, target_exit, out)
        sanitizer = bool(FATAL_RE.search(out) or NONFATAL_RE.search(out))
        kind = (None if runner_started else runner_error_kind(out))
        if runner_started and not target_started:
            kind = "target_incomplete"
        v = {"schema": 2, "verdict": verdict,
             "runner_started": runner_started, "target_started": target_started,
             "runner_exit_code": rc, "target_exit_code": target_exit,
             "runner_error_kind": kind, "sanitizer_detected": sanitizer,
             "rc": rc, "sig": signature(out), "tail": out[-1500:]}
        _oracle_log({"tag": tag or "run_side", "side": info[0].name,
                     "target": info[1], "files": len(mounted), "perturb": perturb,
                     "timeout": timeout, **{k: v[k] for k in
                     ("verdict", "runner_started", "target_started",
                      "runner_exit_code", "target_exit_code",
                      "runner_error_kind", "sanitizer_detected")}})
        return v
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_fix_with_retry(info, files):
    """Disabled 2026-10-09 (D4 strip): fix-side local runs violate the stage3 contract."""
    return {"verdict": "FIX_SIDE_BLOCKED", "rc": -1, "sig": "", "tail": "disabled (D4 strip 10-09)"}

def gate_count(ws: str = None) -> int:
    """Disabled 2026-10-09 (D4 strip): always reports the gate as exhausted."""
    return DIFF_MAX

def gate_take(ws: str = None) -> bool:
    """Disabled 2026-10-09 (D4 strip): never grants a fix-side slot."""
    return False

