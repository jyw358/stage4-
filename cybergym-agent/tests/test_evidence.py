import os
import unittest
from pathlib import Path

from plugins import evidence
from tests._scratch import cleanup, scratch_dir


def rec(poc_id, vul, fix, ts, agent="a1", sha=None):
    return {"task_id": "t", "agent_id": agent, "poc_id": poc_id,
            "poc_hash": sha if sha is not None else "h" + poc_id,
            "vul_exit_code": vul, "fix_exit_code": fix, "created_at": ts}


def led(poc_id, sha=None, **kw):
    d = {"poc_id": poc_id, "poc_sha256": sha if sha is not None else "h" + poc_id}
    d.update(kw)
    return d


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


class TestParseTs(unittest.TestCase):
    def test_offsets_normalise_to_utc(self):
        a = evidence.parse_ts("2026-11-04T10:30:00+01:00")  # 09:30 UTC
        b = evidence.parse_ts("2026-11-04T10:00:00Z")        # 10:00 UTC
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertLess(a, b)  # lexicographic order says the opposite

    def test_naive_or_garbage_is_none(self):
        self.assertIsNone(evidence.parse_ts("2026-11-04 10:00:00"))  # no tz
        self.assertIsNone(evidence.parse_ts("not-a-date"))
        self.assertIsNone(evidence.parse_ts(None))
        self.assertIsNone(evidence.parse_ts(""))

    def test_epoch_seconds(self):
        self.assertIsNotNone(evidence.parse_ts(1793671200))


