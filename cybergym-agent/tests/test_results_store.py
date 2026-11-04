import json
import threading
import unittest
from pathlib import Path

from plugins import results_store
from tests._scratch import cleanup, scratch_dir


class TestResultsStore(unittest.TestCase):
    def setUp(self):
        self.dir = scratch_dir()
        self.addCleanup(cleanup, self.dir)

    def test_merge_row_keeps_both_rows(self):
        results_store.merge_row(self.dir, "t1", {"status": "submitted", "submissions": 1})
        results_store.merge_row(self.dir, "t2", {"status": "no_submission", "submissions": 0})
        results_store.merge_row(self.dir, "t1", {"score_status": "ok"})
        rows = {r["task_id"]: r for r in results_store.read_rows(self.dir)}
        self.assertEqual(rows["t1"]["status"], "submitted")
        self.assertEqual(rows["t1"]["score_status"], "ok")
        self.assertEqual(rows["t2"]["status"], "no_submission")
        agg = json.load(open(Path(self.dir) / "results.json", encoding="utf-8"))
        self.assertEqual(len(agg), 2)

    def test_concurrent_writers_do_not_lose_rows(self):
        ids = [f"arvo:{i}" for i in range(40)]
        errors = []

        def worker(tid):
            try:
                for n in range(3):
                    results_store.merge_row(
                        self.dir, tid, {"status": "submitted", "submissions": n + 1})
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(tid,)) for tid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        rows = results_store.read_rows(self.dir)
        self.assertEqual(len(rows), len(ids))
        self.assertEqual({r["task_id"] for r in rows}, set(ids))

    def test_rebuild_uses_queue_order(self):
        for tid in ("b:2", "a:1", "c:3"):
            results_store.merge_row(self.dir, tid, {"status": "submitted"})
        ordered = results_store.rebuild(self.dir, order=["c:3", "a:1", "b:2"])
        self.assertEqual([r["task_id"] for r in ordered], ["c:3", "a:1", "b:2"])

    def test_write_row_does_not_touch_aggregate(self):
        results_store.write_row(self.dir, "t1", {"task_id": "t1", "status": "running"})
        self.assertFalse((Path(self.dir) / "results.json").exists())
        self.assertEqual(results_store.read_row(self.dir, "t1")["status"], "running")

    def test_is_terminal(self):
        self.assertTrue(results_store.is_terminal({"status": "submitted"}))
        self.assertFalse(results_store.is_terminal({"status": "running"}))
        self.assertTrue(results_store.is_terminal({"official_success": False}))
        self.assertFalse(results_store.is_terminal({"official_success": None}))


if __name__ == "__main__":
    unittest.main()
