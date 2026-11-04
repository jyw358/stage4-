"""Cross-platform advisory file lock.

POSIX uses fcntl.flock; Windows (used only for local tests) uses msvcrt.locking
on a one-byte range.  A process-wide thread lock is acquired first: Windows
LockFile reports EDEADLOCK when two handles in the same process overlap, and
serialising threads in-process is correct for both platforms anyway (the file
lock still provides cross-process exclusion).

The lock file is separate from the data file, so a crash can never leave a
half-written JSON document behind the lock.

R2: ``timeout`` gives a non-blocking (timeout=0) or bounded wait, used by the
stage-4 driver to refuse a second concurrent driver on the same --out.
``fs_type``/``assert_local_lockable`` detect network filesystems where flock
semantics are not equivalent to a local disk (R2 MAJOR): multi-worker runs
must not silently trust them.
"""
from __future__ import annotations

import os
import sys
import threading
import time

_REGISTRY = {}
_REGISTRY_LOCK = threading.Lock()

# filesystems on which flock()/atomic-rename guarantees are NOT equivalent to
# a local disk; multi-worker locking must be refused there, not assumed
NETWORK_FS = {"nfs", "nfs4", "cifs", "smbfs", "smb2", "fuse.sshfs", "sshfs",
              "glusterfs", "lustre", "9p", "ceph", "beegfs", "gpfs", "ocfs2"}


def _thread_lock(path: str) -> threading.Lock:
    key = os.path.abspath(path)
    with _REGISTRY_LOCK:
        return _REGISTRY.setdefault(key, threading.Lock())


class LockBusy(RuntimeError):
    """The lock could not be acquired within the requested timeout."""


class FileLock:
    def __init__(self, path, timeout: float = None):
        self.path = str(path)
        self.timeout = timeout
        self._fh = None
        self._tlock = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._tlock = _thread_lock(self.path)
        self._tlock.acquire()
        try:
            self._fh = open(self.path, "a+b")
            deadline = None if self.timeout is None else time.monotonic() + self.timeout
            while True:
                try:
                    self._try_lock()
                    break
                except OSError:
                    if deadline is not None and time.monotonic() >= deadline:
                        self._fh.close()
                        self._fh = None
                        self._tlock.release()
                        self._tlock = None
                        raise LockBusy(f"lock busy: {self.path}")
                    time.sleep(0.05)
        except Exception:
            if self._tlock is not None and self._tlock.locked():
                self._tlock.release()
            raise
        return self

    def _try_lock(self):
        if os.name == "nt":
            import msvcrt

            self._fh.seek(0, os.SEEK_END)
            if self._fh.tell() == 0:
                self._fh.write(b"\0")
                self._fh.flush()
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, *exc):
        try:
            if self._fh is not None:
                if os.name == "nt":
                    import msvcrt

                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
                self._fh = None
        finally:
            if self._tlock is not None:
                self._tlock.release()
                self._tlock = None
        return False


def fs_type(path) -> str:
    """Best-effort filesystem type for path ('' when it cannot be determined)."""
    if os.name != "posix":
        return ""  # local tests on Windows: no /proc/mounts
    target = os.path.abspath(str(path))
    best_mnt, best_fs = "", ""
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ").replace("\\011", "\t") \
                              .replace("\\134", "\\")
                if target == mnt or target.startswith(mnt.rstrip("/") + "/"):
                    if len(mnt) > len(best_mnt):
                        best_mnt, best_fs = mnt, parts[2]
    except OSError:
        return ""
    return best_fs


def assert_local_lockable(path, multi_worker: bool = True):
    """Return (ok, fstype).  On a network FS multi-worker locking must be
    refused by the caller (R2 MAJOR: flock on NFS is not equivalent)."""
    fstype = fs_type(path)
    if not fstype:
        return True, ""  # unknown (non-POSIX tests): assume local
    if fstype in NETWORK_FS:
        if multi_worker:
            print(f"[locks] WARN: {path} is on {fstype}; flock semantics are "
                  "not equivalent to a local disk", file=sys.stderr)
        return False, fstype
    return True, fstype
