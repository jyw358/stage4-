"""R2/B10 regression tests for verify_stage4.py -- the acceptance checker
must fail (exit 1) on rounds that are merely self-consistent but have no
effective execution, and on every blind spot the review enumerated."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

AG = Path(__file__).resolve().parents[1]
VERIFY = AG / "scripts" / "verify_stage4.py"
from tests._scratch import cleanup, scratch_dir  # noqa: E402


def run_verify(out, queue=None, ws_root=None, soft=False):
    cmd = [sys.executable, str(VERIFY), "--out", str(out)]
    if queue:
        cmd += ["--queue", str(queue)]
    if ws_root:
        cmd += ["--ws-root", str(ws_root)]
    if soft:
        cmd += ["--soft"]
    return subprocess.run(cmd, capture_output=True, text=True)


def write_row(out, tid, row):
    (out / "rows").mkdir(parents=True, exist_ok=True)
    (out / "rows" / (tid.replace(":", "_") + ".json")).write_text(
        json.dumps(row), encoding="utf-8")


def rebuild(out, order):
    (out / "results.json").write_text(json.dumps(order), encoding="utf-8")


def ok_row(tid, **kw):
    base = {
        "task_id": tid, "run_id": "s4-t", "status": "submitted",
        "submissions": 1, "score_status": "ok", "official_success": True,
        "cost_usd": 0.5, "agent_id": "ag1", "ledger_source": "protected",
        "final": {"poc_id": "p1", "poc_sha256": "a" * 64, "poc_hash": "a" * 64,
                  "hash_check": "match", "vul_exit": 1, "fix_exit": 0,
                  "submitted_at": "2026-11-04T00:00:01Z", "agent_id": "ag1"},
    }
    base.update(kw)
    return base


def write_protected(out, tid, entries=("p1",)):
    d = out / "protected_ledger"
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"poc_id": e, "poc_sha256": "a" * 64})
             for e in entries]
    (d / (tid.replace(":", "_") + ".jsonl")).write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


class TestVerifyStage4(unittest.TestCase):
    def setUp(self):
        self.dir = scratch_dir()
        self.addCleanup(cleanup, self.dir)
        self.out = Path(self.dir) / "out"

    def queue_file(self, tids):
        q = Path(self.dir) / "queue.json"
        q.write_text(json.dumps(tids), encoding="utf-8")
        return q

    # ---- B10 repro #1: 50x gen_fail, self-consistent -> must FAIL ---------
    def test_all_gen_fail_is_rejected(self):
        row = {"task_id": "test:001", "run_id": "s4-test", "status": "gen_fail",
               "submissions": 0, "official_success": None, "cost_usd": 0}
        write_row(self.out, "test:001", row)
        rebuild(self.out, [row])
        q = self.queue_file(["test:001"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("coverage_infra", r.stdout)

    def test_worker_error_is_rejected(self):
        row = {"task_id": "t:1", "run_id": "s4", "status": "worker_error",
               "submissions": 0, "official_success": None, "cost_usd": 0}
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 1)
        self.assertIn("coverage_infra", r.stdout)

    # ---- happy path: a fully valid round passes with 0 violations ---------
    def test_valid_round_passes(self):
        row = ok_row("t:1")
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        write_protected(self.out, "t:1")
        (self.out / "scored").mkdir()
        (self.out / "scored" / "t_1.json").write_text(
            json.dumps({"task_id": "t:1", "official_success": True,
                        "score_status": "ok",
                        "final": {"poc_id": "p1"}}), encoding="utf-8")
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("violations=0", r.stdout)

    # ---- no_submission is an allowed terminal state ------------------------
    def test_no_submission_allowed(self):
        row = {"task_id": "t:1", "run_id": "s4", "status": "no_submission",
               "submissions": 0, "official_success": None,
               "score_status": "no_records", "cost_usd": 0.1}
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    # ---- blind spots from the review table ---------------------------------
    def test_workspace_only_ledger_is_rejected(self):
        row = ok_row("t:1", ledger_source="workspace")
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        write_protected(self.out, "t:1")  # file exists, but scoring used ws copy
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ledger_not_protected", r.stdout)

    def test_missing_protected_ledger_is_rejected(self):
        row = ok_row("t:1")
        row.pop("ledger_source")  # never scored from protected
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ledger_missing", r.stdout)

    def test_corrupt_protected_ledger_is_rejected(self):
        row = ok_row("t:1")
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        d = self.out / "protected_ledger"
        d.mkdir(parents=True)
        (d / "t_1.jsonl").write_text("NOT-JSON-AT-ALL\n", encoding="utf-8")
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ledger_corrupt", r.stdout)

    def test_same_score_different_cost_in_aggregate_is_rejected(self):
        row = ok_row("t:1")
        write_row(self.out, "t:1", row)
        drifted = dict(row, cost_usd=99.0)
        rebuild(self.out, [drifted])
        write_protected(self.out, "t:1")
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 1)
        self.assertIn("aggregate_row_mismatch", r.stdout)

    def test_scored_file_contradicting_row_is_rejected(self):
        row = ok_row("t:1")
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        write_protected(self.out, "t:1")
        (self.out / "scored").mkdir()
        (self.out / "scored" / "t_1.json").write_text(
            json.dumps({"task_id": "t:1", "official_success": False,
                        "score_status": "ok",
                        "final": {"poc_id": "p2"}}), encoding="utf-8")
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 1)
        self.assertIn("scored_mismatch", r.stdout)

    def test_renamed_intel_file_is_caught(self):
        row = ok_row("t:1")
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        write_protected(self.out, "t:1")
        ws_root = Path(self.dir) / "ws"
        ws = ws_root / "t_1"
        ws.mkdir(parents=True)
        (ws / "ws_manifest.json").write_text("{}", encoding="utf-8")
        (ws / "reference_intel.md").write_text("secret", encoding="utf-8")
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q, ws_root=ws_root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("isolation_intel", r.stdout)

    def test_missing_row_agent_id_is_rejected(self):
        row = ok_row("t:1")
        row.pop("agent_id")
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        write_protected(self.out, "t:1")
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 1)
        self.assertIn("agent_missing_row", r.stdout)

    def test_missing_cost_is_a_violation(self):
        row = ok_row("t:1", cost_usd=None)
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        write_protected(self.out, "t:1")
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 1)
        self.assertIn("cost_missing", r.stdout)

    def test_mixed_run_ids_rejected(self):
        r1 = ok_row("t:1")
        r2 = ok_row("t:2", run_id="s4-OTHER")
        write_row(self.out, "t:1", r1)
        write_row(self.out, "t:2", r2)
        rebuild(self.out, [r1, r2])
        write_protected(self.out, "t:1")
        write_protected(self.out, "t:2")
        q = self.queue_file(["t:1", "t:2"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 1)
        self.assertIn("run_mixed", r.stdout)

    def test_soft_flag_still_exits_zero(self):
        row = {"task_id": "t:1", "run_id": "s4", "status": "gen_fail",
               "submissions": 0, "official_success": None, "cost_usd": 0}
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q, soft=True)
        self.assertEqual(r.returncode, 0)
        report = json.load(open(self.out / "verify_stage4.json", encoding="utf-8"))
        self.assertEqual(report["coverage"]["infra_failed"], 1)

    # ---- R3: control-plane binding + ledger conflicts -----------------------
    def write_control(self, tid, run_id="s4-t", agent_id="ag1"):
        d = self.out / "control" / tid.replace(":", "_")
        d.mkdir(parents=True)
        (d / "run_id").write_text(run_id + "\n", encoding="utf-8")
        (d / "agent_id").write_text(agent_id + "\n", encoding="utf-8")

    def test_control_binding_mismatch_is_rejected(self):
        row = ok_row("t:1")
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        write_protected(self.out, "t:1")
        self.write_control("t:1", agent_id="ag-EVIL")   # forged binding
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 1)
        self.assertIn("control_mismatch", r.stdout)

    def test_control_missing_for_submitted_row_is_rejected(self):
        row = ok_row("t:1")
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        write_protected(self.out, "t:1")
        (self.out / "control").mkdir()      # R3 layout present, task dir absent
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 1)
        self.assertIn("control_missing", r.stdout)

    def test_control_agreement_passes(self):
        row = ok_row("t:1")
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        write_protected(self.out, "t:1")
        self.write_control("t:1")           # matches run_id + agent_id
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("violations=0", r.stdout)

    def test_ledger_conflict_file_is_a_violation(self):
        row = ok_row("t:1")
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        write_protected(self.out, "t:1")
        d = self.out / "protected_ledger"
        (d / "t_1.jsonl.conflicts.jsonl").write_text(
            json.dumps({"poc_id": "p1", "protected_sha256": "a" * 64,
                        "workspace_sha256": "f" * 64}) + "\n", encoding="utf-8")
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ledger_conflict", r.stdout)

    def test_evidence_error_is_an_infra_failure(self):
        # R3-M3: a promoter failure ends the task as evidence_error and must
        # be treated like any other infrastructure failure at acceptance.
        row = {"task_id": "t:1", "run_id": "s4", "status": "evidence_error",
               "submissions": 0, "official_success": None, "cost_usd": 0}
        write_row(self.out, "t:1", row)
        rebuild(self.out, [row])
        q = self.queue_file(["t:1"])
        r = run_verify(self.out, queue=q)
        self.assertEqual(r.returncode, 1)
        self.assertIn("coverage_infra", r.stdout)


if __name__ == "__main__":
    unittest.main()
