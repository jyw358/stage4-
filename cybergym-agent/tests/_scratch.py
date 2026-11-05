"""Workspace-local scratch dirs for tests.

``tempfile.TemporaryDirectory`` creates 0700 dirs that the Windows sandbox
token cannot write into (the chmod'd ACL denies the process); plain
``Path.mkdir`` inherits the workspace ACL and works.  Production code never
relies on this helper.

On POSIX as root the base must itself pass the control-plane trust check
(root-owned, no group/other write, other-traversable) -- the repo is often
checked out under a 0700 path (e.g. /root/s4test), which the driver rightly
refuses because the prep/agent identities could not reach their own dirs.
"""
import os
import shutil
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / ".testtmp"


def _base() -> Path:
    if os.name == "posix" and getattr(os, "geteuid", lambda: 1)() == 0:
        base = Path(os.environ.get("S4_TEST_TMP", "/var/lib/s4-test-scratch"))
        base.mkdir(parents=True, exist_ok=True)
        os.chown(base, 0, 0)
        os.chmod(base, 0o755)
        return base
    return ROOT


def scratch_dir() -> Path:
    base = _base()
    base.mkdir(parents=True, exist_ok=True)
    path = base / uuid.uuid4().hex
    path.mkdir()
    return path


def cleanup(path) -> None:
    shutil.rmtree(path, ignore_errors=True)
