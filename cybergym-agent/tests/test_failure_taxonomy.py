"""Offline fault-injection tests for failure_taxonomy (E5/G2).

Covers the r5-review injection matrix: missing .checks, empty ledger,
scored-clean, scored fix-dirty, scored no-crash, unscored (must NEVER be
retry-classified), session-dead, and the historical infra incident shape
(submit_error + no_records).
"""
import json
import tempfile
import unittest
from pathlib import Path

from plugins import results_store
from scripts import failure_taxonomy as ft


def make_out(rows, ledgers=None):
    out = Path(tempfile.mkdtemp())
    for tid, row in rows.items():
        results_store.write_row(out, tid, row)
    ledgers = ledgers or {}
    for name, recs in ledgers.items():
        d = out / "protected_ledger"
        d.mkdir(exist_ok=True)
        (d / f"{name}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return out


def run_one(row, ledger_recs):
    name = row["task_id"].replace(":", "_")
    rec = ft.classify(row, ledger_recs)
    return rec


class TestClassify(unittest.TestCase):
    def base_row(self, **kw):
        r = {"task_id": "arvo:1", "run_id": "R", "status": "submitted",
             "score_status": "ok", "official_success": True}
        r.update(kw)
        return r

    def test_scored_success(self):
        rec = run_one(self.base_row(), [{"poc_id": "p1"}])
        self.assertEqual(rec["primary_failure_class"], "success")
        self.assertEqual(rec["evidence_confidence"], "high")

    def test_fix_not_clean(self):
        rec = run_one(self.base_row(official_success=False,
                                    final={"fix_exit": 1, "vul_exit": 1}),
                      [{"poc_id": "p1"}])
        self.assertEqual(rec["primary_failure_class"], "fix_not_clean")

    def test_vul_no_crash(self):
        rec = run_one(self.base_row(official_success=False,
                                    final={"fix_exit": 0, "vul_exit": 0}),
                      [{"poc_id": "p1"}])
        self.assertEqual(rec["primary_failure_class"], "vul_no_crash")

    def test_unscored_query_error_is_never_retryable(self):
        # the infra-incident shape: scoring could not conclude -> unscored/*,
        # NOT an agent-attempt class (E1 must send it to recovery, not a retry)
        rec = run_one(self.base_row(official_success=None,
                                    score_status="query_error", checks=None),
                      [])
        self.assertTrue(rec["primary_failure_class"].startswith("unscored/"))
        self.assertIn("checks_counter=unknown", rec["secondary_failure_classes"])
        self.assertEqual(rec["checks_evidence"], "unknown")

    def test_session_dead_timeout_wins_over_no_check(self):
        rec = run_one(self.base_row(status="timeout", score_status=None,
                                    official_success=None, checks=None), [])
        self.assertEqual(rec["primary_failure_class"], "session_dead/timeout")

    def test_submit_error_no_records_is_no_check_attempt(self):
        # pool-smoke3 after grader restore: submit_error + no_records + empty
        # ledger + no checks -> observed no_check_attempt (not session_dead)
        rec = run_one(self.base_row(status="submit_error",
                                    score_status="no_records",
                                    official_success=None, checks=None), [])
        self.assertEqual(rec["primary_failure_class"], "no_check_attempt")

    def test_checks_observed_but_not_accepted(self):
        rec = run_one(self.base_row(status="submit_error",
                                    score_status="no_records",
                                    official_success=None, checks=3), [])
        self.assertEqual(rec["primary_failure_class"], "submit_not_accepted")

    def test_missing_checks_counter_is_advisory_only(self):
        # .checks lost/corrupt -> checks=None; class must still be derivable
        # from root-side sources alone.  v1.1: a MISSING counter is reported
        # as unknown, NEVER as an observed zero (audit r6).
        rec = run_one(self.base_row(official_success=False,
                                    final={"fix_exit": 1, "vul_exit": 1},
                                    checks=None), [{"poc_id": "p"}])
        self.assertEqual(rec["primary_failure_class"], "fix_not_clean")
        self.assertEqual(rec["checks_evidence"], "unknown")
        self.assertIn("checks_counter=unknown", rec["secondary_failure_classes"])
        self.assertNotIn("checks_counter=zero", rec["secondary_failure_classes"])

    def test_zero_and_positive_checks_evidence_distinction(self):
        # observed 0 and observed >=1 must be distinguishable from each other
        # and from a missing counter
        zero = run_one(self.base_row(official_success=None,
                                     score_status="no_records", checks=0), [])
        pos = run_one(self.base_row(official_success=None,
                                    score_status="no_records", checks=2), [])
        self.assertEqual(zero["checks_evidence"], "zero")
        self.assertIn("checks_counter=zero", zero["secondary_failure_classes"])
        self.assertEqual(zero["primary_failure_class"], "no_check_attempt")
        self.assertEqual(pos["checks_evidence"], "positive")
        self.assertNotIn("checks_counter=unknown", pos["secondary_failure_classes"])
        self.assertNotIn("checks_counter=zero", pos["secondary_failure_classes"])

    def test_retry_queue_gate_requires_scored_false(self):
        # v1.1 independent admission gate: a retryable CLASS LABEL alone is
        # not enough -- score_status must be ok AND official_success False.
        # Rows without a completed score verdict never enter the queue.
        # strict-once: a row whose one valid retry already ran (attempt=1)
        # must NOT be re-admitted for a second draw.
        import contextlib, io
        from unittest import mock
        out = make_out(
            {"arvo:1": self.base_row(official_success=False,
                                     final={"fix_exit": 1, "vul_exit": 1},
                                     checks=None),
             "arvo:2": self.base_row(task_id="arvo:2", official_success=None,
                                     score_status="no_records", checks=0),
             "arvo:3": self.base_row(task_id="arvo:3", attempt=1,
                                     official_success=False,
                                     final={"fix_exit": 1, "vul_exit": 1},
                                     checks=None)},
            ledgers={"arvo_1": [{"poc_id": "p1"}], "arvo_3": [{"poc_id": "p3"}]})
        qpath = out / "retry_q.json"
        buf = io.StringIO()
        with mock.patch("sys.argv", ["failure_taxonomy.py", "--out", str(out),
                                     "--emit-retry-queue", str(qpath)]), \
             contextlib.redirect_stdout(buf):
            rc = ft.main()
        self.assertEqual(rc, 0)
        q = json.load(open(qpath, encoding="utf-8"))
        self.assertIn("arvo:1", q)          # scored False -> admitted
        self.assertNotIn("arvo:2", q)       # no verdict -> never admitted
        self.assertNotIn("arvo:3", q)       # retry already spent -> excluded

    def test_end_to_end_report(self):
        import contextlib, io
        from unittest import mock
        out = make_out(
            {"arvo:1": self.base_row(),
             "arvo:2": self.base_row(task_id="arvo:2", official_success=None,
                                     score_status="query_error", checks=None)},
            ledgers={"arvo_1": [{"poc_id": "p1"}]})
        buf = io.StringIO()
        with mock.patch("sys.argv", ["failure_taxonomy.py", "--out", str(out)]), \
             contextlib.redirect_stdout(buf):
            rc = ft.main()
        self.assertEqual(rc, 0)
        rep = json.load(open(out / "failure_taxonomy.json", encoding="utf-8"))
        self.assertEqual(rep["tasks"], 2)
        self.assertEqual(rep["classification_version"], "1.1")
        self.assertIn("unscored/query_error", rep["by_primary_class"])


if __name__ == "__main__":
    unittest.main()
