"""R2 regression tests for the stage-4 driver logic (B9/B12) and the
root-side ledger promoter (B11).  Runs on Windows too: the POSIX-only paths
are guarded inside the implementation."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

AG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AG))

from scripts import stage4  # noqa: E402
from tests._scratch import cleanup, scratch_dir  # noqa: E402


class TestSelectTodo(unittest.TestCase):
    """B9: submitted-but-unscored rows go to scoring recovery, never back to
    the agent; retry-failed keeps its documented semantics."""

    def setUp(self):
        self.dir = scratch_dir()
        self.addCleanup(cleanup, self.dir)

    def row(self, tid, **kw):
        base = {"task_id": tid, "run_id": "s4-r"}
        base.update(kw)
        (Path(self.dir) / "rows").mkdir(parents=True, exist_ok=True)
        (Path(self.dir) / "rows" / (tid.replace(":", "_") + ".json")).write_text(
            json.dumps(base), encoding="utf-8")
        return base

    def test_unscored_submitted_goes_to_scoring_recovery(self):
        self.row("a:1", status="submitted", score_status="pending",
                 official_success=None)
        todo, recovery = stage4.select_todo(["a:1"], self.dir, "s4-r")
        self.assertEqual(todo, [])
        self.assertEqual(recovery, ["a:1"])

    def test_crashed_running_row_is_retried(self):
        self.row("a:1", status="running")
        todo, recovery = stage4.select_todo(["a:1"], self.dir, "s4-r")
        self.assertEqual(todo, ["a:1"])
        self.assertEqual(recovery, [])

    def test_fully_scored_row_is_skipped(self):
        self.row("a:1", status="submitted", score_status="ok",
                 official_success=True)
        todo, recovery = stage4.select_todo(["a:1"], self.dir, "s4-r")
        self.assertEqual(todo, [])
        self.assertEqual(recovery, [])

    def test_foreign_run_row_is_retried(self):
        self.row("a:1", run_id="s4-OTHER", status="submitted",
                 score_status="ok", official_success=True)
        todo, recovery = stage4.select_todo(["a:1"], self.dir, "s4-r")
        self.assertEqual(todo, ["a:1"])

    def test_retry_failed_retries_official_false_and_worker_error(self):
        self.row("a:1", status="submitted", score_status="ok",
                 official_success=False)
        self.row("b:2", status="worker_error", official_success=None)
        self.row("c:3", status="gen_fail", official_success=None)
        todo, _ = stage4.select_todo(["a:1", "b:2", "c:3"], self.dir, "s4-r",
                                     retry_failed=True)
        self.assertEqual(sorted(todo), ["a:1", "b:2", "c:3"])

    def test_retry_failed_does_not_rerun_submitted_unscored(self):
        # B9: retry-failed must not re-RUN the agent for a row that only
        # lacks a scoring verdict
        self.row("a:1", status="submitted", score_status=None,
                 official_success=None)
        todo, recovery = stage4.select_todo(["a:1"], self.dir, "s4-r",
                                            retry_failed=True)
        self.assertEqual(todo, [])
        self.assertEqual(recovery, ["a:1"])


class TestKillTaskTree(unittest.TestCase):
    """B12: on Windows the fallback kills the direct child and reaps it."""

    def test_windows_fallback_kills_and_reaps(self):
        if sys.platform != "win32":
            self.skipTest("posix path covered by production usage")
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        stage4.kill_task_tree(proc)
        self.assertIsNotNone(proc.poll())  # reaped, not left running


class TestPromoteLedger(unittest.TestCase):
    """B11: the root-side promoter dedups by poc_id and refuses to invent a
    protected ledger."""

    def setUp(self):
        self.dir = scratch_dir()
        self.addCleanup(cleanup, self.dir)
        self.prot = Path(self.dir) / "protected.jsonl"

    def promote(self, ws_entries, prot_entries=()):
        ws = Path(self.dir) / "ws.jsonl"
        ws.write_text("".join(json.dumps(e) + "\n" for e in ws_entries),
                      encoding="utf-8")
        self.prot.write_text(
            "".join(json.dumps(e) + "\n" for e in prot_entries), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, str(AG / "scripts" / "promote_ledger.py"),
             "--ws-ledger", str(ws), "--protected", str(self.prot),
             "--run-id", "s4-r", "--task-id", "t:1"],
            capture_output=True, text=True)
        return r

    def read_prot(self):
        return [json.loads(l) for l in
                self.prot.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_missing_protected_ledger_is_refused(self):
        ws = Path(self.dir) / "ws.jsonl"
        ws.write_text("{}\n", encoding="utf-8")
        self.prot.unlink(missing_ok=True)
        r = subprocess.run(
            [sys.executable, str(AG / "scripts" / "promote_ledger.py"),
             "--ws-ledger", str(ws), "--protected", str(self.prot)],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)

    def test_promotes_only_new_entries_and_dedups(self):
        e1 = {"poc_id": "p1", "poc_sha256": "a" * 64}
        e2 = {"poc_id": "p2", "poc_sha256": "b" * 64}
        r = self.promote([e1, e2], prot_entries=[e1])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        prot = self.read_prot()
        ids = [e["poc_id"] for e in prot]
        self.assertEqual(ids, ["p1", "p2"])   # p1 not duplicated
        by_id = {e["poc_id"]: e for e in prot}
        self.assertEqual(by_id["p2"]["promoted_by"], "root")
        self.assertEqual(by_id["p2"]["run_id"], "s4-r")
        self.assertEqual(by_id["p2"]["task_id"], "t:1")

    def test_nothing_to_promote_is_ok(self):
        e1 = {"poc_id": "p1", "poc_sha256": "a" * 64}
        r = self.promote([e1], prot_entries=[e1])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(self.read_prot()), 1)


if __name__ == "__main__":
    unittest.main()
