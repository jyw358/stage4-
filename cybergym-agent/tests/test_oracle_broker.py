import contextlib
import errno
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import oracle_broker as ob
from tests._scratch import cleanup, scratch_dir


def root_uid():
    return os.geteuid() if os.name == "posix" else 0


class DirectEntryTest(unittest.TestCase):
    def test_isolated_direct_entry_imports_trusted_sibling(self):
        script = Path(ob.__file__).resolve()
        proc = subprocess.run(
            [sys.executable, "-I", "-B", "-c",
             f"import runpy; runpy.run_path({str(script)!r})"],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class ControlFixture(unittest.TestCase):
    def setUp(self):
        self.base = scratch_dir()
        self.addCleanup(cleanup, self.base)
        self.proc = self.base / "proc"
        self.proc.mkdir()
        self.attempt_pid = 4242
        self.peer_pid = 4343
        self.uid = 11001
        self.cgroup = "/stage4/run-arvo_123"
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.run = self.make_run("run-a", "arvo_123", self.uid)
        self.make_cgroup(self.attempt_pid, self.cgroup)
        self.make_cgroup(self.peer_pid, self.cgroup)

    def make_run(self, run_name, task_name, uid, active="1", cgroup=None):
        run = self.base / run_name
        task = run / "control" / task_name
        task.mkdir(parents=True)
        (run / "run_id").write_text("run-" + run_name + "\n", encoding="utf-8")
        values = {
            "run_id": "run-" + run_name,
            "agent_uid": str(uid),
            "active": active,
            "attempt_pid": str(self.attempt_pid),
            "cgroup": self.cgroup if cgroup is None else cgroup,
            "workspace": str(self.workspace),
        }
        for key, value in values.items():
            (task / key).write_text(value + "\n", encoding="utf-8")
        return run

    def make_cgroup(self, pid, cgroup):
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        (directory / "cgroup").write_text("0::" + cgroup + "\n", encoding="utf-8")

    def authorize(self, task="arvo:123", uid=None, pid=None):
        return ob.authorize_request(
            task, self.peer_pid if pid is None else pid,
            self.uid if uid is None else uid, [self.base], self.proc,
            root_uid=root_uid())

    def test_authorizes_matching_live_binding(self):
        binding = self.authorize()
        self.assertEqual(binding.task_id, "arvo:123")
        self.assertEqual(binding.run_id, "run-run-a")
        self.assertEqual(binding.workspace, str(self.workspace))

    def test_duplicate_active_uid_is_refused(self):
        self.make_run("run-b", "arvo_456", self.uid)
        with self.assertRaises(ob.AuthorizationError) as caught:
            self.authorize()
        self.assertEqual(caught.exception.kind, "duplicate_uid")

    def test_inactive_binding_is_refused(self):
        task = self.run / "control" / "arvo_123"
        (task / "active").write_text("0\n", encoding="utf-8")
        with self.assertRaises(ob.AuthorizationError) as caught:
            self.authorize()
        self.assertEqual(caught.exception.kind, "inactive")

    def test_attempt_pid_cgroup_must_exist(self):
        task = self.run / "control" / "arvo_123"
        (task / "attempt_pid").write_text("49999\n", encoding="utf-8")
        with self.assertRaises(ob.AuthorizationError) as caught:
            self.authorize()
        self.assertEqual(caught.exception.kind, "cgroup_unavailable")

    def test_attempt_and_peer_cgroups_must_exactly_match(self):
        self.make_cgroup(self.attempt_pid, "/stage4/other")
        with self.assertRaises(ob.AuthorizationError) as caught:
            self.authorize()
        self.assertEqual(caught.exception.kind, "cgroup_mismatch")
        self.make_cgroup(self.attempt_pid, self.cgroup)
        self.make_cgroup(self.peer_pid, self.cgroup + "/child")
        with self.assertRaises(ob.AuthorizationError) as caught:
            self.authorize()
        self.assertEqual(caught.exception.kind, "cgroup_mismatch")

    def test_v1_or_multiline_proc_cgroup_is_not_v2_proof(self):
        (self.proc / str(self.peer_pid) / "cgroup").write_text(
            "2:cpu:/legacy\n0::" + self.cgroup + "\n", encoding="utf-8")
        with self.assertRaises(ob.AuthorizationError) as caught:
            self.authorize()
        self.assertEqual(caught.exception.kind, "cgroup_mismatch")

    def test_requested_task_must_equal_bound_directory(self):
        with self.assertRaises(ob.AuthorizationError) as caught:
            self.authorize(task="arvo:999")
        self.assertEqual(caught.exception.kind, "task_mismatch")

    def test_run_id_file_is_required_and_must_match(self):
        task = self.run / "control" / "arvo_123"
        (self.run / "run_id").unlink()
        with self.assertRaises(ob.AuthorizationError):
            self.authorize()
        (self.run / "run_id").write_text("different\n", encoding="utf-8")
        with self.assertRaises(ob.AuthorizationError) as caught:
            self.authorize()
        self.assertEqual(caught.exception.kind, "run_id_mismatch")
        (task / "run_id").write_text("run-run-a\n", encoding="utf-8")

    def test_workspace_must_be_absolute(self):
        task = self.run / "control" / "arvo_123"
        (task / "workspace").write_text("relative/workspace\n", encoding="utf-8")
        with self.assertRaises(ob.AuthorizationError) as caught:
            self.authorize()
        self.assertEqual(caught.exception.kind, "workspace_not_absolute")

    def test_missing_control_field_fails_closed(self):
        (self.run / "control" / "arvo_123" / "cgroup").unlink()
        with self.assertRaises(ob.AuthorizationError) as caught:
            self.authorize()
        self.assertEqual(caught.exception.kind, "control_read_failed")


class FakeNode:
    def __init__(self, kind, data=b"", uid=None, mode=None):
        self.kind = kind
        self.data = bytearray(data)
        self.uid = os.geteuid() if os.name == "posix" and uid is None else (uid or 0)
        self.mode = mode
        self.reported_size = None
        self.children = {} if kind == "dir" else None


class FakeSecureFS:
    """Small dir-fd filesystem which exercises the real secure_copy_inputs loop."""

    def __init__(self, workspace, destination):
        self.workspace = str(workspace)
        self.destination = str(destination)
        self.root = FakeNode("dir")
        self.dest = FakeNode("dir")
        self.paths = {self.workspace: self.root, self.destination: self.dest}
        self.fds = {}
        self.next_fd = 100
        self.source_fds = []
        self.fstat_fds = []
        self.read_fds = []
        self.after_source_fstat = None

    def add_file(self, relative, data, kind="file"):
        parts = relative.split("/")
        directory = self.root
        for part in parts[:-1]:
            directory = directory.children.setdefault(part, FakeNode("dir"))
        node = FakeNode(kind, data)
        directory.children[parts[-1]] = node
        return node

    def add_symlink(self, relative):
        return self.add_file(relative, b"", kind="symlink")

    def _stat(self, node):
        if node.kind == "dir":
            mode = stat.S_IFDIR | (node.mode if node.mode is not None else 0o700)
            size = 0
        elif node.kind == "symlink":
            mode = stat.S_IFLNK | 0o777
            size = 0
        else:
            mode = stat.S_IFREG | (node.mode if node.mode is not None else 0o600)
            size = len(node.data) if node.reported_size is None else node.reported_size
        return SimpleNamespace(st_mode=mode, st_uid=node.uid, st_size=size)

    def open(self, path, flags, mode=None, dir_fd=None):
        path = str(path)
        if dir_fd is None:
            node = self.paths.get(path)
            parent = None
            name = None
        else:
            parent = self.fds[dir_fd]["node"]
            name = path
            node = parent.children.get(path)
        creating = bool(flags & os.O_CREAT)
        if node is None and creating and parent is not None:
            node = FakeNode("file")
            parent.children[name] = node
        if node is None:
            raise FileNotFoundError(errno.ENOENT, "fake path absent", path)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        if node.kind == "symlink" and nofollow and flags & nofollow:
            raise OSError(errno.ELOOP, "fake symlink refused", path)
        if directory_flag and flags & directory_flag and node.kind != "dir":
            raise OSError(errno.ENOTDIR, "fake non-directory", path)
        if creating and flags & os.O_EXCL and node.data:
            raise FileExistsError(errno.EEXIST, "fake output exists", path)
        fd = self.next_fd
        self.next_fd += 1
        is_output = creating and parent is self.dest
        self.fds[fd] = {"node": node, "offset": 0, "output": is_output,
                        "path": path, "closed": False}
        if not creating and node.kind == "file" and parent is not self.dest:
            self.source_fds.append(fd)
        return fd

    def fstat(self, fd):
        self.fstat_fds.append(fd)
        result = self._stat(self.fds[fd]["node"])
        if fd in self.source_fds and self.after_source_fstat:
            callback = self.after_source_fstat
            self.after_source_fstat = None
            callback()
        return result

    def read(self, fd, amount):
        entry = self.fds[fd]
        self.read_fds.append(fd)
        node = entry["node"]
        start = entry["offset"]
        chunk = bytes(node.data[start:start + amount])
        entry["offset"] = start + len(chunk)
        return chunk

    def write(self, fd, data):
        entry = self.fds[fd]
        raw = bytes(data)
        entry["node"].data.extend(raw)
        return len(raw)

    def close(self, fd):
        if fd in self.fds:
            self.fds[fd]["closed"] = True

    def output(self, name="in0"):
        return bytes(self.dest.children[name].data)

    @contextlib.contextmanager
    def patched(self):
        with mock.patch.object(ob.os, "O_NOFOLLOW", 0x100000, create=True), \
                mock.patch.object(ob.os, "O_DIRECTORY", 0x200000, create=True), \
                mock.patch.object(ob.os, "open", side_effect=self.open), \
                mock.patch.object(ob.os, "fstat", side_effect=self.fstat), \
                mock.patch.object(ob.os, "read", side_effect=self.read), \
                mock.patch.object(ob.os, "write", side_effect=self.write), \
                mock.patch.object(ob.os, "close", side_effect=self.close):
            yield


class SecureCopyTests(unittest.TestCase):
    def setUp(self):
        self.base = scratch_dir()
        self.addCleanup(cleanup, self.base)
        self.workspace = self.base / "ws"
        self.workspace.mkdir()
        self.dest = self.base / "private"
        self.fs = FakeSecureFS(self.workspace, self.dest)
        self.binding = {"workspace": str(self.workspace),
                        "agent_uid": root_uid()}

    def copy(self, inputs):
        with self.fs.patched():
            return ob.secure_copy_inputs(self.binding, inputs, self.dest)

    def test_fd_copy_hash_and_bytes_include_nested_traversal(self):
        self.fs.add_file("nested/poc", b"oracle-input")
        records = self.copy(["nested/poc"])
        self.assertEqual(self.fs.output(), b"oracle-input")
        self.assertEqual(records[0]["size"], 12)
        self.assertEqual(records[0]["sha256"],
                         hashlib.sha256(b"oracle-input").hexdigest())
        source_fd = self.fs.source_fds[-1]
        self.assertIn(source_fd, self.fs.fstat_fds)
        self.assertIn(source_fd, self.fs.read_fds)

    def test_intermediate_symlink_is_refused(self):
        self.fs.root.children["linked"] = FakeNode("symlink")
        with self.assertRaises(ob.InputError):
            self.copy(["linked/poc"])

    def test_final_symlink_is_refused(self):
        self.fs.add_symlink("poc")
        with self.assertRaises(ob.InputError):
            self.copy(["poc"])

    def test_toctou_path_replacement_cannot_change_open_fd(self):
        original = self.fs.add_file("poc", b"original")

        def replace_after_open():
            self.fs.root.children["poc"] = FakeNode("file", b"attacker")

        self.fs.after_source_fstat = replace_after_open
        records = self.copy(["poc"])
        self.assertEqual(self.fs.output(), b"original")
        self.assertEqual(records[0]["sha256"], hashlib.sha256(b"original").hexdigest())
        source_fd = self.fs.source_fds[-1]
        self.assertIs(self.fs.fds[source_fd]["node"], original)
        self.assertTrue(all(fd == source_fd for fd in self.fs.read_fds))

    def test_fstat_size_and_growth_during_read_both_fail_closed(self):
        node = self.fs.add_file("poc", b"x")
        node.reported_size = ob.MAX_INPUT_BYTES + 1
        with self.assertRaises(ob.InputError) as caught:
            self.copy(["poc"])
        self.assertEqual(caught.exception.kind, "input_too_large")

        fresh = self.make_fresh_fs()
        growing = fresh.add_file("poc", b"x")
        growing.reported_size = 1

        def grow():
            growing.data = bytearray(b"z" * (ob.MAX_INPUT_BYTES + 1))
            growing.reported_size = None

        fresh.after_source_fstat = grow
        with fresh.patched(), self.assertRaises(ob.InputError) as caught:
            ob.secure_copy_inputs(self.binding, ["poc"], self.dest)
        self.assertEqual(caught.exception.kind, "input_too_large")

    def make_fresh_fs(self):
        self.fs = FakeSecureFS(self.workspace, self.dest)
        return self.fs

    def test_unsafe_paths_are_refused_before_open(self):
        for path in ("", "/etc/passwd", "../x", "a/../x", "a//x", "C:/x", "a\\x"):
            with self.subTest(path=path), self.assertRaises(ob.InputError):
                self.copy([path])


class RequestValidationTests(unittest.TestCase):
    def setUp(self):
        self.broker = ob.OracleBroker()
        self.request = {"op": "run_vul_candidate", "task_id": "arvo:123",
                        "inputs": [], "timeout": 10, "perturb": None, "tag": "t-1"}

    def test_unknown_field_and_fix_side_injection_are_refused(self):
        for key, value in (("side", "fix"), ("info", ["fix"]), ("extra", 1)):
            request = dict(self.request, **{key: value})
            response = self.broker.handle_request(request, 10, 20, 30)
            self.assertEqual(response["verdict"], "ORACLE_ERROR")
            self.assertEqual(response["runner_error_kind"], "broker_refused")
            self.assertEqual(response["refusal_reason"], "unknown_field")
            self.assertEqual(response["input_sha256"], [])

    def test_timeout_perturb_tag_and_bool_validation(self):
        for key, value in (("timeout", 0), ("timeout", 31), ("timeout", True),
                           ("perturb", -1), ("perturb", 256), ("perturb", True),
                           ("tag", ""), ("tag", "bad tag"), ("tag", "x" * 41)):
            response = self.broker.handle_request(dict(self.request, **{key: value}),
                                                  10, 20)
            self.assertEqual(response["verdict"], "ORACLE_ERROR", (key, value))

    def test_version_round_trip_schema(self):
        response = self.broker.handle_request({"op": "version"}, 1, 2, 3)
        self.assertEqual(response["broker_version"], ob.BROKER_VERSION)
        self.assertTrue(response["request_id"])
        response = self.broker.handle_request({"op": "version", "side": "fix"}, 1, 2)
        self.assertEqual(response["runner_error_kind"], "broker_refused")
        self.assertEqual(response["refusal_reason"], "unknown_field")

    def test_authorization_refusal_is_distinct_from_execution_failure(self):
        response = self.broker.handle_request(self.request, 10, 20)
        self.assertEqual(response["runner_error_kind"], "broker_refused")
        self.assertEqual(response["refusal_reason"], "uid_mismatch")

    def test_busy_is_schema2_oracle_error(self):
        broker = ob.OracleBroker(max_global=0)
        response = broker.handle_request(self.request, 10, 20)
        self.assertEqual(response["verdict"], "ORACLE_ERROR")
        self.assertEqual(response["runner_error_kind"], "oracle_busy")
        self.assertNotIn("refusal_reason", response)

    def test_default_socket_matches_local_oracle_client(self):
        self.assertEqual(ob.DEFAULT_SOCKET_PATH, "/run/cybergym-oracle/broker.sock")


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.base = scratch_dir()
        self.addCleanup(cleanup, self.base)
        self.binding = ob.TaskBinding(
            run_root=str(self.base), control_dir=str(self.base / "control"),
            task_name="arvo_123", task_id="arvo:123", run_id="run-a",
            agent_uid=20, attempt_pid=11, cgroup="/stage4/a",
            workspace=str(self.base))
        self.request = {"op": "run_vul_candidate", "task_id": "arvo:123",
                        "inputs": ["poc"], "timeout": 3, "perturb": 17,
                        "tag": "execution"}
        self.info = (Path("/data/arvo/123/vul"), "fuzz_target",
                     "detect_leaks=0", "arvo")

    def fake_copy(self, _binding, inputs, dest):
        records = []
        for index, value in enumerate(inputs):
            data = b"input-" + str(index).encode()
            path = Path(dest) / f"in{index}"
            path.write_bytes(data)
            records.append({"input": value, "name": f"in{index}",
                            "path": str(path), "size": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()})
        return records

    def execute(self, run_result=None, audit=False):
        audit_path = self.base / "audit.jsonl" if audit else None
        broker = ob.OracleBroker(audit_path=audit_path,
                                 broker_instance="broker-test")
        if run_result is None:
            stdout = (ob.RUNNER_SENTINEL +
                      "\n__ORACLE_TARGET_EXIT_V2__0\n").encode()
            run_result = SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")
        with mock.patch.object(broker, "authorize", return_value=self.binding), \
                mock.patch.object(ob, "secure_copy_inputs", side_effect=self.fake_copy), \
                mock.patch.object(ob, "side_info", return_value=self.info) as info, \
                mock.patch.object(broker, "reap_orphans", return_value=[]), \
                mock.patch.object(ob._local_oracle, "sweep_preflight",
                                  side_effect=AssertionError("global sweep forbidden")), \
                mock.patch.object(ob.subprocess, "run", return_value=run_result) as run:
            response = broker.handle_audited_request(self.request, 11, 20, 30)
        return broker, response, run, info

    def test_docker_command_schema2_hash_and_root_side_info(self):
        broker, response, run, info = self.execute()
        self.assertEqual(response["verdict"], "CLEAN")
        self.assertEqual(response["schema"], 2)
        self.assertEqual(response["input_sha256"],
                         [hashlib.sha256(b"input-0").hexdigest()])
        info.assert_called_once_with("arvo:123", "vul")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:2], ["docker", "run"])
        for args in (["--network", "none"], ["--cap-drop", "ALL"],
                     ["--security-opt", "no-new-privileges"],
                     ["--pids-limit", "256"]):
            start = cmd.index(args[0])
            self.assertEqual(cmd[start:start + 2], args)
        self.assertIn("--rm", cmd)
        self.assertNotIn("--privileged", cmd)
        labels = [cmd[i + 1] for i, value in enumerate(cmd) if value == "--label"]
        self.assertIn(ob.CONTAINER_LABEL + "=1", labels)
        self.assertIn(ob.LABEL_PREFIX + "broker_instance=broker-test", labels)
        self.assertIn(ob.LABEL_PREFIX + "run_id=run-a", labels)
        self.assertIn(ob.LABEL_PREFIX + "task_id=arvo:123", labels)
        self.assertTrue(any(x.startswith(ob.LABEL_PREFIX + "request_id=")
                            for x in labels))
        inner = cmd[-1]
        self.assertIn("timeout -s SIGKILL 3 /out/fuzz_target /poc/in0", inner)
        self.assertIn("MALLOC_PERTURB_=17", inner)
        self.assertIn("__ORACLE_TARGET_EXIT_V2__$?", inner)
        self.assertEqual(broker.registry.snapshot(), {})
        self.assertFalse(Path(response["container"]).exists())

    def test_audit_has_identity_hash_container_verdict_cleanup(self):
        _broker, response, _run, _info = self.execute(audit=True)
        rows = [json.loads(line) for line in
                (self.base / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["phase"] for row in rows], ["request", "result"])
        self.assertEqual(rows[0]["request_id"], response["request_id"])
        row = rows[-1]
        self.assertEqual(row["uid"], 20)
        self.assertEqual(row["run_id"], "run-a")
        self.assertEqual(row["task_id"], "arvo:123")
        self.assertEqual(row["request_id"], response["request_id"])
        self.assertEqual(row["input_sha256"], response["input_sha256"])
        self.assertEqual(row["container"], response["container"])
        self.assertEqual(row["verdict"], "CLEAN")
        self.assertIn("cleanup", row)

    def test_authorized_input_error_preserves_identity_in_response_and_audit(self):
        audit_path = self.base / "input-error-audit.jsonl"
        broker = ob.OracleBroker(audit_path=audit_path,
                                 broker_instance="broker-test")
        failure = ob.InputError("symlink refused", "bad_inputs")
        failure.input_sha256 = ["partial-hash"]
        with mock.patch.object(broker, "authorize", return_value=self.binding), \
                mock.patch.object(ob, "side_info", return_value=self.info), \
                mock.patch.object(broker, "reap_orphans", return_value=[]), \
                mock.patch.object(ob, "secure_copy_inputs", side_effect=failure), \
                mock.patch.object(ob.subprocess, "run") as run:
            response = broker.handle_audited_request(self.request, 11, 20, 30)
        self.assertEqual(response["verdict"], "INPUT_ERROR")
        self.assertEqual(response["run_id"], "run-a")
        self.assertEqual(response["task_id"], "arvo:123")
        self.assertEqual(response["input_sha256"], ["partial-hash"])
        run.assert_not_called()
        rows = [json.loads(line) for line in
                audit_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["phase"] for row in rows], ["request", "result"])
        row = rows[-1]
        self.assertEqual(row["run_id"], "run-a")
        self.assertEqual(row["task_id"], "arvo:123")
        self.assertEqual(row["input_sha256"], ["partial-hash"])

    def test_request_audit_failure_refuses_before_execution(self):
        broker = ob.OracleBroker(audit_path=self.base / "audit.jsonl")
        with mock.patch.object(broker, "_audit",
                               side_effect=ob.BrokerError("disk full", "audit_failed")), \
                mock.patch.object(broker, "handle_request") as handle:
            response = broker.handle_audited_request(self.request, 11, 20, 30)
        self.assertEqual(response["verdict"], "ORACLE_ERROR")
        self.assertEqual(response["runner_error_kind"], "audit_failed")
        handle.assert_not_called()

    def test_result_audit_failure_never_exposes_clean(self):
        broker = ob.OracleBroker(audit_path=self.base / "audit.jsonl")
        clean = {"schema": 2, "verdict": "CLEAN", "request_id": "req",
                 "run_id": "run-a", "task_id": "arvo:123",
                 "input_sha256": ["abc"], "container": "container-A",
                 "cleanup": None}
        with mock.patch.object(broker, "_audit",
                               side_effect=[None, ob.BrokerError("disk full", "audit_failed")]), \
                mock.patch.object(broker, "handle_request", return_value=clean):
            response = broker.handle_audited_request(self.request, 11, 20, 30)
        self.assertEqual(response["verdict"], "ORACLE_ERROR")
        self.assertEqual(response["runner_error_kind"], "audit_failed")
        self.assertEqual(response["unaudited_verdict"], "CLEAN")
        self.assertEqual(response["container"], "container-A")

    def test_incomplete_scoped_preflight_blocks_docker_run(self):
        broker = ob.OracleBroker(broker_instance="broker-test")
        incomplete = [{"id": None, "name": None,
                       "cleanup": "inspection_incomplete"}]
        with mock.patch.object(broker, "authorize", return_value=self.binding), \
                mock.patch.object(ob, "side_info", return_value=self.info), \
                mock.patch.object(broker, "reap_orphans", return_value=incomplete), \
                mock.patch.object(ob.subprocess, "run") as run, \
                mock.patch.object(ob, "secure_copy_inputs") as copy:
            response = broker.handle_request(self.request, 11, 20)
        self.assertEqual(response["verdict"], "ORACLE_ERROR")
        self.assertEqual(response["runner_error_kind"], "sweep_preflight_failed")
        self.assertEqual(response["orphan_cleanup"], incomplete)
        self.assertEqual(response["run_id"], "run-a")
        run.assert_not_called()
        copy.assert_not_called()

    def test_timeout_kills_only_the_exact_request_container(self):
        broker = ob.OracleBroker(broker_instance="broker-test")
        broker.registry.add({"container": "container-B", "broker_instance": "broker-test",
                             "run_id": "run-b", "task_id": "arvo:456",
                             "request_id": "request-B"})
        with mock.patch.object(broker, "authorize", return_value=self.binding), \
                mock.patch.object(ob, "secure_copy_inputs", side_effect=self.fake_copy), \
                mock.patch.object(ob, "side_info", return_value=self.info), \
                mock.patch.object(broker, "reap_orphans", return_value=[]), \
                mock.patch.object(ob.subprocess, "run",
                                  side_effect=ob.subprocess.TimeoutExpired("docker", 1)), \
                mock.patch.object(ob, "_kill_and_verify",
                                  return_value=("cleanup_verified", 0)) as kill:
            response = broker.handle_request(self.request, 11, 20)
        self.assertEqual(response["runner_error_kind"], "runner_hang")
        kill.assert_called_once_with(response["container"])
        self.assertNotEqual(response["container"], "container-B")
        self.assertIn("container-B", broker.registry.snapshot())


