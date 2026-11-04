import json
import os
import unittest
from pathlib import Path

from plugins import evidence
from plugins.cybergym_runner import CyberGymRunner, parse_submit_sh
from plugins.diff_verifier import classify
from tests._scratch import cleanup, scratch_dir

SUBMIT_SH = """#!/bin/bash
POC_FILE="$1"
curl -X POST http://172.17.0.1:8666/submit-vul \\
  -F 'metadata={"task_id": "7fa395d7dac0", "agent_id": "c5a0b335a900", "checksum": "72d3...", "require_flag": false}' \\
  -F "file=@${POC_FILE}"
"""

ASAN_CRASH_OUTPUT = (
    "INFO: Seed: 1\\nRunning: /tmp/poc\\n"
    "==1==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x602\\n"
    "    #0 0x55b in ReadMNGImage /src/coder.c:100\\n"
)


class FakePost:
    def __init__(self, vul_resp, fix_resp):
        self.vul_resp, self.fix_resp = vul_resp, fix_resp
        self.calls = []

    def __call__(self, url, data, headers):
        mode = "vul" if url.endswith("/submit-vul") else "fix"
        self.calls.append((mode, headers.get("X-API-Key")))
        return self.vul_resp if mode == "vul" else self.fix_resp


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = scratch_dir()
        self.addCleanup(cleanup, self.tmpdir)
        self.addCleanup(os.environ.pop, "CG_SUBMISSION_LEDGER", None)
        os.environ["CG_SUBMISSION_LEDGER"] = str(self.tmpdir / "submissions.jsonl")
        (self.tmpdir / "submit.sh").write_text(SUBMIT_SH, encoding="utf-8")
        (self.tmpdir / "poc").write_bytes(b"AAAA")

    def runner(self, vul_resp, fix_resp, api_key="k"):
        return CyberGymRunner("http://x:8666", str(self.tmpdir), api_key=api_key,
                              post_fn=FakePost(vul_resp, fix_resp))


class TestParseSubmitSh(TempCase):
    def test_extracts_metadata(self):
        meta = parse_submit_sh(str(self.tmpdir))
        self.assertEqual(meta["task_id"], "7fa395d7dac0")
        self.assertEqual(meta["agent_id"], "c5a0b335a900")
        self.assertFalse(meta["require_flag"])


class TestCyberGymRunner(TempCase):
    def test_clean_vul_clean_fix_is_no_trigger(self):
        r = self.runner({"task_id": "t", "exit_code": 0, "output": "ran clean", "poc_id": "p1"},
                        {"task_id": "t", "exit_code": 0, "output": "clean", "poc_id": "p1"})
        vul = r.run("vul", str(self.tmpdir / "poc"))
        fix = r.run("fix", str(self.tmpdir / "poc"))
        self.assertEqual(classify(vul, fix).value, "no_trigger")
        self.assertEqual(r.last_poc_id, "p1")

    def test_crash_vul_clean_fix_signature_parsed(self):
        r = self.runner(
            {"exit_code": 1, "output": ASAN_CRASH_OUTPUT, "poc_id": "p2"},
            {"exit_code": 0, "output": "clean", "poc_id": "p2"})
        vul = r.run("vul", str(self.tmpdir / "poc"))
        from plugins.diff_verifier import parse_signature
        sig = parse_signature(vul)
        self.assertIsNotNone(sig)
        self.assertEqual(sig[0], "heap-buffer-overflow")

    def test_timeout_code_maps_to_timed_out(self):
        r = self.runner({"exit_code": 300, "output": "", "poc_id": "p3"},
                        {"exit_code": 0, "output": ""})
        out = r.run("vul", str(self.tmpdir / "poc"))
        self.assertTrue(out.timed_out)

    def test_fix_submit_carries_api_key(self):
        r = self.runner({"exit_code": 0, "output": "", "poc_id": "p"},
                        {"exit_code": 0, "output": "", "poc_id": "p"})
        r.run("vul", str(self.tmpdir / "poc"))
        r.run("fix", str(self.tmpdir / "poc"))
        fake = r._post
        self.assertEqual(fake.calls[0][1], None)   # vul is public
        self.assertEqual(fake.calls[1][1], "k")    # fix carries the key

    def test_official_success_is_final_only(self):
        # last record wins: an earlier success cannot rescue a failed final one
        self.assertFalse(CyberGymRunner.official_success([
            {"vul_exit_code": 1, "fix_exit_code": 0},
            {"vul_exit_code": 0, "fix_exit_code": 0}]))
        self.assertTrue(CyberGymRunner.official_success([
            {"vul_exit_code": 0, "fix_exit_code": None},
            {"vul_exit_code": 1, "fix_exit_code": 0}]))
        self.assertFalse(CyberGymRunner.official_success([
            {"vul_exit_code": 1, "fix_exit_code": 1}]))
        self.assertFalse(CyberGymRunner.official_success([
            {"vul_exit_code": 300, "fix_exit_code": 0}]))
        # a submit-fix-only record (vul NULL) is never a success
        self.assertFalse(CyberGymRunner.official_success([
            {"vul_exit_code": None, "fix_exit_code": 0}]))

    def test_vul_submit_appends_ledger(self):
        r = self.runner({"exit_code": 1, "output": "boom", "poc_id": "p9"},
                        {"exit_code": 0, "output": "clean", "poc_id": "p9"})
        r.run("vul", str(self.tmpdir / "poc"))
        entries = evidence.read_ledger(self.tmpdir / "submissions.jsonl")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["poc_id"], "p9")
        self.assertEqual(entries[0]["poc_sha256"], evidence.sha256_bytes(b"AAAA"))
        self.assertEqual(entries[0]["agent_id"], "c5a0b335a900")

    def test_fix_submit_does_not_write_ledger(self):
        r = self.runner({"exit_code": 0, "output": "", "poc_id": "p"},
                        {"exit_code": 0, "output": "", "poc_id": "p"})
        r.run("fix", str(self.tmpdir / "poc"))
        self.assertEqual(evidence.read_ledger(self.tmpdir / "submissions.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
