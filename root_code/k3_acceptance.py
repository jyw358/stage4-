#!/usr/bin/env python3
"""K3 broker non-scoring acceptance on the isolated k8 host tree.

Run as root after deploying the sibling unit and starting it once:
  python3 k3_acceptance.py setup
  python3 k3_acceptance.py run --execute

The fixture is built from tests/fixtures/k3_oracle/target.c.  No formal task,
fix-side data, answer key, submission ledger, or historical PoC is read.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid

BASE = pathlib.Path(os.environ.get("K3_BASE", "/data2/arms/k8"))
AG = BASE / "cybergym-agent"
SCRIPTS = AG / "scripts"
FIXTURE_SRC = AG / "tests" / "fixtures" / "k3_oracle"
DATA = pathlib.Path("/data/cybergym/cybergym-server-data/oss-fuzz")
OUT = BASE / "out"
EVIDENCE_ROOT = BASE / "evidence"
MAIN_SOCKET = pathlib.Path("/run/cybergym-oracle/broker.sock")
MAIN_AUDIT = pathlib.Path("/var/lib/cybergym-oracle/audit.jsonl")
UNIT = "cybergym-oracle-broker.service"
RUNNER = "cybergym/oss-fuzz-base-runner:latest"
# Built from target.c in this same non-scoring change and separately executed
# under the Ubuntu-20-compatible runner before this acceptance harness existed.
PREBUILT_SHA256 = "0e5a3697fb71c89aa4dae048bacac8b90a684207328f9fa9df62f6561a5b4a15"
TASK_IDS = {"A": "oss-fuzz:900000001", "B": "oss-fuzz:900000002"}
UIDS = {"A": 46001, "B": 46002}
TOOLS = ("local_oracle.py", "oracle_broker.py", "try_local.sh", "stab_run.sh",
         "minsep.py", "probe_path.sh", "directed_fuzz.py", "hf_mutator.py")


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", 60)
    return subprocess.run([str(x) for x in cmd], **kwargs)


def require_success(proc, what):
    if proc.returncode != 0:
        raise RuntimeError(f"{what} failed rc={proc.returncode}: "
                           f"{(proc.stdout or '')[-1000:]} {(proc.stderr or '')[-1000:]}")
    return proc


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_json_lines(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def root_call(socket_path, request, timeout=40):
    """Version/readiness helper; task calls use a real dropped-UID peer."""
    payload = (json.dumps(request, separators=(",", ":")) + "\n").encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(socket_path))
        conn.sendall(payload)
        data = bytearray()
        while b"\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
    return json.loads(bytes(data).split(b"\n", 1)[0].decode())


def wait_version(socket_path, timeout=15):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            response = root_call(socket_path, {"op": "version"}, timeout=1)
            if response.get("op") == "version":
                return response
            last_error = response
        except (OSError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.05)
    raise RuntimeError(f"broker did not become ready at {socket_path}: {last_error}")


def setup_fixture():
    if os.name != "posix" or os.geteuid() != 0:
        raise SystemExit("setup must run as root on Linux")
    os.umask(0o077)
    if not (FIXTURE_SRC / "target.c").is_file():
        raise SystemExit(f"fixture source missing: {FIXTURE_SRC}")
    build = pathlib.Path(tempfile.mkdtemp(prefix="k3-fixture-build-", dir=str(BASE)))
    try:
        os.chmod(build, 0o700)
        compile_proc = run([
            "docker", "run", "--rm", "-v", f"{FIXTURE_SRC}:/src:ro",
            "-v", f"{build}:/build", RUNNER, "bash", "-c",
            "gcc -O1 -g -fsanitize=address -fno-omit-frame-pointer "
            "-o /build/k3_fixture /src/target.c"], timeout=180)
        built = build / "k3_fixture"
        build_method = "source-in-runner"
        if compile_proc.returncode != 0:
            # The production runner intentionally contains no toolchain.  The
            # pinned binary is an explicit reviewed fixture asset, never a
            # silently selected host binary or a historical task executable.
            prebuilt = FIXTURE_SRC / "k3_fixture_u20"
            if not prebuilt.is_file() or sha256(prebuilt) != PREBUILT_SHA256:
                require_success(compile_proc, "fixture build and pinned prebuilt check")
            shutil.copyfile(prebuilt, built)
            build_method = "pinned-prebuilt-u20"
        if not built.is_file():
            raise RuntimeError("fixture compiler did not produce k3_fixture")
        manifest = {"purpose": "non-scoring-k3-oracle-acceptance",
                    "built_at": utcnow(), "runner": RUNNER,
                    "build_method": build_method,
                    "source_sha256": sha256(FIXTURE_SRC / "target.c"),
                    "binary_sha256": sha256(built), "tasks": {}}
        for label, task_id in TASK_IDS.items():
            number = task_id.split(":", 1)[1]
            vul = DATA / number / "vul"
            metadata = vul / "metadata.json"
            if vul.exists() and not metadata.is_file():
                raise RuntimeError(f"refuse to overwrite non-fixture path {vul}")
            if metadata.is_file():
                old = json.loads(metadata.read_text(encoding="utf-8"))
                if old.get("purpose") != "non-scoring-k3-oracle-acceptance":
                    raise RuntimeError(f"refuse to overwrite non-fixture metadata {metadata}")
            (vul / "out").mkdir(parents=True, exist_ok=True)
            (vul / "libs").mkdir(parents=True, exist_ok=True)
            # Only this dedicated VUL fixture is Agent-readable; real tools
            # resolve local metadata before run_side, while the broker ignores it.
            for directory in (vul.parent, vul, vul / "out", vul / "libs"):
                os.chmod(directory, 0o755)
            shutil.copyfile(built, vul / "out" / "k3_fixture")
            shutil.copyfile(FIXTURE_SRC / "metadata.json", metadata)
            os.chmod(vul / "out" / "k3_fixture", 0o755)
            os.chmod(metadata, 0o644)
            manifest["tasks"][label] = {
                "task_id": task_id, "path": str(vul),
                "binary_sha256": sha256(vul / "out" / "k3_fixture"),
                "inputs": {name: sha256(FIXTURE_SRC / name)
                           for name in ("clean.bin", "fatal.bin", "hang.bin")}}
        write_json(BASE / "fixture-manifest.json", manifest)
        print(json.dumps(manifest, indent=2, sort_keys=True))
    finally:
        shutil.rmtree(build, ignore_errors=True)


class AgentFixture:
    def __init__(self, label, run_root, ws_parent, cgroup_parent, evidence):
        self.label = label
        self.task_id = TASK_IDS[label]
        self.name = self.task_id.replace(":", "_")
        self.uid = UIDS[label]
        self.run_root = run_root
        self.control = run_root / "control" / self.name
        self.control.mkdir(parents=True, exist_ok=True)
        os.chmod(self.control, 0o700)
        self.workspace = ws_parent / self.name
        self.workspace.mkdir(parents=True, exist_ok=True)
        os.chmod(self.workspace, 0o700)
        os.chown(self.workspace, self.uid, self.uid)
        for sub in (".tmp", ".home"):
            path = self.workspace / sub
            path.mkdir(exist_ok=True)
            os.chown(path, self.uid, self.uid)
            os.chmod(path, 0o700)
        for name in ("clean.bin", "fatal.bin", "hang.bin"):
            shutil.copyfile(FIXTURE_SRC / name, self.workspace / name)
        for name in TOOLS:
            shutil.copyfile(SCRIPTS / name, self.workspace / name)
            if name.endswith(".sh"):
                os.chmod(self.workspace / name, 0o755)
        for path in self.workspace.iterdir():
            if path.is_file() and not path.is_symlink():
                os.chown(path, self.uid, self.uid)
        self.control_cgroup = f"/k3acc-{run_root.name}-{label}"
        self.cgroup = cgroup_parent / self.control_cgroup.lstrip("/")
        self.cgroup.mkdir(exist_ok=True)
        self.wrong_cgroup = cgroup_parent / f"k3acc-{run_root.name}-{label}-wrong"
        self.wrong_cgroup.mkdir(exist_ok=True)
        self.evidence = evidence
        self.process = None

    def write_control(self, pid, *, active="1", uid=None, cgroup=None):
        values = {
            "run_id": self.run_root.name,
            "agent_uid": str(self.uid if uid is None else uid),
            "active": str(active),
            "attempt_pid": str(pid),
            "cgroup": str(self.control_cgroup if cgroup is None else cgroup),
            "workspace": str(self.workspace),
        }
        for key, value in values.items():
            path = self.control / key
            path.write_text(value + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        os.chmod(self.run_root / "run_id", 0o600)

    def command(self, argv, *, active="1", uid=None, exec_cgroup=None,
                publish_uid=None, timeout=70):
        if self.process is not None:
            raise RuntimeError(f"fixture {self.label} already has a process")
        before_logs = len(self.logs())
        cg = self.cgroup if exec_cgroup is None else exec_cgroup
        shell = ('echo $$ > "$1/cgroup.procs"; '
                 'while [ ! -e "$2/.k3_ready" ]; do sleep 0.05; done; '
                 'shift 2; exec "$@"')
        prefix = ["setpriv", f"--reuid={self.uid}", f"--regid={self.uid}",
                  "--clear-groups", "env", "-i",
                  "PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8",
                  f"HOME={self.workspace / '.home'}",
                  f"TMPDIR={self.workspace / '.tmp'}",
                  f"CG_TASK_ID={self.task_id}",
                  f"CG_ORACLE_SOCKET={MAIN_SOCKET}"]
        proc = subprocess.Popen(
            ["/bin/sh", "-c", shell, "k3-agent", str(cg), str(self.workspace)]
            + prefix + [str(x) for x in argv],
            cwd=str(self.workspace), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.process = proc
        self.write_control(proc.pid, active=active, uid=publish_uid,
                           cgroup=self.control_cgroup)
        ready = self.workspace / ".k3_ready"
        ready.touch()
        os.chown(ready, self.uid, self.uid)
        os.chmod(ready, 0o600)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            raise RuntimeError(f"agent command timed out: {stdout[-500:]} {stderr[-500:]}")
        finally:
            self.write_control(proc.pid, active="0", uid=uid)
            self.process = None
            try:
                ready.unlink()
            except FileNotFoundError:
                pass
        return {"rc": proc.returncode, "stdout": stdout, "stderr": stderr,
                "new_logs": self.logs()[before_logs:]}

    def logs(self):
        return load_json_lines(self.workspace / ".oracle.log")

    @staticmethod
    def parse_result(output):
        marked = [line[len("K3_JSON="):] for line in output.splitlines()
                  if line.startswith("K3_JSON=")]
        if not marked:
            raise RuntimeError(f"no K3_JSON result in output: {output[-1000:]}")
        return json.loads(marked[-1])

    def oracle(self, files, *, timeout=10, tag="acceptance", socket_path=MAIN_SOCKET):
        argv = ["/usr/bin/python3", "-I", "-B", __file__, "--agent-run",
                self.task_id, str(socket_path), tag, str(timeout)] + list(files)
        result = self.command(argv, timeout=max(70, timeout + 50))
        response = self.parse_result(result["stdout"])
        result["response"] = response
        return result

    def raw(self, request, *, socket_path=MAIN_SOCKET, active="1",
            publish_uid=None, exec_cgroup=None):
        argv = ["/usr/bin/python3", "-I", "-B", __file__, "--agent-raw",
                self.task_id, str(socket_path), json.dumps(request, separators=(",", ":"))]
        result = self.command(argv, active=active, publish_uid=publish_uid,
                              exec_cgroup=exec_cgroup)
        result["response"] = self.parse_result(result["stdout"])
        return result

    def start_oracle(self, files, *, timeout, tag):
        """Start one asynchronous broker request; caller finishes/kills it."""
        argv = ["/usr/bin/python3", "-I", "-B", __file__, "--agent-run",
                self.task_id, str(MAIN_SOCKET), tag, str(timeout)] + list(files)
        holder = {}

        def target():
            try:
                result = self.command(argv, timeout=timeout + 60)
                if "K3_JSON=" in result["stdout"]:
                    result["response"] = self.parse_result(result["stdout"])
                holder["result"] = result
            except BaseException as exc:
                holder["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=target)
        thread.start()
        return thread, holder

    def cleanup(self):
        self.write_control(0, active="0")
        if self.process is not None:
            self.process.kill()
            self.process.communicate()
            self.process = None
        for cg in (self.wrong_cgroup, self.cgroup):
            try:
                os.rmdir(cg)
            except OSError:
                pass


def agent_client(argv):
    mode = argv[0]
    task_id, socket_path = argv[1], argv[2]
    # The client is deliberately workspace code, as in a real generated task.
    sys.path.insert(0, os.getcwd())
    os.environ["CG_TASK_ID"] = task_id
    os.environ["CG_ORACLE_SOCKET"] = socket_path
    if mode == "--agent-raw":
        from oracle_broker import call
        response = call(json.loads(argv[3]), socket_path, timeout=45)
    else:
        from local_oracle import run_side
        tag, timeout, files = argv[3], int(argv[4]), argv[5:]
        ignored_info = ("/agent-controlled/vul", "agent_controlled_target",
                        "detect_leaks=0", "oss-fuzz")
        response = run_side(ignored_info, files, timeout=timeout, tag=tag)
    print("K3_JSON=" + json.dumps(response, separators=(",", ":")))
    return 0


def docker_broker_containers(instance="k8"):
    proc = require_success(run([
        "docker", "ps", "-a", "--filter", "label=cybergym.oracle_broker=1",
        "--filter", f"label=cybergym.oracle_broker.broker_instance={instance}",
        "--format", "{{.ID}}\t{{.Names}}"]), "list broker containers")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def start_alt_broker(name, evidence, extra_env):
    # Agent peers must traverse the socket ancestors; audit logs remain in the
    # root-private evidence directory.  Run-ID names prevent cross-run reuse.
    runtime = MAIN_SOCKET.parent
    runtime.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime, 0o711)
    sock = runtime / (evidence.name + "-" + name + ".sock")
    audit = evidence / (name + "-audit.jsonl")
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1",
           "ORACLE_BROKER_SOCKET": str(sock),
           "ORACLE_BROKER_RUN_ROOTS": str(OUT),
           "ORACLE_BROKER_AUDIT": str(audit),
           "ORACLE_BROKER_INSTANCE": "k8-" + name,
           "ORACLE_RUNNER": RUNNER}
    env.update(extra_env)
    log = (evidence / (name + ".log")).open("w", encoding="utf-8")
    proc = subprocess.Popen(["/usr/bin/python3", "-I", "-B",
                             str(SCRIPTS / "oracle_broker.py")],
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log.flush()
            raise RuntimeError(f"alternate broker {name} exited rc={proc.returncode}")
        if sock.exists():
            try:
                version = root_call(sock, {"op": "version"})
                if version.get("op") == "version":
                    return proc, log, sock, audit
            except OSError:
                pass
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError(f"alternate broker {name} did not become ready")


def stop_alt(proc, log, sock):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    log.close()
    try:
        sock.unlink()
    except FileNotFoundError:
        pass


def acceptance(execute):
    if not execute or os.name != "posix" or os.geteuid() != 0:
        raise SystemExit("run requires root and explicit --execute")
    os.umask(0o077)
    require_success(run(["systemctl", "is-active", "--quiet", UNIT]),
                    "main broker active check")
    run_id = "k3acc-" + datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    evidence = EVIDENCE_ROOT / run_id
    run_root = OUT / run_id
    ws_base = BASE / "ws"
    # Real tools import through an absolute workspace path after setpriv.  The
    # shared ancestor must be traversable; each per-Agent workspace stays 0700.
    ws_base.mkdir(parents=True, exist_ok=True)
    os.chmod(ws_base, 0o711)
    ws_parent = ws_base / run_id
    for path, mode in ((evidence, 0o700), (run_root / "control", 0o700),
                       (ws_parent, 0o711)):
        path.mkdir(parents=True, exist_ok=False)
        os.chmod(path, mode)
    (run_root / "run_id").write_text(run_id + "\n", encoding="utf-8")
    os.chmod(run_root / "run_id", 0o600)
    report = {"schema": 1, "purpose": "K3 broker non-scoring acceptance",
              "run_id": run_id, "started_at": utcnow(), "status": "RUNNING",
              "checks": [], "normal_responses": [], "security_responses": [],
              "fixture_manifest": str(BASE / "fixture-manifest.json")}
    fixtures = []
    alternates = []

    def check(name, condition, details=None):
        row = {"name": name, "passed": bool(condition), "details": details}
        report["checks"].append(row)
        if not condition:
            raise AssertionError(f"{name}: {details}")

    def confirmed(response):
        return (response.get("runner_started") is True and
                response.get("target_started") is True and
                response.get("target_exit_code") is not None and
                response.get("runner_exit_code") == 0)

    def normal(result, expected):
        response = result["response"]
        report["normal_responses"].append(response)
        check("normal_" + expected.lower() + "_" + response.get("request_id", "?"),
              response.get("verdict") == expected and confirmed(response), response)
        expected_hashes = [sha256(fixtures[0].workspace / f) for f in
                           (["clean.bin"] if expected == "CLEAN" else ["fatal.bin"])]
        if response.get("input_sha256") != expected_hashes:
            check("normal_input_hash_" + response.get("request_id", "?"), False,
                  {"actual": response.get("input_sha256"), "expected": expected_hashes})
        return response

    try:
        # Real trusted-boot negative, confined to k8. Restore before any task call.
        trusted_file = SCRIPTS / "local_oracle.py"
        old_mode = stat.S_IMODE(trusted_file.stat().st_mode)
        require_success(run(["systemctl", "stop", UNIT]), "stop k8 for integrity test")
        try:
            os.chmod(trusted_file, old_mode | stat.S_IWGRP)
            direct = run(["/usr/bin/python3", "-B",
                          str(BASE / "root_code" / "assert_code_tree.py"),
                          "--base", str(AG), "--untrusted-users", "s4agent,naco"])
            start = run(["systemctl", "start", UNIT])
            active = run(["systemctl", "is-active", UNIT])
            check("root_code_integrity_rejects_agent_writable_tree",
                  direct.returncode != 0 and start.returncode != 0 and
                  active.stdout.strip() != "active",
                  {"audit_rc": direct.returncode, "start_rc": start.returncode,
                   "active": active.stdout.strip(),
                   "audit_output": (direct.stdout + direct.stderr)[-1200:]})
        finally:
            os.chmod(trusted_file, old_mode)
            run(["systemctl", "reset-failed", UNIT])
            require_success(run(["systemctl", "start", UNIT]), "restore k8 broker")
        version = wait_version(MAIN_SOCKET)
        check("broker_version_and_socket", version.get("op") == "version", version)
        main_pid = require_success(run(["systemctl", "show", UNIT, "-p", "MainPID", "--value"]),
                                   "read broker pid").stdout.strip()
        cmdline = pathlib.Path("/proc") / main_pid / "cmdline"
        argv0 = cmdline.read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        check("root_interpreter_isolated_no_bytecode", "-I -B" in argv0, argv0)

        for label in ("A", "B"):
            fixtures.append(AgentFixture(label, run_root, ws_parent,
                                         pathlib.Path("/sys/fs/cgroup"), evidence))
        a, b = fixtures
        check("no_initial_broker_containers", not docker_broker_containers(),
              docker_broker_containers())

        # Real tool wrappers, not just an in-process classifier test.
        tool = a.command(["/bin/bash", "try_local.sh", "clean.bin"])
        logs = tool["new_logs"]
        check("try_local_clean_tool", tool["rc"] == 0 and len(logs) == 1 and
              logs[-1].get("verdict") == "CLEAN", tool)
        for entry in logs:
            if entry.get("request_id"):
                report["normal_responses"].append(entry)
        tool = a.command(["/bin/bash", "try_local.sh", "fatal.bin"])
        logs = tool["new_logs"]
        check("try_local_fatal_tool", tool["rc"] == 1 and len(logs) == 1 and
              logs[-1].get("verdict") in ("FATAL", "NONFATAL") and
              logs[-1].get("target_exit_code") not in (None, 0), tool)
        report["normal_responses"].extend(x for x in logs if x.get("request_id"))
        tool = a.command(["/bin/bash", "stab_run.sh", "clean.bin", "3"])
        logs = tool["new_logs"]
        check("stab_run_three_broker_executions", len(logs) == 3 and
              all(x.get("verdict") == "CLEAN" for x in logs), tool)
        report["normal_responses"].extend(x for x in logs if x.get("request_id"))
        tool = a.command(["/usr/bin/python3", "minsep.py", "fatal.bin",
                          "--max-calls", "1"])
        logs = tool["new_logs"]
        check("minsep_broker_and_reverify", tool["rc"] == 1 and len(logs) == 3 and
              logs[0].get("verdict") in ("FATAL", "NONFATAL") and
              logs[1].get("verdict") in ("CLEAN", "FATAL", "NONFATAL") and
              logs[2].get("verdict") in ("FATAL", "NONFATAL") and
              logs[-1].get("tag") == "minsep_reverify", tool)
        report["normal_responses"].extend(x for x in logs if x.get("request_id"))

        for _ in range(5):
            normal(a.oracle(["clean.bin"], tag="acceptance_clean"), "CLEAN")
            crash = normal(a.oracle(["fatal.bin"], tag="acceptance_fatal"), "FATAL")
            check("fatal_signature_" + crash["request_id"], bool(crash.get("sig")),
                  crash.get("sig"))

        hang = a.oracle(["hang.bin"], timeout=2, tag="acceptance_hang")["response"]
        report["normal_responses"].append(hang)
        check("hang_is_evidence_based_not_fake_timeout",
              hang.get("verdict") == "UNKNOWN" and
              hang.get("target_exit_code") == 137 and confirmed(hang), hang)
        check("hang_has_no_container_residue", not docker_broker_containers(),
              docker_broker_containers())

        # Unavailable broker, bad image, and a REAL failing docker inspection.
        require_success(run(["systemctl", "stop", UNIT]),
                        "stop broker for unavailable fault injection")
        try:
            unavailable = a.oracle(["clean.bin"], socket_path=MAIN_SOCKET,
                                   tag="fault_unavailable")["response"]
        finally:
            require_success(run(["systemctl", "start", UNIT]),
                            "restart broker after unavailable fault injection")
            wait_version(MAIN_SOCKET)
        check("stopped_broker_fail_closed",
              unavailable.get("verdict") == "ORACLE_ERROR" and
              unavailable.get("runner_error_kind") == "broker_unavailable", unavailable)
        offline_bin = evidence / "offline-bin"
        offline_bin.mkdir()
        offline_docker = offline_bin / "docker"
        offline_docker.write_text(
            '#!/bin/sh\nif [ "$1" = run ]; then shift; exec /usr/bin/docker run --pull=never "$@"; fi\n'
            'exec /usr/bin/docker "$@"\n', encoding="utf-8")
        os.chmod(offline_docker, 0o755)
        alt = start_alt_broker(
            "bad-image", evidence,
            {"ORACLE_RUNNER": "cybergym/k3-definitely-absent:latest",
             "PATH": str(offline_bin) + ":/usr/local/bin:/usr/bin:/bin"})
        alternates.append(alt)
        bad_image = a.oracle(["clean.bin"], socket_path=alt[2], tag="fault_image")["response"]
        check("bad_image_fail_closed", bad_image.get("verdict") == "ORACLE_ERROR" and
              bad_image.get("runner_error_kind") == "image_unavailable" and
              bad_image.get("runner_started") is not True, bad_image)
        fake_bin = evidence / "fake-bin"
        fake_bin.mkdir()
        fake_docker = fake_bin / "docker"
        fake_docker.write_text('#!/bin/sh\nif [ "$1" = ps ]; then exit 1; fi\nexec /usr/bin/docker "$@"\n',
                               encoding="utf-8")
        os.chmod(fake_docker, 0o755)
        alt2 = start_alt_broker("inspect-failure", evidence,
                                {"PATH": str(fake_bin) + ":/usr/local/bin:/usr/bin:/bin"})
        alternates.append(alt2)
        incomplete = a.oracle(["clean.bin"], socket_path=alt2[2],
                              tag="fault_inspection")["response"]
        check("cleanup_inspection_incomplete_blocks_start",
              incomplete.get("verdict") == "ORACLE_ERROR" and
              incomplete.get("runner_error_kind") == "sweep_preflight_failed" and
              incomplete.get("orphan_cleanup", [{}])[0].get("cleanup") ==
              "inspection_incomplete", incomplete)

        # Unknown fields and path/input boundaries over a real dropped-UID UDS peer.
        base_req = {"op": "run_vul_candidate", "task_id": a.task_id,
                    "inputs": ["clean.bin"], "timeout": 3, "perturb": None,
                    "tag": "raw"}
        raw_cases = [
            ("unknown_fix_field", dict(base_req, side="fix"), "unknown_field"),
            ("path_escape", dict(base_req, inputs=["../escape"]), "bad_inputs"),
            ("absolute_path", dict(base_req, inputs=["/etc/passwd"]), "bad_inputs"),
            ("too_many_inputs", dict(base_req, inputs=["clean.bin"] * 9), "bad_inputs"),
        ]
        for name, request, reason in raw_cases:
            response = a.raw(request)["response"]
            report["security_responses"].append(response)
            check(name, response.get("verdict") == "ORACLE_ERROR" and
                  response.get("refusal_reason") == reason, response)
        outside = a.workspace / "outside"
        outside.mkdir(exist_ok=True)
        shutil.copyfile(a.workspace / "fatal.bin", outside / "in0")
        os.symlink("outside/in0", a.workspace / "final-link")
        response = a.raw(dict(base_req, inputs=["final-link"]))["response"]
        check("final_symlink_refused", response.get("verdict") == "INPUT_ERROR", response)
        real = a.workspace / "candidates_real"
        real.mkdir(exist_ok=True)
        shutil.copyfile(a.workspace / "clean.bin", real / "in0")
        os.symlink("outside", a.workspace / "candidates")
        response = a.raw(dict(base_req, inputs=["candidates/in0"]))["response"]
        check("intermediate_symlink_refused", response.get("verdict") == "INPUT_ERROR",
              response)
        (a.workspace / "candidates").unlink()
        os.rename(real, a.workspace / "candidates")
        large = a.workspace / "large.bin"
        with large.open("wb") as fh:
            fh.truncate(1024 * 1024 + 1)
        response = a.raw(dict(base_req, inputs=["large.bin"]))["response"]
        check("oversize_input_refused", response.get("verdict") == "INPUT_ERROR" and
              response.get("runner_error_kind") == "input_too_large", response)

        # A real rename/symlink race may safely use an already-open clean FD or
        # fail closed, but must never execute the outside fatal input.
        race = {"stop": False, "error": None}

        def mutate_directory():
            try:
                while not race["stop"]:
                    link = a.workspace / "candidates"
                    saved = a.workspace / "candidates_real"
                    if link.is_symlink():
                        link.unlink()
                        os.rename(saved, link)
                    elif link.is_dir():
                        os.rename(link, saved)
                        os.symlink("outside", link)
            except OSError as exc:
                race["error"] = str(exc)

        racer = threading.Thread(target=mutate_directory)
        racer.start()
        try:
            race_rows = []
            for _ in range(10):
                response = a.raw(dict(base_req, inputs=["candidates/in0"]))["response"]
                race_rows.append(response)
                safe = (response.get("verdict") == "INPUT_ERROR" or
                        (response.get("verdict") == "CLEAN" and
                         response.get("input_sha256") == [sha256(a.workspace / "clean.bin")]))
                check("toctou_directory_race_safe", safe and race["error"] is None,
                      {"response": response, "race_error": race["error"]})
        finally:
            race["stop"] = True
            racer.join()
            link = a.workspace / "candidates"
            if link.is_symlink():
                link.unlink()
                os.rename(a.workspace / "candidates_real", link)
        report["toctou_responses"] = race_rows

        response = a.raw(dict(base_req, task_id=b.task_id))["response"]
        check("task_binding_mismatch_refused", response.get("refusal_reason") ==
              "task_mismatch", response)
        response = a.raw(base_req, publish_uid=a.uid + 100)["response"]
        check("unregistered_uid_refused", response.get("refusal_reason") ==
              "uid_mismatch", response)
        response = a.raw(base_req, active="0")["response"]
        check("inactive_or_old_uid_refused", response.get("refusal_reason") ==
              "inactive", response)
        response = a.raw(base_req, exec_cgroup=a.wrong_cgroup)["response"]
        check("wrong_cgroup_refused", response.get("refusal_reason") ==
              "cgroup_mismatch", response)
        b.write_control(os.getpid(), active="1", uid=a.uid)
        try:
            response = a.raw(base_req)["response"]
            check("duplicate_active_uid_refused", response.get("refusal_reason") ==
                  "duplicate_uid", response)
        finally:
            b.write_control(0, active="0")

        denied = a.command(["/usr/bin/docker", "ps"])
        check("agent_still_has_no_docker_permission", denied["rc"] != 0 and
              "permission denied" in (denied["stdout"] + denied["stderr"]).lower(), denied)
        probe = a.command(["/bin/bash", "probe_path.sh", "clean.bin", "main"])
        check("probe_path_remains_fail_closed_out_of_scope", probe["rc"] == 5 and
              "ORACLE_ERROR" in probe["stdout"], probe)
        fuzz = a.command(["/usr/bin/python3", "directed_fuzz.py", "clean.bin",
                          "--seconds", "30"])
        check("directed_fuzz_own_docker_remains_fail_closed", fuzz["rc"] == 7 and
              "ORACLE_ERROR" in fuzz["stdout"], fuzz)

        # Client death does not cancel the broker's independent deadline.  B
        # must remain alive throughout A's abnormal client/request boundary.
        before_results = {row.get("request_id") for row in load_json_lines(MAIN_AUDIT)
                          if row.get("phase") == "result"}
        thread_a, holder_a = a.start_oracle(["hang.bin"], timeout=2, tag="client_kill_a")
        time.sleep(0.3)
        thread_b, holder_b = b.start_oracle(["hang.bin"], timeout=8, tag="surviving_b")
        time.sleep(0.8)
        check("parallel_broker_requests_started", a.process is not None and
              b.process is not None and len(docker_broker_containers()) >= 2,
              docker_broker_containers())
        a.process.kill()
        thread_a.join(10)
        check("killed_agent_process_reaped", not thread_a.is_alive() and
              holder_a.get("result", {}).get("rc") == -signal.SIGKILL,
              holder_a.get("result"))
        deadline = time.monotonic() + 15
        a_result = None
        while time.monotonic() < deadline and a_result is None:
            for row in load_json_lines(MAIN_AUDIT):
                if (row.get("phase") == "result" and
                        row.get("request_id") not in before_results and
                        row.get("task_id") == a.task_id and
                        row.get("input_sha256") == [sha256(a.workspace / "hang.bin")]):
                    a_result = row
                    break
            time.sleep(0.1)
        check("killed_client_still_has_root_result", a_result is not None and
              a_result.get("verdict") == "UNKNOWN", a_result)
        check("task_a_failure_does_not_kill_task_b",
              thread_b.is_alive() and len(docker_broker_containers()) >= 1,
              docker_broker_containers())
        thread_b.join(25)
        check("task_b_completes_independently", not thread_b.is_alive() and
              holder_b.get("result", {}).get("response", {}).get("verdict") == "UNKNOWN",
              holder_b.get("result"))
        if holder_b.get("result", {}).get("response"):
            report["normal_responses"].append(holder_b["result"]["response"])
        check("all_parallel_containers_reaped", not docker_broker_containers(),
              docker_broker_containers())

        attempts = len(report["normal_responses"])
        confirmed_n = sum(1 for row in report["normal_responses"] if confirmed(row))
        report["normal_metrics"] = {"validation_attempts": attempts,
                                    "execution_confirmed": confirmed_n,
                                    "rate": confirmed_n / attempts if attempts else 0}
        check("normal_execution_confirmation_at_least_95_percent",
              attempts > 0 and confirmed_n / attempts >= 0.95,
              report["normal_metrics"])

        # Cross-check every Agent request_id with exactly one root request and
        # result, including hashes/container/verdict on the result side.
        audit_files = [MAIN_AUDIT] + [item[3] for item in alternates]
        audit_rows = []
        for path in audit_files:
            audit_rows.extend(load_json_lines(path))
        agent_rows = []
        for fixture in fixtures:
            agent_rows.extend(row for row in fixture.logs() if row.get("request_id"))
        mismatches = []
        for agent in agent_rows:
            rid = agent["request_id"]
            requests = [row for row in audit_rows if row.get("phase") == "request" and
                        row.get("request_id") == rid]
            results = [row for row in audit_rows if row.get("phase") == "result" and
                       row.get("request_id") == rid]
            if (len(requests) != 1 or len(results) != 1 or
                    results[0].get("verdict") != agent.get("verdict") or
                    results[0].get("input_sha256") != agent.get("input_sha256")):
                mismatches.append({"request_id": rid, "request_n": len(requests),
                                   "result_n": len(results)})
        report["audit_crosscheck"] = {"agent_request_ids": len(agent_rows),
                                      "mismatches": mismatches}
        check("agent_root_request_id_audit_crosscheck", not mismatches,
              report["audit_crosscheck"])
        report["status"] = "PASS"
    except BaseException as exc:
        report["status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
        raise
    finally:
        report["finished_at"] = utcnow()
        for fixture in fixtures:
            fixture.cleanup()
        for alt in alternates:
            stop_alt(alt[0], alt[1], alt[2])
        report["remaining_broker_containers"] = docker_broker_containers()
        write_json(evidence / "report.json", report)
        (evidence / "summary.json").write_text(json.dumps({
            "run_id": run_id, "status": report["status"],
            "checks": len(report["checks"]),
            "failed_checks": [x for x in report["checks"] if not x["passed"]],
            "normal_metrics": report.get("normal_metrics"),
            "remaining_broker_containers": report["remaining_broker_containers"]},
            indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"report": str(evidence / "report.json"),
                          "status": report["status"]}, indent=2))


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--agent-run", "--agent-raw"):
        return agent_client(sys.argv[1:])
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("setup")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    if args.command == "setup":
        setup_fixture()
    else:
        acceptance(args.execute)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
