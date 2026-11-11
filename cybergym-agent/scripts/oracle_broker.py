#!/usr/bin/env python3
"""Root-only, SO_PEERCRED-authorised broker for the vulnerability oracle.

Agents never need access to the Docker socket.  They send one JSON-lines
request per Unix connection; this process re-reads the root-owned control
plane for every request and executes only the vulnerability side.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import ntpath
import os
import pathlib
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass

try:  # package import (tests and local_oracle client)
    from . import local_oracle as _local_oracle
except ImportError:  # direct execution: python scripts/oracle_broker.py
    # Python -I deliberately removes the script directory.  Add back only the
    # trusted sibling directory checked by systemd ExecStartPre, never cwd or
    # an Agent-controlled PYTHONPATH.
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import local_oracle as _local_oracle

side_info = _local_oracle.side_info
parse_execution = _local_oracle.parse_execution
classify_exec = _local_oracle.classify_exec
runner_error_kind = _local_oracle.runner_error_kind
signature = _local_oracle.signature
_kill_and_verify = _local_oracle._kill_and_verify
_post_abnormal_cleanup = _local_oracle._post_abnormal_cleanup

BROKER_VERSION = "k3-oracle-broker/1"
DEFAULT_SOCKET_PATH = "/run/cybergym-oracle/broker.sock"
RUNNER = _local_oracle.RUNNER
RUNNER_SENTINEL = _local_oracle.RUNNER_SENTINEL
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_INPUTS = 8
MAX_INPUT_BYTES = 1024 * 1024
MAX_TOTAL_INPUT_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT = 10
MAX_TIMEOUT = 30
MAX_GLOBAL_CONTAINERS = 4
CONTAINER_LABEL = "cybergym.oracle_broker"
LABEL_PREFIX = CONTAINER_LABEL + "."
LABEL_KEYS = ("broker_instance", "run_id", "task_id", "request_id")
CONTROL_FIELDS = ("run_id", "agent_uid", "active", "attempt_pid", "cgroup",
                  "workspace")
_TASK_RE = re.compile(r"^(arvo|oss-fuzz):([0-9]+)$")
_TASK_DIR_RE = re.compile(r"^(arvo|oss-fuzz)_([0-9]+)$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_REQUEST_FIELDS = {"op", "task_id", "inputs", "timeout", "perturb", "tag"}


class BrokerError(Exception):
    """A refused request or a client/server protocol failure."""

    def __init__(self, message: str, kind: str = "broker_error"):
        super().__init__(message)
        self.kind = kind


class RequestError(BrokerError):
    pass


class AuthorizationError(BrokerError):
    pass


class InputError(BrokerError):
    def __init__(self, message: str, kind: str = "bad_inputs"):
        super().__init__(message, kind)
        self.input_sha256 = []


@dataclass(frozen=True)
class TaskBinding:
    run_root: str
    control_dir: str
    task_name: str
    task_id: str
    run_id: str
    agent_uid: int
    attempt_pid: int
    cgroup: str
    workspace: str

    def __getitem__(self, key):  # convenient for callers using mapping bindings
        return getattr(self, key)


def task_dir_name(task_id: str) -> str:
    if not isinstance(task_id, str):
        raise RequestError("task_id must be a string", "invalid_task")
    match = _TASK_RE.fullmatch(task_id)
    if not match:
        raise RequestError("task_id must be arvo:N or oss-fuzz:N", "invalid_task")
    return match.group(1) + "_" + match.group(2)


def _task_id_from_name(name: str) -> str:
    match = _TASK_DIR_RE.fullmatch(name)
    if not match:
        raise AuthorizationError("invalid control task name", "control_invalid")
    return match.group(1) + ":" + match.group(2)


def _trusted_dir(path: pathlib.Path, root_uid: int = 0) -> None:
    try:
        st = os.lstat(str(path))
    except OSError as exc:
        raise AuthorizationError(f"cannot inspect control directory {path}: {exc}",
                                 "control_read_failed")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise AuthorizationError(f"control path is not a real directory: {path}",
                                 "control_untrusted")
    if os.name == "posix":
        if st.st_uid != root_uid:
            raise AuthorizationError(f"control directory is not root-owned: {path}",
                                     "control_untrusted")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise AuthorizationError(f"control directory is group/other-writable: {path}",
                                     "control_untrusted")


def _read_regular(path: pathlib.Path, root_uid: int = 0,
                  max_bytes: int = 65536, control: bool = True) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        kind = "control_read_failed" if control else "cgroup_unavailable"
        raise AuthorizationError(f"cannot read {path}: {exc}", kind)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AuthorizationError(f"not a regular file: {path}",
                                     "control_read_failed" if control else
                                     "cgroup_unavailable")
        if os.name == "posix" and control:
            if st.st_uid != root_uid or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise AuthorizationError(f"untrusted control file: {path}",
                                         "control_untrusted")
        if st.st_size > max_bytes:
            raise AuthorizationError(f"control file too large: {path}",
                                     "control_invalid")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise AuthorizationError(f"control file too large: {path}",
                                         "control_invalid")
        try:
            return b"".join(chunks).decode("utf-8").strip()
        except UnicodeDecodeError:
            raise AuthorizationError(f"non-UTF-8 control file: {path}",
                                     "control_invalid")
    except AuthorizationError:
        raise
    except OSError as exc:
        raise AuthorizationError(f"cannot read {path}: {exc}",
                                 "control_read_failed" if control else
                                 "cgroup_unavailable")
    finally:
        os.close(fd)


def _candidate_runs(run_roots) -> list:
    candidates = []
    seen = set()
    for configured in run_roots:
        root = pathlib.Path(configured)
        possible = []
        if os.path.lexists(str(root / "control")):
            possible.append(root)
        try:
            with os.scandir(str(root)) as entries:
                for entry in entries:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError as exc:
                        raise AuthorizationError(
                            f"cannot scan run root {root}: {exc}",
                            "control_read_failed")
                    child = root / entry.name
                    if os.path.lexists(str(child / "control")):
                        possible.append(child)
        except AuthorizationError:
            raise
        except OSError as exc:
            raise AuthorizationError(f"cannot scan run root {root}: {exc}",
                                     "control_read_failed")
        for run in possible:
            key = os.path.normcase(os.path.abspath(str(run)))
            if key not in seen:
                seen.add(key)
                candidates.append(run)
    return candidates


def _positive_int(value: str, field: str) -> int:
    if not isinstance(value, str) or not value.isdigit():
        raise AuthorizationError(f"invalid {field}", "control_invalid")
    result = int(value)
    if result <= 0:
        raise AuthorizationError(f"invalid {field}", "control_invalid")
    return result


def _check_cgroup(proc_root: pathlib.Path, pid: int, expected: str,
                  which: str) -> None:
    path = proc_root / str(pid) / "cgroup"
    text = _read_regular(path, control=False)
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0].startswith("0::"):
        raise AuthorizationError(f"{which} is not on a single cgroup-v2 path",
                                 "cgroup_mismatch")
    if lines[0][3:] != expected:
        raise AuthorizationError(
            f"{which} cgroup does not exactly match the control binding",
            "cgroup_mismatch")


def authorize_request(task_id: str, peer_pid: int, peer_uid: int, run_roots,
                      proc_root="/proc", root_uid: int = 0) -> TaskBinding:
    """Re-authorise one peer from the current root-owned control plane.

    ``proc_root``, ``run_roots`` and ``root_uid`` are injectable so the exact
    production checks can be exercised with temporary fixtures on any OS.
    """
    expected_name = task_dir_name(task_id)
    if type(peer_pid) is not int or peer_pid <= 0:
        raise AuthorizationError("invalid peer pid", "invalid_peer")
    if type(peer_uid) is not int or peer_uid < 0:
        raise AuthorizationError("invalid peer uid", "invalid_peer")
    proc_root = pathlib.Path(proc_root)
    matching = []
    requested_active = False
    requested_seen = False

    for run in _candidate_runs(run_roots):
        _trusted_dir(run, root_uid)
        control = run / "control"
        _trusted_dir(control, root_uid)
        try:
            entries = list(os.scandir(str(control)))
        except OSError as exc:
            raise AuthorizationError(f"cannot scan {control}: {exc}",
                                     "control_read_failed")
        for entry in entries:
            if not _TASK_DIR_RE.fullmatch(entry.name):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError as exc:
                raise AuthorizationError(f"cannot inspect {entry.path}: {exc}",
                                         "control_read_failed")
            task_dir = control / entry.name
            _trusted_dir(task_dir, root_uid)
            if entry.name == expected_name:
                requested_seen = True
            active_path = task_dir / "active"
            if not os.path.lexists(str(active_path)):
                continue
            active = _read_regular(active_path, root_uid)
            if active == "0":
                continue
            if active != "1":
                raise AuthorizationError("invalid active flag", "control_invalid")
            uid = _positive_int(_read_regular(task_dir / "agent_uid", root_uid),
                                "agent_uid")
            if entry.name == expected_name:
                requested_active = True
            if uid != peer_uid:
                continue

            values = {key: _read_regular(task_dir / key, root_uid)
                      for key in CONTROL_FIELDS}
            if values["active"] != "1":
                raise AuthorizationError("binding changed while being read",
                                         "binding_changed")
            # Re-read identity alongside the live fields; a changed run is not
            # accepted merely because the first scan saw the right active flag.
            if _read_regular(task_dir / "agent_uid", root_uid) != str(peer_uid):
                raise AuthorizationError("binding UID changed while being read",
                                         "binding_changed")
            run_file_id = _read_regular(run / "run_id", root_uid)
            if not values["run_id"] or values["run_id"] != run_file_id:
                raise AuthorizationError("control/run_id mismatch", "run_id_mismatch")
            attempt_pid = _positive_int(values["attempt_pid"], "attempt_pid")
            if not values["cgroup"].startswith("/") or "\x00" in values["cgroup"]:
                raise AuthorizationError("invalid cgroup-v2 path", "control_invalid")
            if not os.path.isabs(values["workspace"]):
                raise AuthorizationError("workspace must be absolute",
                                         "workspace_not_absolute")
            name = entry.name
            matching.append(TaskBinding(
                run_root=str(run), control_dir=str(task_dir), task_name=name,
                task_id=_task_id_from_name(name), run_id=values["run_id"],
                agent_uid=peer_uid, attempt_pid=attempt_pid,
                cgroup=values["cgroup"], workspace=values["workspace"]))

    if len(matching) > 1:
        raise AuthorizationError("UID has more than one active task binding",
                                 "duplicate_uid")
    if not matching:
        if requested_seen and not requested_active:
            raise AuthorizationError("task is not active", "inactive")
        raise AuthorizationError("no active binding for this UID", "uid_mismatch")
    binding = matching[0]
    if binding.task_name != expected_name or binding.task_id != task_id:
        raise AuthorizationError("requested task does not match the UID binding",
                                 "task_mismatch")
    _check_cgroup(proc_root, binding.attempt_pid, binding.cgroup, "attempt process")
    _check_cgroup(proc_root, peer_pid, binding.cgroup, "peer process")
    return binding


# Descriptive alias for integrations which prefer the binding terminology.
authorize_binding = authorize_request


def validate_run_request(request) -> dict:
    if not isinstance(request, dict):
        raise RequestError("request must be a JSON object", "invalid_request")
    unknown = set(request) - _REQUEST_FIELDS
    if unknown:
        raise RequestError("unknown request fields: " + ",".join(sorted(map(str, unknown))),
                           "unknown_field")
    if request.get("op") != "run_vul_candidate":
        raise RequestError("unsupported operation", "invalid_op")
    if set(request) - {"op", "task_id", "inputs", "timeout", "perturb", "tag"}:
        raise RequestError("invalid request schema", "invalid_request")
    for required in ("task_id", "inputs"):
        if required not in request:
            raise RequestError(f"missing {required}", "missing_field")
    task_dir_name(request["task_id"])
    inputs = request["inputs"]
    if not isinstance(inputs, list) or len(inputs) > MAX_INPUTS:
        raise RequestError("inputs must be a list of at most 8 paths", "bad_inputs")
    for value in inputs:
        _validate_relative_input(value)
    timeout = request.get("timeout", DEFAULT_TIMEOUT)
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT:
        raise RequestError("timeout must be an integer in 1..30", "bad_timeout")
    perturb = request.get("perturb")
    if perturb is not None and (type(perturb) is not int or not 0 <= perturb <= 255):
        raise RequestError("perturb must be null or an integer in 0..255",
                           "bad_perturb")
    tag = request.get("tag", "")
    if (not isinstance(tag, str) or
            ("tag" in request and not _TAG_RE.fullmatch(tag))):
        raise RequestError("tag does not match [A-Za-z0-9_.-]{1,40}", "bad_tag")
    return {"op": "run_vul_candidate", "task_id": request["task_id"],
            "inputs": list(inputs), "timeout": timeout, "perturb": perturb,
            "tag": tag}


def _validate_relative_input(value) -> tuple:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RequestError("input paths must be non-empty strings", "bad_inputs")
    if "\\" in value or value.startswith("/") or ntpath.isabs(value):
        raise RequestError(f"input path is not a safe relative path: {value}",
                           "bad_inputs")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise RequestError(f"input path contains a forbidden component: {value}",
                           "bad_inputs")
    return tuple(parts)


def _prepare_private_dir(dest_dir) -> int:
    try:
        os.makedirs(str(dest_dir), mode=0o700, exist_ok=True)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(dest_dir), flags)
    except OSError as exc:
        raise InputError(f"cannot open private input directory: {exc}",
                         "input_tempdir_failed")
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise InputError("input destination is not a directory",
                             "input_tempdir_failed")
        if os.name == "posix":
            if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
                raise InputError("input destination is not root-private 0700",
                                 "input_tempdir_failed")
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_workspace(binding) -> int:
    workspace = binding["workspace"] if isinstance(binding, dict) else binding.workspace
    agent_uid = binding["agent_uid"] if isinstance(binding, dict) else binding.agent_uid
    if os.name == "posix" and (not hasattr(os, "O_DIRECTORY") or
                               not hasattr(os, "O_NOFOLLOW")):
        raise InputError("secure directory open is unavailable", "secure_open_unavailable")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(workspace, flags)
    except OSError as exc:
        raise InputError(f"cannot securely open workspace: {exc}", "workspace_open_failed")
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise InputError("workspace is not a real directory", "workspace_untrusted")
        if os.name == "posix" and (st.st_uid != agent_uid or
                                   stat.S_IMODE(st.st_mode) != 0o700):
            raise InputError("workspace owner/mode must be agent_uid and 0700",
                             "workspace_untrusted")
        return fd
    except Exception:
        os.close(fd)
        raise


def _copy_one_input(root_fd: int, dest_fd: int, value: str, index: int,
                    remaining: int) -> dict:
    parts = _validate_relative_input(value)
    parent_fd = root_fd
    file_fd = None
    out_fd = None
    output_name = f"in{index}"
    try:
        for component in parts[:-1]:
            flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                     getattr(os, "O_NOFOLLOW", 0))
            next_fd = os.open(component, flags, dir_fd=parent_fd)
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = next_fd
            st = os.fstat(parent_fd)
            if not stat.S_ISDIR(st.st_mode):
                raise InputError(f"non-directory input component: {value}", "bad_inputs")
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                 getattr(os, "O_NONBLOCK", 0))
        file_fd = os.open(parts[-1], flags, dir_fd=parent_fd)
        st = os.fstat(file_fd)
        if not stat.S_ISREG(st.st_mode):
            raise InputError(f"input is not a regular file: {value}", "bad_inputs")
        if st.st_size < 0 or st.st_size > MAX_INPUT_BYTES or st.st_size > remaining:
            raise InputError(f"input exceeds the size limit: {value}", "input_too_large")

        out_flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0))
        out_fd = os.open(output_name, out_flags, 0o600, dir_fd=dest_fd)
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(file_fd, min(65536, remaining - copied + 1,
                                         MAX_INPUT_BYTES - copied + 1))
            if not chunk:
                break
            copied += len(chunk)
            if copied > MAX_INPUT_BYTES or copied > remaining:
                raise InputError(f"input grew past the size limit: {value}",
                                 "input_too_large")
            view = memoryview(chunk)
            while view:
                written = os.write(out_fd, view)
                if written <= 0:
                    raise OSError("short write to private input")
                view = view[written:]
            digest.update(chunk)
        return {"input": value, "name": output_name, "size": copied,
                "sha256": digest.hexdigest()}
    except RequestError as exc:
        raise InputError(str(exc), exc.kind)
    except InputError:
        raise
    except OSError as exc:
        raise InputError(f"secure input open/read failed for {value}: {exc}",
                         "bad_inputs")
    finally:
        if out_fd is not None:
            os.close(out_fd)
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd != root_fd:
            os.close(parent_fd)


def secure_copy_inputs(binding, inputs, dest_dir) -> list:
    """Copy inputs using only no-follow dir-fd traversal and verified source FDs.

    The returned hashes are computed from the bytes actually written to the
    root-private destination.  Any failure removes the whole partial input set.
    """
    if not isinstance(inputs, list) or len(inputs) > MAX_INPUTS:
        raise InputError("at most 8 input paths are accepted", "bad_inputs")
    for value in inputs:
        try:
            _validate_relative_input(value)
        except RequestError as exc:
            raise InputError(str(exc), exc.kind)
    root_fd = _open_workspace(binding)
    dest_fd = None
    records = []
    total = 0
    try:
        dest_fd = _prepare_private_dir(dest_dir)
        for index, value in enumerate(inputs):
            record = _copy_one_input(root_fd, dest_fd, value, index,
                                     MAX_TOTAL_INPUT_BYTES - total)
            records.append(record)
            total += record["size"]
            if total > MAX_TOTAL_INPUT_BYTES:
                raise InputError("total input size exceeds 4 MiB", "input_too_large")
            record["path"] = str(pathlib.Path(dest_dir) / record["name"])
        return records
    except InputError as exc:
        exc.input_sha256 = [record["sha256"] for record in records]
        for record in records:
            try:
                os.unlink(str(pathlib.Path(dest_dir) / record["name"]))
            except OSError:
                pass
        # The current output may not yet be in records.
        try:
            os.unlink(str(pathlib.Path(dest_dir) / f"in{len(records)}"))
        except OSError:
            pass
        raise
    except OSError as exc:
        for record in records:
            try:
                os.unlink(str(pathlib.Path(dest_dir) / record["name"]))
            except OSError:
                pass
        raise InputError(f"private input copy failed: {exc}", "bad_inputs")
    finally:
        if dest_fd is not None:
            os.close(dest_fd)
        os.close(root_fd)


def _response_error(request_id: str, kind: str, message: str,
                    verdict: str = "ORACLE_ERROR", input_sha256=None) -> dict:
    return {"schema": 2, "verdict": verdict, "request_id": request_id,
            "broker_version": BROKER_VERSION, "runner_started": None,
            "target_started": None, "runner_exit_code": None,
            "target_exit_code": None, "runner_error_kind": kind,
            "sanitizer_detected": False, "rc": -1, "sig": "",
            "tail": str(message)[:1500], "cleanup": None, "container": None,
            "input_sha256": list(input_sha256 or [])}


def _refusal_error(request_id: str, exc: BrokerError) -> dict:
    response = _response_error(request_id, "broker_refused", str(exc))
    response["refusal_reason"] = exc.kind
    return response


def _as_text(value) -> str:
    if isinstance(value, str):
        return value
    return (value or b"").decode("utf-8", errors="replace")


class ContainerRegistry:
    """Thread-safe live container identities; never an age-based sweep list."""

    def __init__(self):
        self._lock = threading.RLock()
        self._active = {}

    def add(self, metadata: dict) -> None:
        name = metadata["container"]
        with self._lock:
            if name in self._active:
                raise BrokerError("duplicate active container name", "container_registry")
            self._active[name] = dict(metadata)

    def remove(self, name: str, request_id: str) -> None:
        with self._lock:
            current = self._active.get(name)
            if current is not None and current["request_id"] == request_id:
                del self._active[name]

    def snapshot(self) -> dict:
        with self._lock:
            return {name: dict(meta) for name, meta in self._active.items()}

    def is_active(self, name: str) -> bool:
        with self._lock:
            return name in self._active


class OracleBroker:
    def __init__(self, run_roots=(), proc_root="/proc", audit_path=None,
                 broker_instance=None, registry=None, runner_image=RUNNER,
                 max_global=MAX_GLOBAL_CONTAINERS, root_uid=0):
        self.run_roots = list(run_roots)
        self.proc_root = str(proc_root)
        self.audit_path = audit_path
        self.broker_instance = broker_instance or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", self.broker_instance):
            raise ValueError("invalid broker_instance")
        self.registry = registry or ContainerRegistry()
        self.runner_image = runner_image
        self.max_global = max_global
        self.root_uid = root_uid
        self._admission_lock = threading.Lock()
        self._task_slots = set()
        self._global_slots = 0
        self._audit_lock = threading.Lock()

    def authorize(self, task_id: str, peer_pid: int, peer_uid: int) -> TaskBinding:
        return authorize_request(task_id, peer_pid, peer_uid, self.run_roots,
                                 self.proc_root, self.root_uid)

    def _acquire(self, task_id: str) -> None:
        with self._admission_lock:
            if task_id in self._task_slots or self._global_slots >= self.max_global:
                raise BrokerError("oracle concurrency limit reached", "oracle_busy")
            self._task_slots.add(task_id)
            self._global_slots += 1

    def _release(self, task_id: str) -> None:
        with self._admission_lock:
            self._task_slots.discard(task_id)
            self._global_slots -= 1

    def _new_container_name(self) -> str:
        while True:
            name = f"ob-{self.broker_instance[:16]}-{uuid.uuid4().hex}"
            if not self.registry.is_active(name):
                return name

    def _labels(self, binding: TaskBinding, request_id: str) -> dict:
        labels = {CONTAINER_LABEL: "1",
                  LABEL_PREFIX + "broker_instance": self.broker_instance}
        values = {"run_id": binding.run_id, "task_id": binding.task_id,
                  "request_id": request_id}
        for key in LABEL_KEYS[1:]:
            value = values[key]
            if not isinstance(value, str) or not value or "\x00" in value or len(value) > 200:
                raise BrokerError(f"unsafe {key} container label", "control_invalid")
            labels[LABEL_PREFIX + key] = value
        return labels

    def _execute_container(self, binding: TaskBinding, info, records: list,
                           request: dict, request_id: str, prepare_s: float) -> dict:
        cname = self._new_container_name()
        metadata = {"container": cname, "broker_instance": self.broker_instance,
                    "run_id": binding.run_id, "task_id": binding.task_id,
                    "request_id": request_id}
        labels = self._labels(binding, request_id)
        bin_dir, target, asan, _kind = info
        timeout = request["timeout"]
        mounted = ["/poc/" + record["name"] for record in records]
        env_prefix = f"env LD_LIBRARY_PATH=/out-libs ASAN_OPTIONS={asan}"
        if request["perturb"] is not None:
            env_prefix += f" MALLOC_PERTURB_={request['perturb']}"
        inner = (f"echo {RUNNER_SENTINEL}; "
                 f"{env_prefix} timeout -s SIGKILL {timeout} /out/{target} "
                 + " ".join(mounted)
                 + f"; echo __ORACLE_TARGET_EXIT_V2__$?")
        cmd = ["docker", "run", "--rm", "--name", cname]
        for key, value in labels.items():
            cmd += ["--label", f"{key}={value}"]
        cmd += ["--network", "none", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", "256",
                "-v", f"{pathlib.Path(bin_dir) / 'out'}:/out:ro",
                "-v", f"{pathlib.Path(bin_dir) / 'libs'}:/out-libs:ro",
                "-v", f"{pathlib.Path(records[0]['path']).parent}:/poc:ro",
                self.runner_image, "bash", "-c", inner]
        self.registry.add(metadata)
        started = time.monotonic()
        try:
            try:
                result = subprocess.run(cmd, capture_output=True,
                                        timeout=timeout + 150)
                rc = result.returncode
                stdout_text = _as_text(result.stdout)
                stderr_text = _as_text(result.stderr)
            except subprocess.TimeoutExpired:
                cleanup, kill_rc = _kill_and_verify(cname)
                return {"schema": 2, "verdict": "ORACLE_ERROR",
                        "runner_started": None, "target_started": None,
                        "runner_exit_code": None, "target_exit_code": None,
                        "runner_error_kind": "runner_hang",
                        "sanitizer_detected": False, "phase": "outer_deadline",
                        "prepare_s": prepare_s, "cleanup": cleanup,
                        "kill_rc": kill_rc, "rc": -1, "sig": "",
                        "tail": f"container unresponsive (outer kill, cleanup={cleanup})",
                        "killed_name": cname, "container": cname}
            except OSError as exc:
                cleanup, kill_rc = _kill_and_verify(cname)
                return {"schema": 2, "verdict": "ORACLE_ERROR",
                        "runner_started": False, "target_started": False,
                        "runner_exit_code": None, "target_exit_code": None,
                        "runner_error_kind": "docker_missing",
                        "sanitizer_detected": False, "cleanup": cleanup,
                        "kill_rc": kill_rc, "rc": -1, "sig": "",
                        "tail": f"docker invocation failed: {exc}"[:300],
                        "container": cname}
            except BaseException:
                _kill_and_verify(cname)
                raise

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
            sanitizer = bool(_local_oracle.FATAL_RE.search(out_text) or
                             _local_oracle.NONFATAL_RE.search(out_text))
            kind = None if runner_started else runner_error_kind(out_text)
            if runner_started and not target_started:
                kind = "target_incomplete"
            return {"schema": 2, "verdict": verdict,
                    "runner_started": runner_started, "target_started": target_started,
                    "runner_exit_code": rc, "target_exit_code": target_exit,
                    "runner_error_kind": kind, "sanitizer_detected": sanitizer,
                    "killed": target_exit == 137, "cleanup": cleanup,
                    "kill_rc": kill_rc,
                    "oom_evidence": bool(_local_oracle.LIBFUZZ_OOM_RE.search(out_text)),
                    "prepare_s": prepare_s,
                    "docker_s": time.monotonic() - started,
                    "rc": rc, "sig": signature(out_text), "tail": out_text[-1500:],
                    "container": cname}
        finally:
            self.registry.remove(cname, request_id)

    def _run(self, request: dict, peer_pid: int, peer_uid: int,
             request_id: str) -> dict:
        self._acquire(request["task_id"])
        tmp = None
        try:
            started = time.monotonic()
            binding = self.authorize(request["task_id"], peer_pid, peer_uid)
            info = side_info(request["task_id"], "vul")
            if info is None:
                result = _response_error(request_id, "side_info_unavailable",
                                         "vulnerability-side metadata is unavailable")
                result.update({"run_id": binding.run_id, "task_id": binding.task_id})
                return result
            # Scoped lifecycle preflight: inspect only this broker instance and
            # never reap a container still present in the live request registry.
            # Unlike the legacy global age-based sweep, an unknown inspection
            # blocks only a new execution; it never authorises a cross-task kill.
            preflight = self.reap_orphans()
            if any(entry.get("cleanup") != "cleanup_verified"
                   for entry in preflight):
                result = _response_error(
                    request_id, "sweep_preflight_failed",
                    "scoped orphan cleanup could not be verified")
                result.update({"run_id": binding.run_id, "task_id": binding.task_id,
                               "orphan_cleanup": preflight})
                return result
            tmp = tempfile.mkdtemp(prefix="oracle-broker-")
            os.chmod(tmp, 0o700)
            try:
                records = secure_copy_inputs(binding, request["inputs"], tmp)
            except InputError as exc:
                result = _response_error(request_id, exc.kind, str(exc), "INPUT_ERROR",
                                         exc.input_sha256)
                result.update({"run_id": binding.run_id, "task_id": binding.task_id})
                return result
            hashes = [record["sha256"] for record in records]
            if not records:
                result = _response_error(request_id, "no_readable_files",
                                         "no readable regular files", "NO_INPUT")
                result.update({"input_sha256": hashes, "run_id": binding.run_id,
                               "task_id": binding.task_id})
                return result
            prepare_s = time.monotonic() - started
            result = self._execute_container(binding, info, records, request,
                                             request_id, prepare_s)
            result["input_sha256"] = hashes
            result["run_id"] = binding.run_id
            result["task_id"] = binding.task_id
            if preflight:
                result["orphan_cleanup"] = preflight
            return result
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
            self._release(request["task_id"])

    def handle_request(self, raw, peer_pid: int, peer_uid: int,
                       peer_gid: int = 0, request_id=None) -> dict:
        request_id = request_id or uuid.uuid4().hex
        try:
            if not isinstance(raw, dict):
                raise RequestError("request must be a JSON object", "invalid_request")
            if raw.get("op") == "version":
                if set(raw) != {"op"}:
                    raise RequestError("version accepts only op", "unknown_field")
                return {"request_id": request_id, "broker_version": BROKER_VERSION,
                        "version": BROKER_VERSION, "op": "version"}
            request = validate_run_request(raw)
            result = self._run(request, peer_pid, peer_uid, request_id)
            result.update({"request_id": request_id,
                           "broker_version": BROKER_VERSION})
            return result
        except InputError as exc:
            return _response_error(request_id, exc.kind, str(exc), "INPUT_ERROR",
                                   exc.input_sha256)
        except (RequestError, AuthorizationError) as exc:
            return _refusal_error(request_id, exc)
        except BrokerError as exc:
            return _response_error(request_id, exc.kind, str(exc))
        except Exception as exc:
            return _response_error(request_id, "broker_internal",
                                   f"internal broker error: {type(exc).__name__}")

    def handle_audited_request(self, raw, peer_pid: int, peer_uid: int,
                               peer_gid: int = 0) -> dict:
        request_id = uuid.uuid4().hex
        task_id = raw.get("task_id") if isinstance(raw, dict) else None
        identity = {"uid": peer_uid, "gid": peer_gid, "pid": peer_pid,
                    "task_id": task_id, "request_id": request_id}
        try:
            # A request which cannot enter the root audit log is never executed.
            self._audit(dict(identity, phase="request"))
        except BrokerError as exc:
            return _response_error(request_id, exc.kind, str(exc))
        response = self.handle_request(raw, peer_pid, peer_uid, peer_gid,
                                       request_id=request_id)
        result_record = dict(
            identity, phase="result", run_id=response.get("run_id"),
            task_id=response.get("task_id", task_id),
            input_sha256=response.get("input_sha256", []),
            container=response.get("container"),
            verdict=response.get("verdict", "VERSION"),
            cleanup=response.get("cleanup"))
        try:
            self._audit(result_record)
        except BrokerError as exc:
            # In particular, never expose CLEAN/FATAL without a root audit record.
            failed = _response_error(request_id, exc.kind, str(exc),
                                     input_sha256=response.get("input_sha256", []))
            failed.update({"run_id": response.get("run_id"),
                           "task_id": response.get("task_id", task_id),
                           "container": response.get("container"),
                           "cleanup": response.get("cleanup"),
                           "unaudited_verdict": response.get("verdict")})
            return failed
        return response

    def _audit(self, record: dict) -> None:
        if not self.audit_path:
            return
        path = pathlib.Path(self.audit_path)
        payload = dict(record)
        payload["ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) +
                    "\n").encode("utf-8")
            with self._audit_lock:
                flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(str(path), flags, 0o600)
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode):
                        raise OSError("audit path is not a regular file")
                    if os.name == "posix" and (
                            st.st_uid != self.root_uid or
                            st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                        raise OSError("audit file is not root-owned and private")
                    view = memoryview(line)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("short audit write")
                        view = view[written:]
                finally:
                    os.close(fd)
        except (OSError, TypeError, ValueError) as exc:
            raise BrokerError(f"root audit write failed: {exc}", "audit_failed")

    def reap_orphans(self) -> list:
        """Reap only provably inactive containers from this broker instance."""
        filter_instance = f"label={LABEL_PREFIX}broker_instance={self.broker_instance}"
        try:
            listed = subprocess.run(
                ["docker", "ps", "-a", "--filter", f"label={CONTAINER_LABEL}=1",
                 "--filter", filter_instance, "--format", "{{.ID}}\t{{.Names}}"],
                capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            return [{"id": None, "name": None, "cleanup": "inspection_incomplete"}]
        if listed.returncode != 0:
            return [{"id": None, "name": None, "cleanup": "inspection_incomplete"}]
        active = self.registry.snapshot()
        results = []
        for line in (listed.stdout or "").splitlines():
            cid, sep, listed_name = line.partition("\t")
            if not sep or not cid or not listed_name:
                results.append({"id": cid or None, "name": listed_name or None,
                                "cleanup": "inspection_incomplete"})
                continue
            if listed_name in active:
                continue
            try:
                inspected = subprocess.run(
                    ["docker", "inspect", "--type", "container", cid],
                    capture_output=True, text=True, timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                results.append({"id": cid, "name": listed_name,
                                "cleanup": "inspection_incomplete"})
                continue
            if inspected.returncode != 0:
                results.append({"id": cid, "name": listed_name,
                                "cleanup": "inspection_incomplete"})
                continue
            try:
                objects = json.loads(inspected.stdout)
                obj = objects[0]
                name = obj["Name"].lstrip("/")
                labels = obj["Config"]["Labels"]
                if name != listed_name or not isinstance(labels, dict):
                    raise ValueError("inconsistent container identity")
                if labels.get(CONTAINER_LABEL) != "1":
                    continue
                if labels.get(LABEL_PREFIX + "broker_instance") != self.broker_instance:
                    continue
                if any(not labels.get(LABEL_PREFIX + key) for key in LABEL_KEYS[1:]):
                    raise ValueError("incomplete broker labels")
            except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
                results.append({"id": cid, "name": listed_name,
                                "cleanup": "inspection_incomplete"})
                continue
            # Close the inspect-to-kill race against this process's registry.
            if self.registry.is_active(name):
                continue
            cleanup, kill_rc = _kill_and_verify(name)
            results.append({"id": cid, "name": name, "cleanup": cleanup,
                            "kill_rc": kill_rc})
        return results

    def handle_connection(self, conn, peer=None) -> None:
        request_id = uuid.uuid4().hex
        try:
            conn.settimeout(35)
            if peer is None:
                peer = peer_credentials(conn)
            peer_pid, peer_uid, peer_gid = peer
            raw = _recv_json_line(conn, MAX_MESSAGE_BYTES)
            response = self.handle_audited_request(raw, peer_pid, peer_uid, peer_gid)
        except RequestError as exc:
            response = _refusal_error(request_id, exc)
        except BrokerError as exc:
            response = _response_error(request_id, exc.kind, str(exc))
        except (OSError, socket.timeout) as exc:
            response = _response_error(request_id, "connection_error", str(exc))
        try:
            _send_json_line(conn, response)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def peer_credentials(conn) -> tuple:
    if not hasattr(socket, "SO_PEERCRED"):
        raise BrokerError("SO_PEERCRED is unavailable", "peercred_unavailable")
    try:
        size = struct.calcsize("3i")
        data = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
        if len(data) != size:
            raise BrokerError("short SO_PEERCRED result", "peercred_failed")
        return struct.unpack("3i", data)
    except OSError as exc:
        raise BrokerError(f"SO_PEERCRED failed: {exc}", "peercred_failed")


def _recv_json_line(conn, max_bytes: int):
    data = bytearray()
    while True:
        remaining = max_bytes + 1 - len(data)
        if remaining <= 0:
            raise RequestError("JSON message exceeds 2 MiB", "message_too_large")
        chunk = conn.recv(min(65536, remaining))
        if not chunk:
            raise RequestError("connection closed before a JSON line", "truncated_message")
        data.extend(chunk)
        if len(data) > max_bytes:
            raise RequestError("JSON message exceeds 2 MiB", "message_too_large")
        if b"\n" in data:
            line, _newline, extra = bytes(data).partition(b"\n")
            if extra.strip():
                raise RequestError("exactly one request/response per connection",
                                   "multiple_messages")
            break
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestError(f"invalid JSON request: {exc}", "invalid_json")
    if not isinstance(value, dict):
        raise RequestError("JSON message must be an object", "invalid_request")
    return value


def _send_json_line(conn, value: dict) -> None:
    payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) +
               "\n").encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        payload = (json.dumps(_response_error(uuid.uuid4().hex, "response_too_large",
                                              "broker response exceeds 2 MiB"),
                              separators=(",", ":")) + "\n").encode("utf-8")
    conn.sendall(payload)


def call(request, socket_path, timeout=210) -> dict:
    """Make one broker call; return its JSON response or raise BrokerError."""
    if not hasattr(socket, "AF_UNIX"):
        raise BrokerError("Unix sockets are unavailable", "client_unavailable")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise BrokerError("timeout must be positive", "client_timeout")
    try:
        payload = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) +
                   "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BrokerError(f"cannot encode request: {exc}", "client_protocol")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise BrokerError("request exceeds 2 MiB", "message_too_large")
    conn = None
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        conn.connect(str(socket_path))
        conn.sendall(payload)
        response = _recv_json_line(conn, MAX_MESSAGE_BYTES)
        if response.get("broker_version") != BROKER_VERSION:
            raise BrokerError("broker version mismatch", "client_protocol")
        return response
    except BrokerError:
        raise
    except (OSError, socket.timeout) as exc:
        raise BrokerError(f"broker call failed: {exc}", "client_io")
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


def reap_orphans(broker_instance: str, registry: ContainerRegistry) -> list:
    """Functional entry point for operators using an existing live registry."""
    broker = OracleBroker((), broker_instance=broker_instance, registry=registry)
    return broker.reap_orphans()


def serve(socket_path=None, run_roots=None, audit_path=None, *, proc_root="/proc",
          broker=None) -> None:
    """Serve forever on a root-owned Unix socket, one thread per connection."""
    if os.name != "posix" or not hasattr(socket, "AF_UNIX"):
        raise BrokerError("the root broker requires POSIX Unix sockets", "server_unavailable")
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise BrokerError("the oracle broker must run as root", "server_not_root")
    socket_path = str(socket_path or os.environ.get(
        "ORACLE_BROKER_SOCKET", DEFAULT_SOCKET_PATH))
    if run_roots is None:
        configured = os.environ.get("ORACLE_BROKER_RUN_ROOTS", "/data/cybergym/stage4")
        run_roots = [item for item in configured.split(os.pathsep) if item]
    audit_path = audit_path or os.environ.get(
        "ORACLE_BROKER_AUDIT", "/data/cybergym/oracle-broker-audit.jsonl")
    broker = broker or OracleBroker(
        run_roots, proc_root, audit_path,
        broker_instance=os.environ.get("ORACLE_BROKER_INSTANCE"))
    path = pathlib.Path(socket_path)
    os.makedirs(str(path.parent), mode=0o711, exist_ok=True)
    _trusted_dir(path.parent)
    if stat.S_IMODE(os.lstat(str(path.parent)).st_mode) != 0o711:
        raise BrokerError("broker socket directory must be root-owned 0711",
                          "socket_directory_untrusted")
    if os.path.lexists(str(path)):
        raise BrokerError("broker socket already exists; refusing to replace it",
                          "socket_exists")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound_stat = None
    try:
        server.bind(str(path))
        bound_stat = os.lstat(str(path))
        os.chmod(str(path), 0o666)  # filesystem access is not authorisation
        server.listen(128)
        while True:
            conn, _address = server.accept()
            thread = threading.Thread(target=broker.handle_connection,
                                      args=(conn,), daemon=True)
            thread.start()
    finally:
        server.close()
        if bound_stat is not None:
            try:
                current = os.lstat(str(path))
                if (current.st_dev, current.st_ino) == (bound_stat.st_dev,
                                                        bound_stat.st_ino):
                    os.unlink(str(path))
            except OSError:
                pass


if __name__ == "__main__":
    serve()
