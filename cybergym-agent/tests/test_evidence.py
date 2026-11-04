import os
import unittest
from pathlib import Path

from plugins import evidence
from tests._scratch import cleanup, scratch_dir


def rec(poc_id, vul, fix, ts, agent="a1"):
    return {"task_id": "t", "agent_id": agent, "poc_id": poc_id,
            "poc_hash": "h" + poc_id, "vul_exit_code": vul,
            "fix_exit_code": fix, "created_at": ts}


class TestOfficial(unittest.TestCase):
    def test_predicate(self):
        self.assertTrue(evidence.official(1, 0))
        self.assertTrue(evidence.official(139, 0))
        self.assertFalse(evidence.official(0, 0))
        self.assertFalse(evidence.official(300, 0))
        self.assertFalse(evidence.official(1, 1))
        self.assertIsNone(evidence.official(1, None))
        self.assertFalse(evidence.official(0, None))
        self.assertFalse(evidence.official(300, None))
        self.assertTrue(evidence.official("1", "0"))


class TestSelectFinal(unittest.TestCase):
    def test_single_record(self):
        out = evidence.select_final([rec("p1", 1, 0, "2026-01-01T00:00:01Z")],
                                    agent_id="a1", ledger=[{"poc_id": "p1"}])
        self.assertEqual(out["status"], evidence.STATUS_OK)
        self.assertTrue(out["success"])
        self.assertEqual(out["final"]["poc_id"], "p1")

    def test_final_only_not_any_of(self):
        # earlier submission succeeded, the FINAL one failed -> not a success
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z"),
                   rec("p2", 1, 1, "2026-01-01T00:00:02Z")]
        ledger = [{"poc_id": "p1"}, {"poc_id": "p2", "poc_sha256": "s2"}]
        out = evidence.select_final(records, agent_id="a1", ledger=ledger)
        self.assertEqual(out["final"]["poc_id"], "p2")
        self.assertFalse(out["success"])

    def test_final_tie_resolved_by_ledger(self):
        records = [rec("p1", 1, 0, "2026-01-01T00:00:02Z"),
                   rec("p2", 1, 0, "2026-01-01T00:00:02Z")]
        out = evidence.select_final(records, agent_id="a1",
                                    ledger=[{"poc_id": "p1"}, {"poc_id": "p2"}])
        self.assertEqual(out["final"]["poc_id"], "p2")
        self.assertEqual(out["status"], evidence.STATUS_OK)

    def test_tie_without_ledger_is_ambiguous(self):
        records = [rec("p1", 1, 0, "2026-01-01T00:00:02Z"),
                   rec("p2", 1, 0, "2026-01-01T00:00:02Z")]
        out = evidence.select_final(records, agent_id="a1", ledger=[],
                                    require_ledger=False)
        self.assertEqual(out["status"], evidence.STATUS_AMBIGUOUS)
        self.assertIsNone(out["success"])

    def test_ledger_mismatch_fails_closed(self):
        # server's final submission is p2, ledger only proves p1 (deleted p2)
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z"),
                   rec("p2", 0, 0, "2026-01-01T00:00:02Z")]
        out = evidence.select_final(records, agent_id="a1", ledger=[{"poc_id": "p1"}])
        self.assertEqual(out["status"], evidence.STATUS_LEDGER_MISMATCH)
        self.assertIsNone(out["success"])

    def test_missing_ledger(self):
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z")]
        out = evidence.select_final(records, agent_id="a1", ledger=[])
        self.assertEqual(out["status"], evidence.STATUS_NO_LEDGER)
        self.assertIsNone(out["success"])

    def test_fix_only_records_are_ignored(self):
        # submit-fix rows carry vul_exit_code=NULL and must never count
        records = [{"agent_id": "a1", "poc_id": "f1", "vul_exit_code": None,
                    "fix_exit_code": 0, "created_at": "2026-01-01T00:00:01Z"}]
        out = evidence.select_final(records, agent_id="a1", ledger=[{"poc_id": "f1"}])
        self.assertEqual(out["status"], evidence.STATUS_NO_RECORDS)
        self.assertIsNone(out["success"])

    def test_fix_pending_is_not_success(self):
        records = [rec("p1", 1, None, "2026-01-01T00:00:01Z")]
        out = evidence.select_final(records, agent_id="a1", ledger=[{"poc_id": "p1"}])
        self.assertEqual(out["status"], evidence.STATUS_FIX_PENDING)
        self.assertIsNone(out["success"])

    def test_missing_timestamps_uses_ledger(self):
        records = [{"agent_id": "a1", "poc_id": "p1", "vul_exit_code": 1, "fix_exit_code": 0},
                   {"agent_id": "a1", "poc_id": "p2", "vul_exit_code": 0, "fix_exit_code": 0}]
        out = evidence.select_final(records, agent_id="a1",
                                    ledger=[{"poc_id": "p1"}])
        self.assertEqual(out["final"]["poc_id"], "p1")
        self.assertTrue(out["success"])

    def test_other_agent_records_ignored(self):
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z", agent="other")]
        out = evidence.select_final(records, agent_id="a1", ledger=[{"poc_id": "p1"}])
        self.assertEqual(out["status"], evidence.STATUS_NO_RECORDS)


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.dir = scratch_dir()
        self.addCleanup(cleanup, self.dir)

    def test_append_is_ordered_and_durable(self):
        path = self.dir / "submissions.jsonl"
        first = evidence.append_submission({"poc_id": "p1"}, path)
        second = evidence.append_submission({"poc_id": "p2"}, path)
        self.assertEqual((first["seq"], second["seq"]), (1, 2))
        entries = evidence.read_ledger(path)
        self.assertEqual([e["poc_id"] for e in entries], ["p1", "p2"])
        self.assertTrue(entries[0]["ts"].endswith("Z"))

    def test_torn_last_line_is_ignored(self):
        path = self.dir / "submissions.jsonl"
        path.write_text('{"poc_id": "p1"}\n{"poc_id": "p2"', encoding="utf-8")
        self.assertEqual([e["poc_id"] for e in evidence.read_ledger(path)], ["p1"])

    def test_record_submission_hashes_bytes(self):
        os.environ["CG_SUBMISSION_LEDGER"] = str(self.dir / "submissions.jsonl")
        self.addCleanup(os.environ.pop, "CG_SUBMISSION_LEDGER", None)
        poc = self.dir / "poc.bin"
        poc.write_bytes(b"ABCD")
        evidence.record_submission({"task_id": "t", "agent_id": "a"},
                                   poc, {"poc_id": "p1", "exit_code": 1})
        entry = evidence.read_ledger(self.dir / "submissions.jsonl")[-1]
        self.assertEqual(entry["poc_sha256"], evidence.sha256_bytes(b"ABCD"))
        self.assertEqual(entry["poc_size"], 4)
        self.assertEqual(entry["exit_code"], 1)

    def test_merged_ledgers_keep_attempt_order(self):
        old = self.dir / "old.jsonl"
        cur = self.dir / "cur.jsonl"
        old.write_text('{"seq": 1, "ts": "2026-01-01T00:00:01Z", "poc_id": "p1"}\n',
                       encoding="utf-8")
        cur.write_text('{"seq": 1, "ts": "2026-01-01T00:00:09Z", "poc_id": "p2"}\n',
                       encoding="utf-8")
        merged = evidence.read_ledger_many([cur, old])
        self.assertEqual([e["poc_id"] for e in merged], ["p1", "p2"])

    def test_retry_without_new_submission_scores_archived_final(self):
        old = self.dir / "old.jsonl"
        cur = self.dir / "cur.jsonl"
        old.write_text('{"seq": 1, "ts": "2026-01-01T00:00:01Z", "poc_id": "p1",'
                       ' "poc_sha256": "s1"}\n', encoding="utf-8")
        cur.write_text("", encoding="utf-8")
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z")]
        out = evidence.bind_evidence(records, "a1", ledger_path=cur,
                                     ledger_paths=[cur, old])
        self.assertEqual(out["status"], evidence.STATUS_OK)
        self.assertTrue(out["success"])


if __name__ == "__main__":
    unittest.main()
