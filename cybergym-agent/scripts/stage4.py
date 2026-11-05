#!/usr/bin/env python3
"""stage4.py -- clean-run queue driver (stage-4).

One authoritative per-task path: this driver prepares the task dir, calls
run_agent_task.sh (fresh workspace + ledger + row), then scores the whole run
with score_all.py (final-only) and runs verify_stage4.py.

  python3 stage4.py --queue /data/cybergym/stage4/queue.json \
      --out /data/cybergym/stage4/r_engineer --workers 3 [--limit 50]

Resume: re-running with the same --out reuses <out>/run_id and skips tasks
whose row is execution-terminal for that run (a row that is terminal but
unscored goes to the scoring phase, it is never re-RUN: B9).  --retry-failed
re-runs tasks whose official_success is False (or that ended in
worker_error/gen_fail).

R2 fixes:
  B7  backfill/score/verify exit codes propagate: any failed phase makes the
      whole round exit non-zero; a missing phase script is a failure, not a
      warning.
  B9  terminal states split into execution vs scoring; scoring-incomplete
      rows are listed in batch_summary and must be resolved by scoring,
      never reported as completed tasks.
  B12 each task runs in its own session/process group; on timeout the whole
      tree is terminated (SIGTERM, then SIGKILL) before the row is written,
      so a timed-out attempt cannot overlap with a retry.
  MAJOR run-level exclusive lock on <out>/.run.lock; atomic run_meta.json /
      run_id / rows_index.json / batch_summary.json writes; archive names get
      a UUID suffix and archiving happens under the results lock; task-id ->
      file-name collisions are refused up front; the filesystem under --out
      must support local locking for multi-worker runs.

R3 fixes (dedicated-user review):
  T1  trust is established by the ROOT-SIDE launcher (root_code/stage4.sh +
      root_code/assert_code_tree.py, deployed outside the code tree) BEFORE
      this process starts; this module keeps only a narrow defensive lstat
      check for the exact files it imports (a check inside the tree cannot
      establish trust by itself).
  ID  three identities: root (driver/scoring/promotion), S4_PREP_USER
      (default naco: gen_task + task-dir prep only), and ONE NUMERIC UID PER
      TASK for the agent (S4_AGENT_UID_BASE pool, never reused within a run)
      -- a single shared agent UID cannot isolate same-batch workspaces.
  CP  per-task control-plane binding under <out>/control/<task>/ (root:root
      0700, O_NOFOLLOW writes): run_id, expected agent_id and submit.sh
      sha256.  Reuse requires the binding to still match; the workspace
      marker is demoted to a legacy hint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX only; the driver never runs on Windows in production
    import pwd
except ImportError:  # pragma: no cover
    pwd = None

AG = os.environ.get("S4_AGENT_HOME", "/opt/cybergym-agent")
REPO = os.environ.get("S4_REPO", "/home/naco/work/cybergym")
DATA = os.environ.get("S4_DATA", "/data/cybergym/cybergym_data/data")
SERVER = os.environ.get("S4_SERVER", "http://172.17.0.1:8666")
# prep identity (gen_task + task-dir writes): deliberately SEPARATE from the
# agent identity -- an agent that shares the prep account could read other
# tasks' prepared inputs (dedicated-user review, question 4).
PREP_USER = os.environ.get("S4_PREP_USER", "naco")
# per-task agent UIDs are allocated from this pool (R3: one UID per task)
AGENT_UID_BASE = int(os.environ.get("S4_AGENT_UID_BASE", "30001"))
AGENT_UID_SPAN = int(os.environ.get("S4_AGENT_UID_SPAN", "4096"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _defensive_tree_check() -> None:
    """Defense-in-depth ONLY: lstat the exact import surface of this module
    before importing it.  The authoritative audit runs BEFORE python starts
    (root_code/assert_code_tree.py, deployed outside this tree)."""
    if os.name != "posix" or (pwd and hasattr(os, "geteuid")
                              and os.geteuid() != 0):
        return
    import stat as _st
    base = Path(__file__).resolve().parents[1]
    targets = [base, base / "plugins", base / "plugins" / "__init__.py",
               base / "plugins" / "results_store.py",
               base / "plugins" / "locks.py"]
    for t in targets:
        try:
            st = os.lstat(t)
        except OSError:
            continue
        if _st.S_ISLNK(st.st_mode):
            raise SystemExit(f"REFUSE: {t} is a symlink (untrusted tree)")
        if st.st_mode & (_st.S_IWGRP | _st.S_IWOTH):
            raise SystemExit(f"REFUSE: {t} is group/other-writable "
                             "(T1: root-run code must be go-w free)")
        if st.st_uid != 0 and os.geteuid() != st.st_uid:
            raise SystemExit(f"REFUSE: {t} owned by uid={st.st_uid}, not root")


_defensive_tree_check()

from plugins import results_store  # noqa: E402
from plugins.locks import FileLock, LockBusy, assert_local_lockable  # noqa: E402

EXECUTION_FAILURE_STATUSES = ("worker_error", "gen_fail")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def euid() -> int:
    return os.geteuid() if hasattr(os, "geteuid") else -1


def atomic_write(path: Path, text: str) -> None:
    """Same-directory atomic replace for driver-owned metadata files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def chown_prep(path) -> None:
    """gen_task runs as PREP_USER (default naco): the dirs it writes into
    must be owned by that account.  R3: fail-closed -- a silent chown failure
    would leave prep dirs unusable or, worse, mixed-ownership; review noted
    the old swallow-everything version must not survive the migration."""
    if pwd is None or euid() != 0:
        return
    try:
        pw = pwd.getpwnam(PREP_USER)
    except KeyError:
        raise SystemExit(f"prep user {PREP_USER!r} does not exist "
                         "(S4_PREP_USER; see STAGE4_DEPLOY.md)")
    try:
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except OSError as e:
        raise SystemExit(f"chown {path} -> {PREP_USER} failed: {e}")


