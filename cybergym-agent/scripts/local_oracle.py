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
import time
import uuid

try:
    import fcntl
except ImportError:          # non-POSIX (unit tests on Windows)
    fcntl = None

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
                    echo is the LAST match. The value is TRUSTED ONLY when
                    the docker CLI itself exited 0 (bash provably reached
                    its final echo).  runner_rc None (unknown runner state)
                    or non-zero means the parse could be a spoof ->
                    target_exit None -> ORACLE_ERROR.  (r14 review: the
                    former best-effort rc=None branch is removed.)"""
    first = stdout_text.splitlines()[0].strip() if stdout_text else ""
    runner_started = first == RUNNER_SENTINEL
    matches = _TARGET_EXIT_LINE_RE.findall(stdout_text or "")
    target_exit = None
    if matches and runner_rc == 0:
        target_exit = int(matches[-1])          # last full-line match wins
    return {"runner_started": runner_started,
            "target_exit": target_exit,
            "exit_marker_seen": bool(matches)}


def classify_exec(runner_started: bool, target_exit, out: str,
                  runner_rc) -> str:
    """State-first verdict; unknown states NEVER fall through to CLEAN.

    r14 A2: deadline (124) vs external SIGKILL (137) are separated;
    libFuzzer timeout text is TIMEOUT evidence, OOM text is recorded
    separately; sanitizer TEXT alone cannot produce FATAL without a
    non-zero target exit.

    r14-review: runner_rc is REQUIRED and must be 0 -- an unproven runner
    completion never yields a classified verdict.  NONFATAL likewise
    requires either the benchmark exit code 77 or sanitizer text PLUS a
    non-zero target exit: a target printing "MemorySanitizer" and exiting
    0 is CLEAN (text is not crash evidence)."""
    if not runner_started:
        return "ORACLE_ERROR"
    if target_exit is None:
        return "ORACLE_ERROR"           # no trusted exit marker
    if runner_rc != 0:
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
    if target_exit == 77 or (NONFATAL_RE.search(out)
                             and target_exit != 0):
        return "NONFATAL"
    if target_exit == 0:
        return "CLEAN"
    return "UNKNOWN"


_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{4,}")


# ---- r14-review C3/C4: agent-supplied identifiers never reach a shell -----
# Allowlist grammars (NOT denylists): anything that does not match is
# refused before command construction.

_FOCUS_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*((::|\.)[A-Za-z_][A-Za-z0-9_]*)*$")
_DICT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# gdb breakpoint locations: func | file:line | *addr (+ thread modifiers)
_GDB_LOC_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_:.]*|[*]?[A-Za-z0-9_./+-]+:\d+|\*0x[0-9a-fA-F]+)"
    r"(/[0-9]+)?$")


def focus_ok(sym: str) -> bool:
    """--focus symbol for libFuzzer -focus_function."""
    return bool(sym) and len(sym) <= 200 and bool(_FOCUS_RE.match(sym))


def dict_name_ok(name: str) -> bool:
    """dictionary FILENAME (basename only) mounted into the container."""
    return bool(name) and bool(_DICT_NAME_RE.match(name))


def gdb_loc_ok(loc: str) -> bool:
    """gdb breakpoint location: func, file:line, or *address.
    Rejects quotes/semicolons/newlines/substitution/unicode by grammar."""
    return bool(loc) and len(loc) <= 200 and bool(_GDB_LOC_RE.match(loc))


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

    r14 D2 + r14-review: ONE os.write per line under an exclusive flock
    (where available) so concurrent writers cannot interleave a JSON line;
    short-write remainder is flushed inside the same lock. Telemetry is
    best-effort and never changes the verdict path."""
    try:
        path = os.environ.get("ORACLE_LOG", ".oracle.log")
        line = (json.dumps({"ts": datetime.datetime.utcnow().isoformat() + "Z",
                            **rec}, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except OSError:
                    pass
            n = os.write(fd, line)          # single write for the whole line
            while 0 < n < len(line):        # rare short write: finish in-lock
                line = line[n:]
                n = os.write(fd, line)
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)
    except Exception:
        pass


MAX_INPUT_BYTES = 8 * 1024 * 1024


def _copy_input(src, dst):
    """Open-then-copy (r14 B1 + r14-review): returns (ok, reason).

      * O_NOFOLLOW refuses symlinked inputs (open-then-stat means the fd
        we validate IS the fd we read);
      * non-regular files (fifo/dir/device) are refused at fstat time;
      * a per-file size cap is enforced at fstat time AND during the read
        loop (race-safe against post-open growth);
      * a short/copy error refuses the file -- never a silent partial;
      * O_NONBLOCK so a FIFO (or other weird node) cannot block open()
        BEFORE the fstat type check (r15: availability edge)."""
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    try:
        fd = os.open(str(src), flags)
    except OSError as exc:
        return False, f"open_failed:{type(exc).__name__}"
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            return False, "not_regular_file"
        if st.st_size > MAX_INPUT_BYTES:
            return False, f"too_large:{st.st_size}"
        total = 0
        with open(dst, "wb") as w:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_INPUT_BYTES:      # grew after open
                    return False, f"grew_past_cap:{total}"
                w.write(chunk)
        return True, ""
    except OSError as exc:
        return False, f"io_error:{type(exc).__name__}"
    finally:
        os.close(fd)


def _container_gone(cname, tries=3, delay=1.0):
    """Prove the container is really gone (r14-review B2; r16 battery fix).

    Matches by container ID **or** name -- `--filter name=` alone never
    matches a bare ID, which made kill-by-ID verification a FALSE
    positive (the filter simply never matched).  Returns True (provably
    absent) / False (still listed) / None (inspection itself failed ->
    inspection_incomplete).  Absence of evidence is not cleanup proof."""
    target = (cname or "").strip()
    if not target:
        return None
    for _ in range(tries):
        try:
            r = subprocess.run(
                ["docker", "ps", "-aq", "--format", "{{.ID}} {{.Names}}"],
                capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if r.returncode != 0:
            return None
        found = False
        for line in (r.stdout or "").splitlines():
            cid, _, name = line.partition(" ")
            if cid == target or name == target:
                found = True
                break
        if not found:
            return True
        time.sleep(delay)
    return False


def _kill_and_verify(cname):
    """docker kill + verified disappearance -> status string."""
    try:
        kr = subprocess.run(["docker", "kill", cname],
                            capture_output=True, timeout=20)
        kill_rc = kr.returncode
    except (subprocess.TimeoutExpired, OSError):
        return "cleanup_unverified", None
    gone = _container_gone(cname)
    if gone is True:
        return "cleanup_verified", kill_rc
    if gone is None:
        return "inspection_incomplete", kill_rc
    # still alive: one more kill attempt, then report honestly
    try:
        subprocess.run(["docker", "kill", cname],
                       capture_output=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        pass
    gone2 = _container_gone(cname, tries=2)
    if gone2 is True:
        return "cleanup_verified", kill_rc
    if gone2 is None:
        return "inspection_incomplete", kill_rc
    return "cleanup_failed", kill_rc


def _post_abnormal_cleanup(cname):
    """r15 P0: explicit THREE-WAY decision after an abnormal container rc.

    _container_gone() is tri-state (True/False/None); the former
    `is False` test silently mapped None (inspection itself failed) to
    cleanup_verified.  An unknown inspection result stays
    inspection_incomplete -- NEVER success."""
    gone = _container_gone(cname, tries=1)
    if gone is True:
        return "cleanup_verified", None
    if gone is False:
        return _kill_and_verify(cname)
    return "inspection_incomplete", None


_AGE_RE = re.compile(r"(\d+)\s*(second|minute|hour|day|week)s?")
_AGE_UNITS = {"second": 1 / 60, "minute": 1.0, "hour": 60.0,
              "day": 1440.0, "week": 10080.0, "month": 43200.0,
              "year": 525600.0}


def _age_minutes(running_for: str):
    """r17 P1 fail-closed: returns float minutes, or None when the age
    text is empty/unrecognised.  UNKNOWN IS NEVER TREATED AS OLD -- a
    Docker output-format change must not turn a fresh sibling into a
    sweep target."""
    txt = running_for or ""
    if not txt.strip():
        return None
    m = _AGE_RE.search(txt)
    if m:
        return int(m.group(1)) * _AGE_UNITS[m.group(2)]
    for word, mul in _AGE_UNITS.items():      # "About a minute/an hour"...
        if word in txt:
            return mul
    return None                                # unrecognised -> unknown


def sweep_label(label: str, min_age_min: float = 15.0):
    """r16/r17: converge containers orphaned by SIGKILLed callers.

    Tri-state outcome semantics (r17 P1): a FAILED scan is reported as
    inspection_incomplete and is NEVER an empty "no orphans" list;
    unknown age text is never treated as old (no sweep); only containers
    whose age is PROVABLY >= min_age_min are killed with verified
    disappearance (never a healthy sibling: oracle containers live
    <= timeout+150s)."""
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"label={label}",
             "--format", "{{.ID}}\t{{.RunningFor}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return [{"id": None, "cleanup": "inspection_incomplete"}]
    if r.returncode != 0:
        # r17 P1: a failed scan must not look like "no orphans found"
        return [{"id": None, "cleanup": "inspection_incomplete"}]
    out = []
    for line in (r.stdout or "").splitlines():
        parts = (line or "").split("\t")
        if len(parts) < 3:
            continue
        cid, age_txt, status = parts[0], parts[1], parts[2]
        if "Exited" in status:
            continue                      # --rm leftovers are gone anyway
        age = _age_minutes(age_txt)
        if age is None:
            # r17 P1: unrecognised/empty age -> fail-closed, never sweep
            out.append({"id": cid, "age": age_txt,
                        "cleanup": "inspection_incomplete"})
            continue
        if age < min_age_min:
            continue                      # possibly an ACTIVE sibling
        cs, _ = _kill_and_verify(cid)
        out.append({"id": cid, "age": age_txt, "cleanup": cs})
    return out


def sweep_preflight(label: str):
    """r18 P1: startup gate -- a tool may only launch its own container
    when the sweep either found nothing to clean or PROVABLY cleaned
    everything.  Young siblings (age below threshold) never appear in
    the results and never block startup; unverified/incomplete/failed
    cleanups DO.  Returns (ok, entries)."""
    entries = sweep_label(label)
    if not entries:
        return True, []
    bad = [e for e in entries
           if e.get("cleanup") != "cleanup_verified"]
    return (not bad), entries


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
        mounted, input_errors = [], []
        for i, f in enumerate(files):
            dst = tmp / f"in{i}"
            ok, reason = _copy_input(f, dst)
            if ok:
                mounted.append(f"/poc/in{i}")
            else:
                input_errors.append({"file": str(f), "reason": reason})
        if input_errors:
            # r14-review B1: input-set integrity -- a partially-copied set
            # would silently change WHAT is executed; refuse instead.
            _oracle_log({"tag": tag or "run_side", "side": str(bin_dir),
                         "target": target, "files": len(files),
                         "perturb": perturb, "timeout": timeout,
                         "verdict": "INPUT_ERROR",
                         "runner_error_kind": "bad_inputs",
                         "input_errors": input_errors[:8]})
            return {"schema": 2, "verdict": "INPUT_ERROR", "rc": -1,
                    "sig": "",
                    "tail": "input set incomplete: "
                            + "; ".join(f"{e['file']}: {e['reason']}"
                                        for e in input_errors[:8])}
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
        sw_ok, sw_entries = sweep_preflight("cybergym.local_oracle=1")
        if sw_entries:                          # audit what the sweep saw
            _oracle_log({"tag": "sweep", "label": "cybergym.local_oracle=1",
                         "swept": sw_entries})
        if not sw_ok:
            # r18 P1: a failed/unverified sweep is NOT "no orphans" -- the
            # tool refuses to start in an unverifiable lifecycle state
            _oracle_log({"tag": tag or "run_side", "side": str(bin_dir),
                         "target": target, "files": len(mounted),
                         "timeout": timeout, "verdict": "ORACLE_ERROR",
                         "runner_error_kind": "sweep_preflight_failed",
                         "swept": sw_entries})
            return {"schema": 2, "verdict": "ORACLE_ERROR",
                    "runner_started": None, "target_started": None,
                    "runner_exit_code": None, "target_exit_code": None,
                    "runner_error_kind": "sweep_preflight_failed",
                    "sanitizer_detected": False, "sig": "",
                    "tail": "sweep preflight failed (unverified/incomplete "
                            "cleanup of prior orphans): "
                            + json.dumps(sw_entries)[:300]}
        cmd = ["docker", "run", "--rm", "--name", cname,
               "--label", "cybergym.local_oracle=1",
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
            # r14-review B2: kill AND prove it died; an unchecked kill is
            # not cleanup evidence.
            cleanup, kill_rc = _kill_and_verify(cname)
            v = {"schema": 2, "verdict": "ORACLE_ERROR",
                 "runner_started": None, "target_started": None,
                 "runner_exit_code": None, "target_exit_code": None,
                 "runner_error_kind": "runner_hang", "sanitizer_detected": False,
                 "phase": "outer_deadline", "prepare_s": t_prepare,
                 "cleanup": cleanup, "kill_rc": kill_rc,
                 "sig": "", "tail": f"container unresponsive (outer kill, "
                                    f"cleanup={cleanup})",
                 "killed_name": cname}
            _oracle_log({"tag": tag or "run_side", "side": str(bin_dir),
                         "target": target, "files": len(mounted),
                         "perturb": perturb, "timeout": timeout,
                         **{k: v[k] for k in
                            ("verdict", "runner_started", "target_started",
                             "runner_error_kind", "sanitizer_detected",
                             "cleanup")}})
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
        except BaseException:
            # r16: caller interrupt (KeyboardInterrupt/SIGTERM...) must not
            # orphan the container; verified cleanup, then re-raise.
            cleanup, _krc = _kill_and_verify(cname)
            _oracle_log({"tag": tag or "run_side", "side": str(bin_dir),
                         "target": target, "files": len(mounted),
                         "perturb": perturb, "timeout": timeout,
                         "verdict": "ORACLE_ERROR",
                         "runner_error_kind": "caller_interrupt",
                         "cleanup": cleanup})
            raise
        t2 = datetime.datetime.utcnow()
        # r14-review B2 + r15 P0: abnormal container exit needs an
        # independent resource-state check, with an explicit THREE-WAY
        # tri-state decision (unknown inspection != verified cleanup).
        cleanup = None
        kill_rc = None
        if rc != 0:
            cleanup, kill_rc = _post_abnormal_cleanup(cname)
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
             "cleanup": cleanup,
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
