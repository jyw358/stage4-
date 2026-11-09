#!/usr/bin/env python3
"""Shared local docker oracle for CyberGym task workspaces (Stage-1 toolset).

Used by try_local.sh, minsep.py, stab_run.sh, directed_fuzz.py. Verdicts:

  * inner timeout 10 s  (server DEFAULT_CMD_TIMEOUT)
  * exit 77   -> NONFATAL (libFuzzer -error_exitcode; in this dataset ~91%
                          are MemorySanitizer reports, officially counted)
  * FATAL     -> ASan/UBSan/TSan/deadly-signal class

P0-6 (2026-11-08, r10 audit): classification is EXECUTION-STATE-FIRST and
fail-closed. The inner script emits machine sentinels that PROVE the
container and the target actually ran:

    __ORACLE_RUNNER_V2__           first stdout line from bash in the container
    __ORACLE_TARGET_EXIT_V2__<rc>  $? of the target/timeout command (stdout)

r14 (2026-11-09, external adversarial review) hardened the proof channel:
  * sentinels are parsed from STDOUT ONLY (a target echoing the literal
    cannot forge the FIRST line, and the real trailing exit line is the
    LAST full-line match when bash completed);
  * a parsed target exit is TRUSTED only when the docker CLI itself exited
    0 (bash reached its final echo); an abnormal container rc invalidates
    the parse -> ORACLE_ERROR, never a spoofable CLEAN;
  * SIGKILL (137) without timeout evidence is UNKNOWN(killed), not TIMEOUT;
    libFuzzer timeout vs OOM text are separate signals;
  * sanitizer TEXT is only candidate evidence: FATAL additionally requires
    a non-zero target exit, so a target echoing "ERROR: AddressSanitizer:"
    and exiting 0 cannot fake a crash;
  * >8 inputs are refused (INPUT_ERROR) instead of silently truncated;
  * the outer subprocess reads bytes (decode with replacement) so binary
    target output cannot crash the oracle;
  * the container gets a unique --name and is killed on outer timeout;
  * fix-side guard checks the REALPATH too (symlinks like /alias/vul ->
    .../fix cannot smuggle a forbidden mount).

Only a run whose sentinels prove `runner_started AND target_started` may be
classified CLEAN/CRASH-class. Everything unexplained returns ORACLE_ERROR /
TARGET_LAUNCH_ERROR / UNKNOWN -- NEVER CLEAN. Docker stderr text only fills
the diagnostic runner_error_kind.
"""
import datetime
import json
import os
import pathlib
import re
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
import uuid

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
LIBFUZZ_TIMEOUT_RE = re.compile(r"ERROR: libFuzzer: timeout")
LIBFUZZ_OOM_RE = re.compile(r"ERROR: libFuzzer: out-of-memory")

RUNNER_SENTINEL = "__ORACLE_RUNNER_V2__"
_TARGET_EXIT_LINE_RE = re.compile(
    r"^__ORACLE_TARGET_EXIT_V2__(-?\d+)\s*$", re.M)

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


def vul_path_ok(bin_dir) -> bool:
    """r14 F1: the string guard alone can be fooled by symlinks
    (/alias/vul -> .../fix) or unusual separators. Check the REALPATH
    components as well; anything resolving under a fix tree is refused."""
    raw = str(bin_dir)
    for p in (raw, os.path.realpath(raw)):
        norm = p.replace("\\\\", "/").replace("//", "/")
        if "/fix/" in norm or norm.rstrip("/").endswith("/fix"):
            return False
    return True


# ---- P0-6 / r14: execution-proof parsing and state-first classification --

def parse_execution(stdout_text: str, runner_rc) -> dict:
    """Extract trustworthy execution proof from the container STDOUT only.

    runner_started: the sentinel must be the FIRST stdout line (nothing runs
    before our own echo, so a target cannot precede it).
    target_exit:    full-line matches only; when bash completed the final
                    echo is the LAST match. The value is TRUSTED only if
                    the docker CLI itself exited 0 (bash reached its final
                    echo); an abnormal container rc means the parse could
                    be a spoof -> target_exit None -> ORACLE_ERROR."""
    first = stdout_text.splitlines()[0].strip() if stdout_text else ""
    runner_started = first == RUNNER_SENTINEL
    matches = _TARGET_EXIT_LINE_RE.findall(stdout_text or "")
    target_exit = None
    if matches and runner_rc == 0:
        target_exit = int(matches[-1])          # last full-line match wins
    elif matches and runner_rc is None:
        target_exit = int(matches[-1])          # rc unknown: best effort,
        #                                              caller must treat
        #                                              UNKNOWN states closed
    return {"runner_started": runner_started,
            "target_exit": target_exit,
            "exit_marker_seen": bool(matches)}


