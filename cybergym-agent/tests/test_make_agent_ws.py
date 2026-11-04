import io
import json
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path

from scripts.make_agent_ws import assert_clean_workspace, assert_whitelist
from tests._scratch import cleanup, scratch_dir

AG = Path(__file__).resolve().parents[1]


class TestWorkspaceGenerator(unittest.TestCase):
    def setUp(self):
        self.dir = scratch_dir()
        self.addCleanup(cleanup, self.dir)
        self.task = self.dir / "tasks" / "arvo_1"
        self.task.mkdir(parents=True)
        (self.task / "description.txt").write_text("bug\n", encoding="utf-8")
        (self.task / "submit.sh").write_text(
            'curl -F \'metadata={"task_id": "t", "agent_id": "a", "checksum": "c"}\'\n',
            encoding="utf-8")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b"int main(void){return 0;}\n"
            info = tarfile.TarInfo("repo/foo.c")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        (self.task / "repo-vul.tar.gz").write_bytes(buf.getvalue())
        self.wsroot = self.dir / "ws"

    def run_gen(self, *extra):
        return subprocess.run(
            [sys.executable, str(AG / "scripts" / "make_agent_ws.py"),
             "--task-dir", str(self.task), "--task-id", "arvo:1",
             "--ws-root", str(self.wsroot), *extra],
            capture_output=True, text=True)

    def test_fresh_workspace_has_no_intel_and_manifest(self):
        r = self.run_gen("--fresh", "--max-turns", "33", "--checks", "7")
        self.assertEqual(r.returncode, 0, r.stderr)
        ws = self.wsroot / "arvo_1"
        self.assertFalse((ws / "reference-intel.md").exists())
        self.assertEqual((ws / ".checks").read_text(), "0")
        self.assertIn("hard turn limit 33", (ws / "CLAUDE.md").read_text(encoding="utf-8"))
        manifest = json.load(open(ws / "ws_manifest.json", encoding="utf-8"))
        self.assertEqual(manifest["reference_intel"], [])
        self.assertEqual(manifest["max_turns"], 33)
        self.assertIn("repo", [e["name"] for e in manifest["top_level"]])

    def test_existing_workspace_is_refused_without_fresh(self):
        self.assertEqual(self.run_gen("--fresh").returncode, 0)
        r = self.run_gen()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already exists", r.stderr)

    def test_fresh_replaces_stale_files(self):
        self.assertEqual(self.run_gen("--fresh").returncode, 0)
        ws = self.wsroot / "arvo_1"
        (ws / "poc.bin").write_bytes(b"stale")
        (ws / "SOLUTION.md").write_text("old", encoding="utf-8")
        self.assertEqual(self.run_gen("--fresh").returncode, 0)
        self.assertFalse((ws / "poc.bin").exists())
        self.assertFalse((ws / "SOLUTION.md").exists())

    def test_intel_gate(self):
        ws = self.dir / "ws2"
        ws.mkdir()
        (ws / "reference-intel.md").write_text("x", encoding="utf-8")
        with self.assertRaises(SystemExit):
            assert_clean_workspace(ws)

    def test_whitelist_rejects_stray_entry(self):
        ws = self.dir / "ws3"
        ws.mkdir()
        (ws / "CLAUDE.md").write_text("x", encoding="utf-8")
        assert_whitelist(ws)
        (ws / "stray.bin").write_bytes(b"x")
        with self.assertRaises(SystemExit):
            assert_whitelist(ws)


if __name__ == "__main__":
    unittest.main()
