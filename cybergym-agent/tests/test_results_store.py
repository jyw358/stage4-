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

    # ---- R2/B9: execution vs scoring terminality -------------------------
    def test_submitted_without_score_is_not_scoring_terminal(self):
        row = {"status": "submitted", "score_status": None}
        self.assertTrue(results_store.is_execution_terminal(row))
        self.assertFalse(results_store.is_scoring_terminal(row))

    def test_submitted_pending_is_not_scoring_terminal(self):
        row = {"status": "submitted", "score_status": "pending"}
        self.assertFalse(results_store.is_scoring_terminal(row))

    def test_query_error_is_not_scoring_terminal(self):
        row = {"status": "submitted", "score_status": "query_error"}
        self.assertFalse(results_store.is_scoring_terminal(row))

    def test_scored_or_failclosed_is_scoring_terminal(self):
        for st in ("ok", "fix_pending", "no_records", "no_ledger",
                   "ledger_mismatch", "hash_mismatch"):
            self.assertTrue(results_store.is_scoring_terminal(
                {"status": "submitted", "score_status": st}), st)

    # ---- R2/B8: rebuild must not resurrect legacy aggregate rows ----------
    def test_rebuild_drops_rows_that_only_live_in_results_json(self):
        # review repro #2: an old success only in results.json, rows/ cleared
        results_store._atomic_json(Path(self.dir) / "results.json", [{
            "task_id": "old:001", "run_id": "old-run",
            "status": "submitted", "official_success": True,
        }])
        rows = results_store.rebuild(self.dir)
        self.assertEqual(rows, [])
        agg = json.load(open(Path(self.dir) / "results.json", encoding="utf-8"))
        self.assertEqual(agg, [])

    def test_merge_row_does_not_inherit_legacy_aggregate_rows(self):
        results_store._atomic_json(Path(self.dir) / "results.json", [{
            "task_id": "ghost:1", "run_id": "old", "status": "submitted",
            "official_success": True}])
        results_store.merge_row(self.dir, "new:1", {"status": "no_submission"})
        agg = json.load(open(Path(self.dir) / "results.json", encoding="utf-8"))
        self.assertEqual([r["task_id"] for r in agg], ["new:1"])

    def test_merge_row_writes_authority_before_aggregate(self):
        # B8 ordering: after merging, rows/ (authority) must contain the patch
        # even when the derived aggregate is stale
        results_store.merge_row(self.dir, "t1", {"status": "submitted",
                                                 "submissions": 2})
        row = results_store.read_row(self.dir, "t1")
        self.assertEqual(row["submissions"], 2)
        agg = {r["task_id"]: r for r in
               json.load(open(Path(self.dir) / "results.json", encoding="utf-8"))}
        self.assertEqual(agg["t1"]["submissions"], 2)

    # ---- R2 MAJOR: corrupt rows / task-id collisions ----------------------
    def test_read_rows_strict_raises_on_corrupt_row(self):
        (Path(self.dir) / "rows").mkdir(exist_ok=True)
        (Path(self.dir) / "rows" / "bad.json").write_text("{not json",
                                                          encoding="utf-8")
        with self.assertRaises(results_store.ResultsIntegrityError):
            results_store.read_rows(self.dir, strict=True)
        # non-strict keeps skipping (legacy readers)
        self.assertEqual(results_store.read_rows(self.dir), [])

    def test_assert_unique_task_ids_rejects_collision(self):
        with self.assertRaises(results_store.ResultsIntegrityError):
            results_store.assert_unique_task_ids(["a:b", "a_b"])
        # identical ids repeated are fine; distinct non-colliding ids are fine
        results_store.assert_unique_task_ids(["a:b", "a:b", "c:d"])


if __name__ == "__main__":
    unittest.main()