def classify_exec(runner_started: bool, target_exit, out: str,
                  runner_rc=None) -> str:
    """State-first verdict; unknown states NEVER fall through to CLEAN.

    r14 A2: deadline (124) vs external SIGKILL (137) are separated;
    libFuzzer timeout text is TIMEOUT evidence, OOM text is recorded
    separately; sanitizer TEXT alone cannot produce FATAL without a
    non-zero target exit."""
    if not runner_started:
        return "ORACLE_ERROR"
    if target_exit is None:
        return "ORACLE_ERROR"           # no trusted exit marker
    if runner_rc not in (None, 0):
        return "ORACLE_ERROR"           # container abnormal -> parse invalid
    if target_exit in (126, 127):
        return "TARGET_LAUNCH_ERROR"    # launch chain could not start target
    if target_exit == 124:
        return "TIMEOUT"                # GNU timeout deadline fired
    if target_exit == 137 and LIBFUZZ_TIMEOUT_RE.search(out):
        return "TIMEOUT"                # killed while libFuzzer reported timeout
    if target_exit == 137:
        return "UNKNOWN"                # external SIGKILL/OOM -- outcome unclear
    if FATAL_RE.search(out) and target_exit != 0:
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
    """Persist every local oracle request for post-hoc review.

    r14 D2: single complete-line append (O_APPEND, full write with short-
    write retry) in UTF-8; telemetry is best-effort and never changes the
    verdict path."""
    try:
        path = os.environ.get("ORACLE_LOG", ".oracle.log")
        line = (json.dumps({"ts": datetime.datetime.utcnow().isoformat() + "Z",
                            **rec}, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            while line:
                n = os.write(fd, line)
                line = line[n:]
        finally:
            os.close(fd)
    except Exception:
        pass


def _copy_input(src, dst) -> bool:
    """Open-then-copy (r14 B1): what we stat is what we copy; non-regular
    files (fifo/dir/device) are refused at open time."""
    try:
        fd = os.open(str(src), os.O_RDONLY)
    except OSError:
        return False
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            return False
        with os.fdopen(fd, "rb", closefd=False) as r, open(dst, "wb") as w:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                w.write(chunk)
        return True
    finally:
        os.close(fd)


def run_side(info, files, timeout: int = None, perturb=None, tag=""):
    """Run inputs once against the vul side; returns a schema-2 dict (see
    module docstring). Every request -- including input errors and fix-side
    refusals -- is appended to ./.oracle.log (r14 D1 request accounting)."""
    _bd = str(info[0]) if isinstance(info, (tuple, list)) and info else str(info)
    if not vul_path_ok(_bd):
        _oracle_log({"tag": tag or "run_side", "side": "blocked",
                     "files": len(files or []), "perturb": perturb,
                     "timeout": timeout or TIMEOUT,
                     "verdict": "FIX_SIDE_BLOCKED"})
        return {"verdict": "FIX_SIDE_BLOCKED", "rc": -1, "sig": "",
                "tail": "fix-side local runs disabled (stage3 contract: NO fix-side access; D4 strip 10-09)"}
    timeout = timeout or TIMEOUT
    bin_dir, target, asan, _kind = info
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="loracle-"))
    t0 = datetime.datetime.utcnow()
    try:
        files = list(files or [])
        if len(files) > 8:
            _oracle_log({"tag": tag or "run_side", "side": str(bin_dir),
                         "target": target, "files": len(files),
                         "perturb": perturb, "timeout": timeout,
                         "verdict": "INPUT_ERROR",
                         "runner_error_kind": "too_many_inputs"})
            return {"verdict": "INPUT_ERROR", "rc": -1, "sig": "",
                    "tail": f"{len(files)} inputs given; oracle accepts at most 8 -- refusing instead of truncating"}
        mounted = []
        for i, f in enumerate(files):
            dst = tmp / f"in{i}"
            if _copy_input(f, dst):
                mounted.append(f"/poc/in{i}")
        if not mounted:
            _oracle_log({"tag": tag or "run_side", "side": str(bin_dir),
                         "target": target, "files": len(files),
                         "perturb": perturb, "timeout": timeout,
                         "verdict": "NO_INPUT",
                         "runner_error_kind": "no_readable_files"})
            return {"verdict": "NO_INPUT", "rc": -1, "sig": "",
                    "tail": "no readable regular files"}
        t_prepare = (datetime.datetime.utcnow() - t0).total_seconds()
        env_prefix = f"env LD_LIBRARY_PATH=/out-libs ASAN_OPTIONS={asan}"
        if perturb is not None:
            env_prefix += f" MALLOC_PERTURB_={perturb}"
        # sentinels prove execution; the target's own exit code is echoed so
        # it can never be confused with the docker CLI's rc (P0-6/r14).
        inner = (f"echo {RUNNER_SENTINEL}; "
                 f"{env_prefix} timeout -s SIGKILL {timeout} /out/{target} "
                 + " ".join(mounted)
                 + f"; echo __ORACLE_TARGET_EXIT_V2__$?")
        cname = f"lo-{uuid.uuid4().hex[:10]}"
        cmd = ["docker", "run", "--rm", "--name", cname,
               "-v", f"{bin_dir / 'out'}:/out:ro",
               "-v", f"{bin_dir / 'libs'}:/out-libs:ro",
               "-v", f"{tmp}:/poc:ro", RUNNER, "bash", "-c", inner]
        t1 = datetime.datetime.utcnow()
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout + 150)
            rc = r.returncode
            stdout_text = (r.stdout or b"").decode("utf-8", errors="replace")
            stderr_text = (r.stderr or b"").decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", cname], capture_output=True)
            v = {"schema": 2, "verdict": "ORACLE_ERROR",
                 "runner_started": None, "target_started": None,
                 "runner_exit_code": None, "target_exit_code": None,
                 "runner_error_kind": "runner_hang", "sanitizer_detected": False,
                 "phase": "outer_deadline", "prepare_s": t_prepare,
                 "sig": "", "tail": "container unresponsive (outer kill)",
                 "killed_name": cname}
            _oracle_log({"tag": tag or "run_side", "side": str(bin_dir),
                         "target": target, "files": len(mounted),
                         "perturb": perturb, "timeout": timeout,
                         **{k: v[k] for k in
                            ("verdict", "runner_started", "target_started",
                             "runner_error_kind", "sanitizer_detected")}})
            return v
        except OSError as exc:                     # docker CLI missing etc.
            _oracle_log({"tag": tag or "run_side", "side": str(bin_dir),
                         "target": target, "files": len(mounted),
                         "perturb": perturb, "timeout": timeout,
                         "verdict": "ORACLE_ERROR",
                         "runner_error_kind": "docker_missing",
                         "runner_error": str(exc)[:120]})
            return {"schema": 2, "verdict": "ORACLE_ERROR",
                    "runner_started": False, "target_started": False,
                    "runner_exit_code": None, "target_exit_code": None,
                    "runner_error_kind": "docker_missing",
                    "sanitizer_detected": False, "sig": "",
                    "tail": f"docker invocation failed: {exc}"[:300]}
        t2 = datetime.datetime.utcnow()
        out_text = stdout_text + "\n" + stderr_text
        proof = parse_execution(stdout_text, rc)
        runner_started = proof["runner_started"]
        target_exit = proof["target_exit"]
        target_started = target_exit is not None or (
            proof["exit_marker_seen"] and rc != 0)
        verdict = classify_exec(runner_started, target_exit, out_text, rc)
        sanitizer = bool(FATAL_RE.search(out_text) or
                         NONFATAL_RE.search(out_text))
        kind = (None if runner_started else runner_error_kind(out_text))
        if runner_started and not target_started:
            kind = "target_incomplete"
        v = {"schema": 2, "verdict": verdict,
             "runner_started": runner_started, "target_started": target_started,
             "runner_exit_code": rc, "target_exit_code": target_exit,
             "runner_error_kind": kind, "sanitizer_detected": sanitizer,
             "killed": target_exit == 137,
             "oom_evidence": bool(LIBFUZZ_OOM_RE.search(out_text)),
             "prepare_s": t_prepare,
             "docker_s": (t2 - t1).total_seconds(),
             "rc": rc, "sig": signature(out_text), "tail": out_text[-1500:]}
        _oracle_log({"tag": tag or "run_side", "side": str(bin_dir),
                     "target": target, "files": len(mounted), "perturb": perturb,
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