# ---- R3: per-task control-plane binding (<out>/control/<task>/) -----------
CONTROL_KEYS = ("run_id", "agent_id", "submit_sha256", "task_sha256", "agent_uid")


def control_root(out: Path) -> Path:
    return Path(out) / "control"


def init_control_plane(out: Path) -> None:
    """<out>/control is root:root 0700: the ONLY place the run/task identity
    binding lives.  Task dirs and workspaces are owned by untrusted accounts
    and treated as replaceable input copies.

    R3-M1: the whole ancestor chain of --out must be root-owned and free of
    group/other write, otherwise a parent directory can be swapped under us
    (chmod on the leaf proves nothing about the chain above it)."""
    ok, why = ancestors_trusted(out)
    if not ok:
        raise SystemExit(f"refusing --out {out}: {why} "
                         "(control plane must live under a root-owned path)")
    d = control_root(out)
    d.mkdir(parents=True, exist_ok=True)
    if os.name == "posix" and euid() == 0:
        os.chown(d, 0, 0)
        os.chmod(d, 0o700)


def ancestors_trusted(path: Path, root_uid: int = 0) -> tuple:
    """(ok, reason): every existing ancestor of ``path`` must be owned by
    root, not group/other-writable, and traversable by others (so the agent
    can reach its OWN workspace/dir).  Pure lstat walk; POSIX only.

    Behind this: if `<out>` or its parents are writable by the prep user or a
    task uid, that account can rename/replace `<out>/control` wholesale, and
    binding files inside a perfectly clean leaf directory mean nothing."""
    if os.name != "posix" or euid() != 0:
        return True, ""
    import stat as _st
    cur = os.path.abspath(path)
    while True:
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            cur = os.path.dirname(cur)
            if cur == "/":
                return True, ""
            continue
        except OSError as e:
            return False, f"lstat {cur} failed: {e}"
        if _st.S_ISLNK(st.st_mode):
            return False, f"{cur} is a symlink"
        if st.st_uid != root_uid:
            return False, f"{cur} owned by uid={st.st_uid}, not root"
        if st.st_mode & (_st.S_IWGRP | _st.S_IWOTH):
            return False, f"{cur} is group/other-writable"
        # R3.3 (real-task smoke): a root-owned 0700 ancestor (e.g. /root) also
        # blocks the run -- the PREP identity writes <out>/tasks/<task> and the
        # per-task uid enters <out>-ws/<task>, and neither can traverse a
        # root-only directory.  Fail here with a clear message instead of a
        # cryptic gen_task PermissionError.
        if not st.st_mode & _st.S_IXOTH:
            return False, (f"{cur} is not other-traversable (mode "
                           f"{oct(st.st_mode & 0o777)}): prep/agent uids must "
                           "be able to reach their own subdirectories")
        if cur == "/":
            return True, ""
        nxt = os.path.dirname(cur)
        if nxt == cur:
            return True, ""
        cur = nxt


def control_dir_trusted(d: Path) -> tuple:
    """(ok, reason): the control dir itself must be a real, root-owned,
    go-w-free directory (review Q5: an agent-writable parent could swap the
    files inside it) AND its ancestor chain must be root-owned (R3-M1)."""
    if os.name != "posix" or euid() != 0:
        return True, ""
    import stat as _st
    try:
        st = os.lstat(d)
    except OSError as e:
        return False, f"lstat {d} failed: {e}"
    if _st.S_ISLNK(st.st_mode):
        return False, f"{d} is a symlink"
    if st.st_uid != 0:
        return False, f"{d} owned by uid={st.st_uid}, not root"
    if st.st_mode & (_st.S_IWGRP | _st.S_IWOTH):
        return False, f"{d} is group/other-writable"
    return True, ""