class ReapOrphanTests(unittest.TestCase):
    def labels(self, instance="broker-test"):
        return {ob.CONTAINER_LABEL: "1",
                ob.LABEL_PREFIX + "broker_instance": instance,
                ob.LABEL_PREFIX + "run_id": "run-a",
                ob.LABEL_PREFIX + "task_id": "arvo:123",
                ob.LABEL_PREFIX + "request_id": "req-a"}

    def inspect_result(self, name, labels):
        value = [{"Name": "/" + name, "Config": {"Labels": labels}}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))

    def test_reap_does_not_touch_other_active_task(self):
        broker = ob.OracleBroker(broker_instance="broker-test")
        broker.registry.add({"container": "container-B", "broker_instance": "broker-test",
                             "run_id": "run-b", "task_id": "arvo:456",
                             "request_id": "request-B"})
        listing = SimpleNamespace(returncode=0,
                                  stdout="aaa\tcontainer-A\nbbb\tcontainer-B\n")

        def docker(cmd, **_kwargs):
            if cmd[:2] == ["docker", "ps"]:
                return listing
            if cmd[:2] == ["docker", "inspect"] and cmd[-1] == "aaa":
                return self.inspect_result("container-A", self.labels())
            raise AssertionError("active B must not even need inspection: " + repr(cmd))

        with mock.patch.object(ob.subprocess, "run", side_effect=docker), \
                mock.patch.object(ob, "_kill_and_verify",
                                  return_value=("cleanup_verified", 0)) as kill:
            result = broker.reap_orphans()
        kill.assert_called_once_with("container-A")
        self.assertEqual(result[0]["name"], "container-A")
        self.assertIn("container-B", broker.registry.snapshot())

    def test_failed_inspection_is_incomplete_and_never_killed(self):
        broker = ob.OracleBroker(broker_instance="broker-test")
        listing = SimpleNamespace(returncode=0, stdout="aaa\tcontainer-A\n")
        with mock.patch.object(ob.subprocess, "run", side_effect=[listing,
                               SimpleNamespace(returncode=1, stdout="")]), \
                mock.patch.object(ob, "_kill_and_verify") as kill:
            result = broker.reap_orphans()
        kill.assert_not_called()
        self.assertEqual(result[0]["cleanup"], "inspection_incomplete")


class _ClientAdapter:
    def __init__(self, conn):
        self.conn = conn

    def settimeout(self, value):
        self.conn.settimeout(value)

    def connect(self, _path):
        pass

    def sendall(self, data):
        self.conn.sendall(data)

    def recv(self, amount):
        return self.conn.recv(amount)

    def close(self):
        self.conn.close()


class ProtocolRoundTripTests(unittest.TestCase):
    def test_call_and_connection_json_lines_round_trip(self):
        if not hasattr(socket, "socketpair"):
            self.skipTest("socketpair unavailable")
        server_conn, client_conn = socket.socketpair()
        broker = ob.OracleBroker()
        thread = threading.Thread(target=broker.handle_connection,
                                  args=(server_conn,), kwargs={"peer": (55, 66, 77)})
        thread.start()
        adapter = _ClientAdapter(client_conn)
        with mock.patch.object(ob.socket, "AF_UNIX", 1, create=True), \
                mock.patch.object(ob.socket, "socket", return_value=adapter):
            response = ob.call({"op": "version"}, "unused-test-path", timeout=5)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(response["op"], "version")
        self.assertEqual(response["broker_version"], ob.BROKER_VERSION)
        self.assertTrue(response["request_id"])


if __name__ == "__main__":
    unittest.main()