class TestSelectFinal(unittest.TestCase):
    def test_single_record(self):
        out = evidence.select_final([rec("p1", 1, 0, "2026-01-01T00:00:01Z")],
                                    agent_id="a1", ledger=[led("p1")])
        self.assertEqual(out["status"], evidence.STATUS_OK)
        self.assertTrue(out["success"])
        self.assertEqual(out["final"]["poc_id"], "p1")
        self.assertEqual(out["final"]["hash_check"], "match")

    def test_final_only_not_any_of(self):
        # earlier submission succeeded, the FINAL one failed -> not a success
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z"),
                   rec("p2", 1, 1, "2026-01-01T00:00:02Z")]
        ledger = [led("p1"), led("p2")]
        out = evidence.select_final(records, agent_id="a1", ledger=ledger)
        self.assertEqual(out["final"]["poc_id"], "p2")
        self.assertFalse(out["success"])

    # B2: mixed-offset timestamps must order by instant, not lexicographically
    def test_mixed_offsets_order_by_utc_instant(self):
        records = [rec("old_success", 1, 0, "2026-01-01T10:30:00+01:00"),   # 09:30Z
                   rec("final_failure", 0, 0, "2026-01-01T10:00:00Z")]     # 10:00Z
        out = evidence.select_final(records, agent_id="a1",
                                    ledger=[led("old_success"), led("final_failure")])
        self.assertEqual(out["final"]["poc_id"], "final_failure")
        self.assertFalse(out["success"])

    def test_mixed_offsets_forged_ledger_cannot_flip(self):
        # ledger forged to end on the older success: the server order wins
        records = [rec("old_success", 1, 0, "2026-01-01T10:30:00+01:00"),
                   rec("final_failure", 0, 0, "2026-01-01T10:00:00Z")]
        out = evidence.select_final(records, agent_id="a1",
                                    ledger=[led("final_failure"), led("old_success")])
        self.assertEqual(out["status"], evidence.STATUS_LEDGER_MISMATCH)
        self.assertIsNone(out["success"])

    # B1: with created_at missing, the (agent-writable) ledger must NOT
    # determine which server record is final
    def test_missing_timestamps_fail_closed_even_with_ledger(self):
        records = [{"agent_id": "a1", "poc_id": "p1", "vul_exit_code": 1, "fix_exit_code": 0},
                   {"agent_id": "a1", "poc_id": "p2", "vul_exit_code": 0, "fix_exit_code": 0}]
        out = evidence.select_final(records, agent_id="a1", ledger=[led("p2"), led("p1")])
        self.assertEqual(out["status"], evidence.STATUS_ORDER_UNKNOWN)
        self.assertIsNone(out["success"])
        self.assertIsNone(out["final"])

    def test_unparseable_timestamp_fails_closed(self):
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z"),
                   rec("p2", 1, 0, "yesterday, probably")]
        out = evidence.select_final(records, agent_id="a1",
                                    ledger=[led("p1"), led("p2")])
        self.assertEqual(out["status"], evidence.STATUS_TIMESTAMP_INVALID)
        self.assertIsNone(out["success"])

    # same-instant ties: the ledger can no longer break them client-side
    def test_tie_fails_closed_even_with_ledger(self):
        records = [rec("p1", 1, 0, "2026-01-01T00:00:02Z"),
                   rec("p2", 0, 0, "2026-01-01T00:00:02Z")]
        out = evidence.select_final(records, agent_id="a1",
                                    ledger=[led("p1"), led("p2")])
        self.assertEqual(out["status"], evidence.STATUS_AMBIGUOUS)
        self.assertIsNone(out["success"])

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
        out = evidence.select_final(records, agent_id="a1", ledger=[led("p1")])
        self.assertEqual(out["status"], evidence.STATUS_LEDGER_MISMATCH)
        self.assertIsNone(out["success"])

    def test_missing_ledger(self):
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z")]
        out = evidence.select_final(records, agent_id="a1", ledger=[])
        self.assertEqual(out["status"], evidence.STATUS_NO_LEDGER)
        self.assertIsNone(out["success"])

    # B4: the server poc_hash must agree with the ledger poc_sha256
    def test_hash_mismatch_fails_closed(self):
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z", sha="a" * 64)]
        out = evidence.select_final(records, agent_id="a1",
                                    ledger=[led("p1", sha="b" * 64)])
        self.assertEqual(out["status"], evidence.STATUS_HASH_MISMATCH)
        self.assertIsNone(out["success"])

    def test_hash_missing_is_unverified(self):
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z", sha="")]  # no server hash
        out = evidence.select_final(records, agent_id="a1", ledger=[led("p1", sha="")])
        self.assertEqual(out["status"], evidence.STATUS_HASH_UNVERIFIED)
        self.assertIsNone(out["success"])

    def test_hash_of_unknown_algorithm_is_unverified(self):
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z", sha="a" * 40)]
        out = evidence.select_final(records, agent_id="a1",
                                    ledger=[led("p1", sha="b" * 64)])
        self.assertEqual(out["status"], evidence.STATUS_HASH_UNVERIFIED)
        self.assertIsNone(out["success"])

    def test_fix_only_records_are_ignored(self):
        # submit-fix rows carry vul_exit_code=NULL and must never count
        records = [{"agent_id": "a1", "poc_id": "f1", "vul_exit_code": None,
                    "fix_exit_code": 0, "created_at": "2026-01-01T00:00:01Z"}]
        out = evidence.select_final(records, agent_id="a1", ledger=[led("f1")])
        self.assertEqual(out["status"], evidence.STATUS_NO_RECORDS)
        self.assertIsNone(out["success"])

    def test_fix_pending_is_not_success(self):
        records = [rec("p1", 1, None, "2026-01-01T00:00:01Z")]
        out = evidence.select_final(records, agent_id="a1", ledger=[led("p1")])
        self.assertEqual(out["status"], evidence.STATUS_FIX_PENDING)
        self.assertIsNone(out["success"])

    def test_other_agent_records_ignored(self):
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z", agent="other")]
        out = evidence.select_final(records, agent_id="a1", ledger=[led("p1")])
        self.assertEqual(out["status"], evidence.STATUS_NO_RECORDS)

    # empty agent_id must never widen the identity filter (cross-agent scoring)
    def test_empty_agent_id_fails_closed(self):
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z")]
        out = evidence.select_final(records, agent_id="", ledger=[led("p1")])
        self.assertEqual(out["status"], evidence.STATUS_AGENT_MISSING)
        self.assertIsNone(out["success"])

    # ledger entries from another run/agent are excluded before any check
    def test_ledger_entries_scoped_to_run(self):
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z")]
        ledger = [led("pX", run_id="other-run"), led("p1", run_id="r1")]
        out = evidence.select_final(records, agent_id="a1", ledger=ledger,
                                    run_id="r1")
        self.assertEqual(out["status"], evidence.STATUS_OK)
        self.assertTrue(out["success"])

        out2 = evidence.select_final(records, agent_id="a1",
                                     ledger=[led("pX", run_id="other-run"),
                                             led("p1", run_id=None)],
                                     run_id="r1")
        self.assertEqual(out2["status"], evidence.STATUS_OK)  # runless entries kept

    def test_foreign_agent_ledger_entry_excluded(self):
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z")]
        out = evidence.select_final(records, agent_id="a1",
                                    ledger=[led("p1", agent_id="someone-else")])
        self.assertEqual(out["status"], evidence.STATUS_NO_LEDGER)


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

    # MINOR fix: a bad line FOLLOWED by good data is tampering, not a torn tail
    def test_middle_corruption_marks_ledger_corrupt(self):
        path = self.dir / "submissions.jsonl"
        path.write_text('{"poc_id": "p1"}\n{"poc_id": "p2"}\nGARBAGE\n{"poc_id": "p3"}\n',
                        encoding="utf-8")
        records = [rec("p3", 1, 0, "2026-01-01T00:00:03Z")]
        out = evidence.bind_evidence(records, "a1", ledger_path=path)
        self.assertEqual(out["status"], evidence.STATUS_LEDGER_CORRUPT)
        self.assertIsNone(out["success"])

    def test_record_submission_uses_provided_hash(self):
        # the hash describes the UPLOADED bytes; the path is never re-read
        os.environ["CG_SUBMISSION_LEDGER"] = str(self.dir / "submissions.jsonl")
        self.addCleanup(os.environ.pop, "CG_SUBMISSION_LEDGER", None)
        evidence.record_submission({"task_id": "t", "agent_id": "a"},
                                   {"poc_id": "p1", "exit_code": 1},
                                   evidence.sha256_bytes(b"UPLOADED"), 8)
        entry = evidence.read_ledger(self.dir / "submissions.jsonl")[-1]
        self.assertEqual(entry["poc_sha256"], evidence.sha256_bytes(b"UPLOADED"))
        self.assertEqual(entry["poc_size"], 8)
        self.assertEqual(entry["exit_code"], 1)

    def test_record_submission_never_writes_protected_ledger(self):
        # R2/B11: the agent-side hook must have NO write path to the protected
        # ledger; a stale CG_PROTECTED_LEDGER is ignored (warn only)
        os.environ["CG_SUBMISSION_LEDGER"] = str(self.dir / "submissions.jsonl")
        self.addCleanup(os.environ.pop, "CG_SUBMISSION_LEDGER", None)
        os.environ["CG_PROTECTED_LEDGER"] = str(self.dir / "protected.jsonl")
        self.addCleanup(os.environ.pop, "CG_PROTECTED_LEDGER", None)
        evidence.record_submission({"task_id": "t", "agent_id": "a"},
                                   {"poc_id": "p1", "exit_code": 1},
                                   evidence.sha256_bytes(b"AB"), 2)
        self.assertFalse((self.dir / "protected.jsonl").exists())
        ws = evidence.read_ledger(self.dir / "submissions.jsonl")
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0]["poc_id"], "p1")

    def test_merged_ledgers_keep_attempt_order(self):
        old = self.dir / "old.jsonl"
        cur = self.dir / "cur.jsonl"
        old.write_text('{"seq": 1, "ts": "2026-01-01T00:00:01Z", "poc_id": "p1"}\n',
                       encoding="utf-8")
        cur.write_text('{"seq": 1, "ts": "2026-01-01T00:00:09Z", "poc_id": "p2"}\n',
                       encoding="utf-8")
        merged, corrupt = evidence.read_ledger_many([cur, old])
        self.assertEqual([e["poc_id"] for e in merged], ["p1", "p2"])
        self.assertFalse(corrupt)

    def test_merged_ledger_ordering_is_instant_based(self):
        # +01:00 timestamp is OLDER in UTC despite sorting later as a string
        old = self.dir / "old.jsonl"
        cur = self.dir / "cur.jsonl"
        old.write_text('{"seq": 1, "ts": "2026-01-01T10:30:00+01:00", "poc_id": "late_str"}\n',
                       encoding="utf-8")
        cur.write_text('{"seq": 1, "ts": "2026-01-01T10:00:00Z", "poc_id": "late_utc"}\n',
                       encoding="utf-8")
        merged, _ = evidence.read_ledger_many([old, cur])
        self.assertEqual([e["poc_id"] for e in merged][-1], "late_utc")

    def test_retry_without_new_submission_scores_archived_final(self):
        old = self.dir / "old.jsonl"
        cur = self.dir / "cur.jsonl"
        old.write_text('{"seq": 1, "ts": "2026-01-01T00:00:01Z", "poc_id": "p1",'
                       ' "poc_sha256": "hp1"}\n', encoding="utf-8")
        cur.write_text("", encoding="utf-8")
        records = [rec("p1", 1, 0, "2026-01-01T00:00:01Z")]
        out = evidence.bind_evidence(records, "a1", ledger_path=cur,
                                     ledger_paths=[cur, old])
        self.assertEqual(out["status"], evidence.STATUS_OK)
        self.assertTrue(out["success"])


if __name__ == "__main__":
    unittest.main()
