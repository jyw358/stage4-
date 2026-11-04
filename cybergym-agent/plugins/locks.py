"""Cross-platform advisory file lock.

POSIX uses fcntl.flock; Windows (used only for local tests) uses msvcrt.locking
on a one-byte range.  A process-wide thread lock is acquired first: Windows
LockFile reports EDEADLOCK when two handles in the same process overlap, and
serialising threads in-process is correct for both platforms anyway (the file
lock still provides cross-process exclusion).

The lock file is separate from the data file, so a crash can never leave a
half-written JSON document behind the lock.
"""
from __future__ import annotations

import os
import threading

_REGISTRY = {}
_REGISTRY_LOCK = threading.Lock()


def _thread_lock(path: str) -> threading.Lock:
    key = os.path.abspath(path)
    with _REGISTRY_LOCK:
        return _REGISTRY.setdefault(key, threading.Lock())


class FileLock:
    def __init__(self, path):
        self.path = str(path)
        self._fh = None
        self._tlock = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._tlock = _thread_lock(self.path)
        self._tlock.acquire()
        try:
            self._fh = open(self.path, "a+b")
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0, os.SEEK_END)
                if self._fh.tell() == 0:
                    self._fh.write(b"\0")
                    self._fh.flush()
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            self._tlock.release()
            raise
        return self

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
