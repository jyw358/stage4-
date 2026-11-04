"""cybergym-env plugin: task environment + sandbox execution.

Async task trio (create/get/stop) copied from the reference mcp-server.yaml
pattern: heavy jobs return a task_id immediately; callers poll status; they
never block the agent loop.

DockerRunner shells out to `docker run` on the pre-built vul/fix pair
(image build itself is offline preprocessing, see README). FakeRunner is
scripted for tests and dryrun, so the whole stack runs on any machine.
"""
import json
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------- runners
@dataclass
class RunOutcome:
    stage: str = "run"            # "compile" | "run"
    ok: bool = False              # process completed within timeout
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    signature: Optional[tuple] = None   # (bug_type, top_frame) from ASAN; set by verifier
    timed_out: bool = False
    duration_s: float = 0.0


class DockerRunner:
    """Runs a PoC inside the vul/fix image pair. One runner, many tasks."""

    def __init__(self, timeout_s: int = 20):
        self.timeout_s = timeout_s
        self._exec_tasks: dict = {}

    def run(self, image: str, poc_path: str | Path, timeout_s: Optional[int] = None) -> RunOutcome:
        poc_path = str(Path(poc_path).resolve()).replace("\\", "/")
        # poc mounted read-only at /poc; image entrypoint knows how to feed it
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{poc_path}:/poc:ro",
            "--network", "none",
            "--memory", "2g", "--cpus", "1",
            image,
        ]
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout_s or self.timeout_s,
            )
            return RunOutcome(
                ok=True, returncode=proc.returncode,
                stdout=proc.stdout[-20000:], stderr=proc.stderr[-20000:],
                duration_s=time.monotonic() - t0,
            )
        except subprocess.TimeoutExpired:
            return RunOutcome(ok=False, timed_out=True, duration_s=time.monotonic() - t0)

    # ---- async trio facade (pattern parity with mcp-server.yaml) ----
    def create_exec_task(self, image: str, poc_path: str) -> str:
        task_id = f"exec-{uuid.uuid4().hex[:12]}"
        self._exec_tasks[task_id] = {"image": image, "poc": poc_path, "state": "queued"}
        return task_id

    def get_exec_status(self, task_id: str) -> str:
        return self._exec_tasks.get(task_id, {}).get("state", "unknown")

    def get_exec_result(self, task_id: str) -> Optional[RunOutcome]:
        meta = self._exec_tasks.get(task_id)
        if not meta:
            return None
        meta["state"] = "running"
        outcome = self.run(meta["image"], meta["poc"])
        meta["state"] = "completed"
        meta["result"] = outcome
        return outcome


class FakeRunner:
    """Scripted outcomes for tests/dryrun. outcomes = {"vul": [...], "fix": [...]}, popped per call."""

    def __init__(self, outcomes: dict):
        self.outcomes = {k: list(v) for k, v in outcomes.items()}
        self.calls: list = []

    def run(self, image: str, poc_path: str, timeout_s: Optional[int] = None) -> RunOutcome:
        self.calls.append(image)
        key = "vul" if "vul" in image else "fix"
        queue = self.outcomes.get(key, [])
        return queue.pop(0) if queue else RunOutcome(ok=True, returncode=0)


# ---------------------------------------------------------------- env
@dataclass
class TaskEnv:
    task_id: str
    workdir: Path
    vul_image: str
    fix_image: str
    description: str = ""

    @classmethod
    def from_cybergym_out(cls, out_dir: str | Path, task_id: str,
                          vul_image: str = "", fix_image: str = "") -> "TaskEnv":
        """Wire-up point for `python -m cybergym.task.gen_task` output dirs.

        Expects: description.txt, repo-vul.tar.gz (extracted by caller into repo/),
        submit.sh. Images come from the offline pre-build step.
        """
        out_dir = Path(out_dir)
        desc_file = out_dir / "description.txt"
        return cls(
            task_id=task_id,
            workdir=out_dir,
            vul_image=vul_image or f"cybergym/{task_id.replace(':', '_')}:vul",
            fix_image=fix_image or f"cybergym/{task_id.replace(':', '_')}:fix",
            description=desc_file.read_text(encoding="utf-8", errors="ignore") if desc_file.exists() else "",
        )

    def context_manifest(self) -> dict:
        """Files the worker is allowed to pull into context (sliced reads only)."""
        repo = self.workdir / "repo"
        if not repo.exists():
            return {"repo_dir": str(repo), "files": []}
        files = sorted(
            str(p.relative_to(repo)) for p in repo.rglob("*")
            if p.is_file() and p.suffix in {".c", ".cc", ".cpp", ".h", ".hpp", ".java", ".py", ".go", ".js"}
        )
        return {"repo_dir": str(repo), "files": files[:500]}