def control_write(out: Path, name: str, key: str, value: str) -> None:
    """Write one control-plane field with O_NOFOLLOW + atomic replace, so a
    pre-planted symlink can never redirect a root write (review Q5/B)."""
    d = control_root(out) / name
    d.mkdir(parents=True, exist_ok=True)
    ok, why = control_dir_trusted(d)
    if not ok:
        raise SystemExit(f"control plane for {name} is not trustworthy: {why}")
    # R3.3: the chain check starts ABOVE <out>/control: the control root is
    # deliberately root-only 0700, so it is not other-traversable and must not
    # be judged by the prep/agent traversal rule.
    ok, why = ancestors_trusted(control_root(out).parent)
    if not ok:
        raise SystemExit(f"control plane for {name} is not trustworthy: {why}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    tmp = d / f".{key}.tmp.{os.getpid()}"
    fd = os.open(tmp, flags, 0o600)
    try:
        os.write(fd, (value.rstrip("\n") + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, d / key)   # rename replaces a symlink, never follows it
    if os.name == "posix" and euid() == 0:
        os.chmod(d / key, 0o600)


def control_read(out: Path, name: str) -> dict:
    """All control-plane fields present for a task (missing files -> {})."""
    d = control_root(out) / name
    vals = {}
    for key in CONTROL_KEYS:
        p = d / key
        if p.exists():
            try:
                vals[key] = p.read_text(encoding="utf-8").strip()
            except OSError:
                pass
    return vals


def submit_sha256(tdir: Path) -> str:
    data = (Path(tdir) / "submit.sh").read_bytes()
    return hashlib.sha256(data).hexdigest()


def task_inputs_sha256(tdir: Path) -> str:
    """Digest of the WHOLE prepared input tree (relative path, size, content
    hash), not just submit.sh.

    R3.1 (review MAJOR): binding only submit.sh proved the submission identity
    but said nothing about description.txt / repo archives / any other input
    the agent is handed.  A task dir could be edited between prep and reuse
    and still pass the old binding."""
    root = Path(tdir)
    rows = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if rel == ".stage4_run_id":
                continue          # driver-owned legacy marker, not an input
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                # a symlink is never a legitimate prepared input
                rows.append(f"{rel}\tlink\t{os.readlink(p)}")
                continue
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            rows.append(f"{rel}\t{st.st_size}\t{h}")
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def submit_agent_id(tdir: Path) -> str:
    """Expected agent_id as baked into submit.sh by gen_task."""
    from plugins.cybergym_runner import parse_submit_sh  # noqa: PLC0415
    try:
        return str(parse_submit_sh(str(Path(tdir))).get("agent_id") or "")
    except Exception:
        return ""


def pick_free_uid(start: int, taken: set, exists=None, span: int = AGENT_UID_SPAN) -> int:
    """First uid in [start, start+span) that has no passwd entry and is not
    already assigned within this run (pure; unit-tested)."""
    exists = exists or (lambda u: False)
    for uid in range(int(start), int(start) + int(span)):
        if uid in taken:
            continue
        if exists(uid):
            continue
        return uid
    raise RuntimeError(f"agent uid pool exhausted in [{start}, {start + span})")


UID_REGISTRY = os.environ.get("S4_UID_STATE",
                              "/var/lib/cybergym/stage4/uid_registry.json")


def _registry_path() -> Path:
    return Path(UID_REGISTRY)


def _registry_taken() -> dict:
    """uid(str) -> record: HOST-WIDE, root-only registry of task uids.

    R3-B2: a per-run uid_alloc.json only proves ``never reused within THIS
    run``.  Two runs with different --out could both hand out 30001, and the
    new task could then read files the old (still existing) run left 0700
    under the same uid.  The registry is append-only: a uid, once issued on
    this host, is never issued again, so cross-run isolation holds by
    construction (retired uids simply stay burned)."""
    return _load_json_object(_registry_path(), "uid registry")


def allocate_agent_uid(out: Path, name: str, run_id: str = "") -> int:
    """R3: ONE numeric uid per task, never reused within the run OR across
    runs on this host (global append-only registry, S4_UID_STATE)."""
    rp = _registry_path()
    # Harden the registry's directory ONLY when we create it: S4_UID_STATE may
    # point at an existing shared directory (the R3.2 host negative test keeps
    # it directly under /var/tmp/s4neg), and lock-chmod'ing someone else's
    # directory as root silently breaks every other user of it.  The registry
    # FILE is 0600 root below, which is what actually protects its content.
    created = not rp.parent.exists()
    rp.parent.mkdir(parents=True, exist_ok=True)
    if created and os.name == "posix" and euid() == 0:
        os.chown(rp.parent, 0, 0)
        os.chmod(rp.parent, 0o700)
    lock = rp.with_name(".uid.lock")
    with FileLock(str(lock)):
        registry = _registry_taken()
        taken = {int(k) for k in registry if str(k).isdigit()}
        taken.add(AGENT_UID_BASE - 1)  # keep clear of the pool floor
        # per-run file kept for operator visibility (not the trust anchor).
        # R3.1 MAJOR (review): a corrupt file must NOT be treated as "no
        # allocations yet" -- that could reissue a uid this run already used.
        run_alloc_path = control_root(out) / "uid_alloc.json"
        run_alloc = _load_json_object(run_alloc_path, "per-run uid_alloc")
        taken |= {int(v) for v in run_alloc.values() if str(v).isdigit()}
        if pwd is not None:
            for pw in pwd.getpwall():
                taken.add(pw.pw_uid)
        uid = pick_free_uid(AGENT_UID_BASE, taken,
                            exists=lambda u: _uid_exists(u))
        registry[str(uid)] = {"run_id": run_id, "task": name,
                             "out": str(out), "issued_at": utcnow()}
        atomic_write(rp, json.dumps(registry, indent=1, sort_keys=True))
        if os.name == "posix" and euid() == 0:
            os.chmod(rp, 0o600)
        run_alloc[name] = uid
        atomic_write(run_alloc_path, json.dumps(run_alloc, indent=1, sort_keys=True))
        if os.name == "posix" and euid() == 0:
            os.chmod(run_alloc_path, 0o600)
        return uid


def _load_json_object(path: Path, what: str) -> dict:
    """Read a root-owned JSON object file; missing = {} but CORRUPT = stop.

    R3.1 (review MAJOR): `except JSONDecodeError: pass` silently downgraded a
    damaged allocation file to "empty", so already-issued uids could be handed
    out again.  Allocation state is security state: fail closed."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"{what} {path} is corrupt: {e} (repair as root; "
                         "do NOT delete it -- uids already issued would be "
                         "reissued)")
    if not isinstance(data, dict):
        raise SystemExit(f"{what} {path} is not a JSON object")
    return data


def _uid_exists(uid: int) -> bool:
    if pwd is None:
        return False
    try:
        pwd.getpwuid(uid)
        return True
    except KeyError:
        return False


def harden_task_dir(tdir: Path, uid: int) -> None:
    """R3-B1: after prep, the task dir belongs to THIS task's uid only.

    Review blocker: workspaces were isolated per uid, but the prepared task
    dir stayed 0755 (naco-owned), so uid_A could read task_B's
    description.txt / submit.sh.  The parent task_base is root:root 0711 (traverse
    only, no listing), and every per-task dir becomes 0700 owned by its uid,
    so a task can read its own inputs and nothing else."""
    if os.name != "posix" or euid() != 0:
        return
    for dirpath, dirnames, filenames in os.walk(tdir):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                raise SystemExit(f"REFUSE: {p} is a symlink in the prepared "
                                 "task dir (prep produced an untrusted tree)")
            os.chown(p, uid, uid)
    os.chown(tdir, uid, uid)
    os.chmod(tdir, 0o700)
    # keep the dir chain traversable but not listable for other task uids
    if tdir.parent.exists():
        os.chown(tdir.parent, 0, 0)
        os.chmod(tdir.parent, 0o711)


def validate_reuse(tdir: Path, control: dict, run_id: str) -> tuple:
    """(ok, reason) for reusing a pre-generated task dir: the control-plane
    binding must exist, belong to THIS run, and still match the submit.sh on
    disk (review Q5-C: a marker alone never bound submit.sh's content)."""
    if not control:
        return False, (f"{tdir} has submit.sh but no control-plane binding "
                       "(<out>/control); regenerate the task dir")
    if control.get("run_id") != run_id:
        return False, (f"{tdir} control-plane binding belongs to run "
                       f"{control.get('run_id')!r}, not {run_id!r}")
    want = control.get("submit_sha256")
    if not want:
        return False, "control-plane binding has no submit_sha256"
    have = submit_sha256(tdir)
    if have != want:
        return False, (f"{tdir}/submit.sh changed since prep "
                       f"(sha256 {have} != binding {want}); regenerate")
    want_tree = control.get("task_sha256")
    if not want_tree:
        return False, ("control-plane binding has no task_sha256 (pre-R3.1 "
                       "task dir); regenerate the task dir")
    have_tree = task_inputs_sha256(tdir)
    if have_tree != want_tree:
        return False, (f"{tdir} inputs changed since prep (tree sha256 "
                       f"{have_tree[:12]}... != binding {want_tree[:12]}...); "
                       "regenerate")
    want_agent = control.get("agent_id")
    if want_agent:
        have_agent = submit_agent_id(tdir)
        if have_agent != want_agent:
            return False, (f"{tdir}/submit.sh agent_id {have_agent!r} != "
                           f"binding {want_agent!r}; regenerate")
    return True, ""


def load_queue(path: Path) -> list:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
        if isinstance(data, dict) and "queue" in data:
            return [str(x).strip() for x in data["queue"] if str(x).strip()]
    except json.JSONDecodeError:
        pass
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def queue_digest(queue: list) -> str:
    return hashlib.sha256("\n".join(queue).encode()).hexdigest()[:16]


def prepare_task(tid: str, task_base: Path, out: Path, run_id: str, log) -> tuple:
    """Prepare the task dir.  A pre-generated dir is only reused when the
    ROOT-OWNED control-plane binding says it was created by THIS run AND its
    submit.sh still hashes to the bound value.

    R3: the old .stage4_run_id marker (root-owned file inside a PREP-user-
    owned directory) is NOT a trust anchor -- the prep account can replace
    the directory entry.  It is still written, but only as a legacy hint;
    the binding lives in <out>/control/<task>/ (root:root 0700)."""
    name = tid.replace(":", "_")
    tdir = task_base / name
    marker = tdir / ".stage4_run_id"
    if (tdir / "submit.sh").exists():
        ok, why = validate_reuse(tdir, control_read(out, name), run_id)
        if not ok:
            return False, why
        # legacy marker: refuse an obviously foreign one, ignore absence
        if marker.exists() and marker.read_text(encoding="utf-8").strip() != run_id:
            return False, (f"{marker} belongs to another run; regenerate "
                           "the task dir")
        return True, ""
    tdir.mkdir(parents=True, exist_ok=True)
    chown_prep(tdir)
    if os.name == "posix" and euid() == 0:
        # R3-B1: 0700 owned by the PREP identity while gen_task writes -- no
        # task uid can read a half-prepared dir; harden_task_dir then hands it
        # to the owning task uid (also 0700).
        os.chmod(tdir, 0o700)
    cmd = [
        "sudo", "-u", PREP_USER, "env", f"PYTHONPATH={REPO}/src",
        "/usr/bin/python3.12", "-m", "cybergym.task.gen_task",
        "--task-id", tid, "--out-dir", str(tdir),
        "--data-dir", DATA, "--server", SERVER,
        "--mask-map", f"{REPO}/mask_map.json", "--difficulty", "level1",
    ]
    log(f"[gen] {tid}")
    try:
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                              timeout=600)
    except subprocess.TimeoutExpired:
        return False, "gen_task timeout"
    if not (tdir / "submit.sh").exists():
        tail = (proc.stderr or proc.stdout or "")[-400:].replace("\n", " ")
        return False, f"gen_task failed rc={proc.returncode}: {tail}"

    # bind identity in the control plane FIRST (root-only dir), marker second
    control_write(out, name, "run_id", run_id)
    control_write(out, name, "submit_sha256", submit_sha256(tdir))
    control_write(out, name, "task_sha256", task_inputs_sha256(tdir))
    control_write(out, name, "agent_id", submit_agent_id(tdir))
    try:
        _marker_write(marker, run_id)
    except OSError as e:
        log(f"[warn] marker write failed (binding is in control plane): {e}")
    return True, ""


def _marker_write(marker: Path, run_id: str) -> None:
    """Legacy marker with O_NOFOLLOW + rename (never follows a pre-planted
    symlink); root-owned 0644 inside the prep-owned task dir."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    tmp = marker.with_name(f".{marker.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, flags, 0o644)
    try:
        os.write(fd, (run_id + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, marker)
    if os.name == "posix" and euid() == 0:
        os.chown(marker, 0, 0)
        os.chmod(marker, 0o644)


def _stat_fields(stat_line: str) -> list:
    """Fields of /proc/<pid>/stat after the parenthesised comm (pid, state,
    ppid, pgrp, session, ...).  comm may contain spaces and braces."""
    rparen = stat_line.rfind(")")
    if rparen < 0:
        return []
    return stat_line[rparen + 1:].split()


def session_members(session_id: int, proc_root: str = "/proc") -> list:
    """PIDs still in ``session_id`` (pure enough to unit-test with a fake
    /proc).  R3-M4: SIGKILL on the process group does not reach descendants
    that called setsid(); sweeping the SESSION catches those (it cannot catch
    a descendant that started a brand-new session -- that still needs
    cgroup/systemd scope recycling, documented as a residual)."""
    out = []
    try:
        names = os.listdir(proc_root)
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, name, "stat"), encoding="utf-8") as fh:
                line = fh.read()
        except OSError:
            continue
        f = _stat_fields(line)
        # f[0]=state f[1]=ppid f[2]=pgrp f[3]=session
        if len(f) >= 4 and f[3].isdigit() and int(f[3]) == session_id:
            out.append(int(name))
    return sorted(out)


def sweep_session(session_id: int) -> int:
    """SIGKILL every remaining member of a task session; returns how many
    were still alive.  After this it also re-checks, so the caller can log
    the residual count (0 = the session is confirmed empty)."""
    left = session_members(session_id)
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(0.2)
    return len(session_members(session_id))


def token_members(token: str, proc_root: str = "/proc") -> list:
    """PIDs whose environment still carries ``S4_TASK_TOKEN=<token>``.

    R3.4 (review: setsid escape): a descendant that calls setsid() leaves the
    task's session, so neither the process-group kill nor the session sweep
    reaches it.  The driver exports a per-attempt token into every agent
    process's environment, so a root-side scan of /proc/<pid>/environ finds
    those descendants whichever session/process group they moved to.  A
    process that re-execs with a scrubbed environment still escapes; closing
    that needs cgroups (documented residual)."""
    out = []
    needle = ("S4_TASK_TOKEN=" + token).encode()
    try:
        names = os.listdir(proc_root)
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, name, "environ"), "rb") as fh:
                blob = fh.read()
        except OSError:
            continue
        if needle in blob.split(b"\0"):
            out.append(int(name))
    return sorted(out)


def sweep_token(token: str, rounds: int = 3, proc_root: str = "/proc") -> int:
    """SIGKILL everything still carrying the task token; returns the residual
    count (0 = confirmed empty)."""
    left = token_members(token, proc_root)
    for _ in range(rounds):
        if not left:
            return 0
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.2)
        left = token_members(token, proc_root)
    return len(left)


def kill_task_tree(proc: subprocess.Popen, token: str = "") -> int:
    """B12/R3-M4: terminate the WHOLE process group of a task (the wrapper
    starts a session, so sudo/agcli/timeout descendants are all inside it),
    escalate to SIGKILL after a grace period, then sweep anything left in the
    SESSION and anything that escaped it but still carries the task token
    (R3.4: setsid'd descendants).  Returns the residual member count."""
    if os.name != "posix":
        proc.kill()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass
        return 0
    try:
        pgid = os.getpgid(proc.pid)
        sid = os.getsid(proc.pid)
    except ProcessLookupError:
        return 0
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        try:
            proc.wait(timeout=15)
            break
        except subprocess.TimeoutExpired:
            continue
    else:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - last resort
            pass
    residual = sweep_session(sid)
    if token:
        residual += sweep_token(token)
    return residual


def run_one(tid: str, args, task_base: Path, run_id: str) -> dict:
    name = tid.replace(":", "_")
    log_dir = Path(args.out) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}.log"
    tdir = task_base / name
    with open(log_path, "a", encoding="utf-8") as logfh:
        def log(msg):
            logfh.write(msg.rstrip() + "\n")
            logfh.flush()

        ok, err = prepare_task(tid, task_base, Path(args.out), run_id, log)
        if not ok:
            results_store.merge_row(args.out, tid, {
                "task_id": tid, "run_id": run_id, "status": "gen_fail",
                "error": err, "finished_at": utcnow(),
                "official_success": None,
            })
            return {"task_id": tid, "status": "gen_fail", "error": err}

        # R3: one numeric uid per task; the wrapper runs the agent as
        # "#<uid>" with a private HOME/TMPDIR/XDG inside the 0700 workspace
        env = dict(os.environ, S4_RUN_ID=run_id, S4_WSROOT=str(args.ws_root),
                   S4_PASS1_TURNS=str(args.pass1_turns),
                   S4_PASS2_TURNS=str(args.pass2_turns))
        # R3.4: per-attempt token, exported into every agent process, so a
        # descendant that setsid()'d out of the task session can still be
        # found (and killed) by scanning /proc/<pid>/environ.
        token = f"s4t-{run_id}-{name}-{uuid.uuid4().hex[:12]}"
        env["S4_TASK_TOKEN"] = token
        if os.name == "posix" and euid() == 0:
            agent_uid = allocate_agent_uid(Path(args.out), name, run_id)
            control_write(Path(args.out), name, "agent_uid", str(agent_uid))
            # R3-B1: hand the prepared inputs to THIS uid only (0700)
            harden_task_dir(tdir, agent_uid)
            env["S4_AGENT_UID"] = str(agent_uid)
            log(f"[uid] {tid} agent uid={agent_uid} (task dir 0700, own uid)")

        log(f"[run] {tid} -> {tdir}")
        cmd = ["bash", f"{AG}/scripts/run_agent_task.sh", tid, str(tdir), str(args.out)]
        rc = None
        if os.name == "posix":
            # B12: own session per task -> the whole tree is killable on timeout
            proc = subprocess.Popen(cmd, stdout=logfh, stderr=subprocess.STDOUT,
                                    env=env, start_new_session=True)
            try:
                rc = proc.wait(timeout=args.task_timeout)
            except subprocess.TimeoutExpired:
                rc = 124
                log(f"[timeout] {tid} after {args.task_timeout}s; killing the process group")
                residual = kill_task_tree(proc, token)
                log(f"[timeout] {tid} process group terminated, "
                    f"session+token residual={residual}")
                if residual:
                    # R3.4: something survived SIGKILL after leaving the
                    # session -- retrying now would overlap two attempts on
                    # the same task.  `process_residual_error` is deliberately
                    # NOT in EXECUTION_FAILURE_STATUSES, so --retry-failed will
                    # not touch it until an operator has cleaned the host.
                    left = token_members(token)
                    results_store.merge_row(args.out, tid, {
                        "task_id": tid, "run_id": run_id,
                        "status": "process_residual_error",
                        "error": (f"{residual} process(es) survived the kill "
                                  f"(setsid escape; pids={left}); clean them "
                                  "before retrying this task"),
                        "finished_at": utcnow(), "official_success": None,
                    })
        else:  # Windows local tests
            try:
                proc = subprocess.run(cmd, stdout=logfh, stderr=subprocess.STDOUT,
                                      env=env, timeout=args.task_timeout)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                rc = 124
                log(f"[timeout] {tid} after {args.task_timeout}s")
        row = results_store.read_row(args.out, tid)
        if not row:
            results_store.merge_row(args.out, tid, {
                "task_id": tid, "run_id": run_id, "status": "worker_error",
                "error": f"run_agent_task rc={rc}, no row written",
                "finished_at": utcnow(), "official_success": None,
            })
            row = results_store.read_row(args.out, tid)
        log(f"[done] {tid} rc={rc} status={row.get('status')} "
            f"subs={row.get('submissions')}")
        return {"task_id": tid, "status": row.get("status"), "rc": rc}


def archive_attempt(out, tid: str, run_id: str) -> None:
    """Keep the previous attempt's row, then clear it for the new attempt.

    R2 MAJOR: the whole read-archive-clear sequence runs under the results
    lock, and the archive name carries a UUID suffix so two rapid attempts
    can never collide on the second-resolution timestamp."""
    out = Path(out)
    with FileLock(str(out / ".results.lock")):
        row = results_store.read_row(out, tid)
        if not row or row.get("run_id") not in (None, run_id):
            return
        arch = out / "rows" / "attempts"
        arch.mkdir(parents=True, exist_ok=True)
        stamp = (utcnow().replace(":", "").replace("-", "")
                 + "-" + uuid.uuid4().hex[:8])
        (arch / f"{tid.replace(':', '_')}.{stamp}.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        row.update({"run_id": run_id, "status": "running", "official_success": None,
                    "score_status": "pending", "final": None,
                    "attempt": int(row.get("attempt") or 0) + 1,
                    "attempted_at": utcnow()})
        results_store.write_row(out, tid, row)


def select_todo(queue: list, out, run_id: str, retry_failed: bool = False):
    """B9: split the queue into (todo, scoring_recovery).

    todo               tasks whose AGENT phase must (re)run
    scoring_recovery   execution-terminal rows without a scoring verdict --
                       they go to the scoring phase, never back to the agent
    """
    todo, scoring_recovery = [], []
    for tid in queue:
        row = results_store.read_row(out, tid)
        if not row or row.get("run_id") != run_id:
            todo.append(tid)
            continue
        retry = (retry_failed and (
            row.get("official_success") is False
            or row.get("status") in EXECUTION_FAILURE_STATUSES))
        if retry or not results_store.is_execution_terminal(row):
            todo.append(tid)   # crashed mid-run without a terminal status
        elif not results_store.is_scoring_terminal(row):
            scoring_recovery.append(tid)
    return todo, scoring_recovery


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", required=True)
    ap.add_argument("--out", default="/data/cybergym/stage4/r_engineer")
    ap.add_argument("--task-base", default="")
    ap.add_argument("--ws-root", default="")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--no-score", action="store_true")
    ap.add_argument("--no-backfill", action="store_true")
    ap.add_argument("--skip-verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pass1-turns", type=int, default=45)
    ap.add_argument("--pass2-turns", type=int, default=15)
    ap.add_argument("--task-timeout", type=int, default=7200)
    args = ap.parse_args()

    # R3.4: the degraded (unprotected-ledger) mode is a DEV-only escape hatch
    # that the wrapper alone honours.  The FORMAL driver refuses it up front:
    # a switch left in the environment by mistake would otherwise cost model
    # budget on the first task before anything rejected the run.  Dev runs
    # that really need it call run_agent_task.sh directly.
    if os.environ.get("S4_ALLOW_UNPROTECTED", "") == "1":
        raise SystemExit(
            "REFUSE: S4_ALLOW_UNPROTECTED=1 is a DEV-only escape hatch "
            "(unprotected ledger) and is not accepted on the formal driver "
            "entry point; unset it")

    if euid() != 0 and not args.dry_run:
        raise SystemExit("stage4.py must run as root (it starts the root wrapper)")
    out = Path(args.out)
    queue_path = Path(args.queue)
    if not queue_path.exists():
        raise SystemExit(f"queue not found: {queue_path}")
    queue = load_queue(queue_path)
    if not queue:
        raise SystemExit("queue is empty")
    try:
        results_store.assert_unique_task_ids(queue)   # R2 MAJOR: file-name collisions
    except results_store.ResultsIntegrityError as e:
        raise SystemExit(str(e))
    task_base = Path(args.task_base or (out / "tasks"))
    # R3.3: no hardcoded /data/cybergym path -- the workspace root defaults to
    # a sibling of the run dir, so one --out fully describes the run layout.
    ws_root = Path(args.ws_root or (out.parent / f"{out.name}-ws"))
    for d in (out, out / "rows", out / "logs", task_base, ws_root):
        d.mkdir(parents=True, exist_ok=True)
    # R3: the run dirs stay ROOT-owned.  task_base gets per-task subdirs
    # chowned to the prep user for gen, then to the per-task agent uid 0700
    # (R3-B1); task_base and ws_root are 0711 root:root so each per-task uid
    # can reach ITS OWN dir but cannot list its siblings.
    init_control_plane(out)
    if os.name == "posix" and euid() == 0:
        os.chmod(ws_root, 0o711)
        os.chmod(task_base, 0o711)
        os.chown(task_base, 0, 0)

    # R2 MAJOR: multi-worker locking needs a local filesystem
    lockable, fstype = assert_local_lockable(out, multi_worker=args.workers > 1)
    if not lockable:
        raise SystemExit(f"--out is on {fstype}; flock semantics are not "
                         f"equivalent to a local disk -- refuse to run "
                         f"{args.workers} workers (use a local --out)")

    # R2 MAJOR: one driver per --out; a second concurrent driver would race
    # on todo selection, archiving and phase execution
    try:
        run_lock = FileLock(str(out / ".run.lock"), timeout=0)
        run_lock.__enter__()
    except LockBusy:
        raise SystemExit(f"another stage4 driver holds {out}/.run.lock; "
                         "refusing to run concurrently")

    try:
        return _main_locked(args, out, queue_path, queue, task_base, ws_root)
    finally:
        run_lock.__exit__(None, None, None)


def _main_locked(args, out: Path, queue_path: Path, queue: list,
                 task_base: Path, ws_root: Path) -> int:
    run_id = args.run_id
    run_id_file = out / "run_id"
    if not run_id:
        if run_id_file.exists():
            run_id = run_id_file.read_text(encoding="utf-8").strip()
        else:
            run_id = "s4-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    atomic_write(run_id_file, run_id + "\n")

    meta_path = out / "run_meta.json"
    meta = {}
    if meta_path.exists():
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except json.JSONDecodeError:
            meta = {}
    meta.setdefault("started_at", utcnow())
    meta.update({"run_id": run_id, "queue": str(queue_path), "queue_size": len(queue),
                 "queue_sha256": queue_digest(queue), "host": socket.gethostname(),
                 "workers": args.workers, "task_base": str(task_base),
                 "ws_root": str(ws_root)})
    atomic_write(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))
    atomic_write(out / "rows_index.json", json.dumps(queue))

    todo, scoring_recovery = select_todo(queue, out, run_id, args.retry_failed)
    if args.limit:
        todo = todo[:args.limit]

    for tid in todo:
        archive_attempt(out, tid, run_id)

    print(f"[stage4] run_id={run_id} queue={len(queue)} todo={len(todo)} "
          f"scoring_recovery={len(scoring_recovery)} "
          f"workers={args.workers} out={out}")

    execution_failures = []
    if args.dry_run:
        print("TODO:\n" + "\n".join(todo[:200]))
        if scoring_recovery:
            print("SCORING-RECOVERY (agent NOT re-run):\n"
                  + "\n".join(scoring_recovery[:200]))
        return 0
    if not todo:
        print("[stage4] nothing to do")
    else:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futs = {pool.submit(run_one, tid, args, task_base, run_id): tid
                    for tid in todo}
            for fut in as_completed(futs):
                tid = futs[fut]
                try:
                    res = fut.result()
                    print(f"[stage4] {tid}: {res.get('status')}")
                    if res.get("status") in EXECUTION_FAILURE_STATUSES:
                        execution_failures.append(tid)
                except Exception as e:
                    print(f"[stage4] {tid}: EXC {type(e).__name__}: {e}")
                    execution_failures.append(tid)

    results_store.rebuild(out, order=queue)

    # ---- B7: every phase's exit code decides the round's outcome ----------
    # A missing phase script is a FAILED phase, not a warning: a round whose
    # fix-side backfill never ran must not be published as scored.
    phase_rc: dict = {}

    def run_phase(name: str, cmd: list) -> None:
        print(f"[stage4] {name}: {' '.join(str(c) for c in cmd)}")
        # phases must see the deployment + this run: root_code/score_all.sh
        # resolves $S4_AGENT_HOME and $S4_RUN_ID from its environment
        child_env = {**os.environ, "S4_AGENT_HOME": str(AG), "S4_RUN_ID": run_id}
        r = subprocess.run(cmd, check=False, env=child_env)
        phase_rc[name] = r.returncode
        if r.returncode != 0:
            print(f"[stage4] {name} FAILED rc={r.returncode}")

    if not args.no_backfill:
        backfill = os.environ.get("S4_BACKFILL", "/root/backfill_fix_side.sh")
        if Path(backfill).exists():
            run_phase("backfill", ["bash", backfill])
        else:
            print(f"[stage4] backfill script not found at {backfill}")
            phase_rc["backfill"] = 127
    if not args.no_score:
        # R3.3: score with the deployment's OWN scorer by default.  The old
        # default (/root/score_all.sh) was a stage-3 host wrapper that only
        # filled official_success in results.json: the row kept score_status
        # None / final.* empty, so verify reported a submitted-but-unscored
        # task.  S4_SCORE_ALL still overrides for hosts that need a wrapper
        # (it is called with <out> <task_base> <ws_root> positionally).
        score_all = os.environ.get("S4_SCORE_ALL", "")
        scorer = Path(AG) / "scripts" / "score_all.py"
        if score_all and Path(score_all).exists():
            run_phase("score", ["bash", score_all, str(out), str(task_base), str(ws_root)])
        elif not score_all and scorer.exists():
            run_phase("score", [sys.executable, str(scorer), "--out", str(out),
                                "--task-base", str(task_base),
                                "--ws-root", str(ws_root), "--run-id", run_id])
        else:
            print(f"[stage4] score_all script not found (S4_SCORE_ALL={score_all!r}, "
                  f"deployment={scorer})")
            phase_rc["score"] = 127
    if not args.skip_verify:
        verify = Path(AG) / "scripts" / "verify_stage4.py"
        if verify.exists():
            run_phase("verify", [sys.executable, str(verify), "--out", str(out),
                                 "--queue", str(queue_path),
                                 "--ws-root", str(ws_root)])
        else:
            print(f"[stage4] verify script not found at {verify}")
            phase_rc["verify"] = 127

    phase_failures = sorted(k for k, v in phase_rc.items() if v != 0)

    # rows still without a scoring verdict after the scoring phase ran
    still_unscored = []
    for tid in queue:
        row = results_store.read_row(out, tid)
        if (row is not None and row.get("run_id") == run_id
                and results_store.is_execution_terminal(row)
                and not results_store.is_scoring_terminal(row)):
            still_unscored.append(tid)

    summary = {
        "run_id": run_id, "finished_at": utcnow(), "todo": len(todo),
        "execution_failures": sorted(execution_failures),
        "scoring_pending": sorted(still_unscored),
        "phase_exit_codes": phase_rc,
        "phase_failures": phase_failures,
    }
    atomic_write(out / "batch_summary.json",
                 json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[stage4] done run_id={run_id} execution_failures={len(execution_failures)} "
          f"scoring_pending={len(still_unscored)} phase_failures={phase_failures or 'none'}")
    if execution_failures or still_unscored or phase_failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
