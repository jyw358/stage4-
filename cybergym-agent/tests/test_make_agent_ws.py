import io
import json
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path

from scripts.make_agent_ws import (OPTIONAL_TOOLS, REQUIRED_TOOLS,
                                   SAFETY_HOOKS, assert_clean_workspace,
                                   assert_whitelist)
from tests._scratch import cleanup, scratch_dir

AG = Path(__file__).resolve().parents[1]

PY_STUB = "#!/usr/bin/env python3\nprint('stub')\n"
SH_STUB = "#!/bin/sh\ntrue\n"


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
        # r9 P0-1: every generation now needs a trusted tool source dir
        self.toolsrc = self.dir / "toolsrc"
        self.toolsrc.mkdir()
        self.stage_tools()

    def stage_tools(self, skip=()):
        for t in REQUIRED_TOOLS + OPTIONAL_TOOLS + SAFETY_HOOKS:
            if t in skip:
                continue
            stub = SH_STUB if t.endswith(".sh") else PY_STUB
            (self.toolsrc / t).write_text(stub, encoding="utf-8")

    def run_gen(self, *extra):
        return subprocess.run(
            [sys.executable, str(AG / "scripts" / "make_agent_ws.py"),
             "--task-dir", str(self.task), "--task-id", "arvo:1",
             "--ws-root", str(self.wsroot), "--tool-src", str(self.toolsrc),
             *extra],
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
        # r9 P0-1: toolchain provenance recorded, required tools shipped
        for t in REQUIRED_TOOLS:
            entry = manifest["tools"]["required"][t]
            self.assertEqual(len(entry["sha256"]), 64)
            self.assertTrue((ws / t).is_file())

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
        assert_whitelist(ws, {"CLAUDE.md"})
        (ws / "stray.bin").write_bytes(b"x")
        with self.assertRaises(SystemExit):
            assert_whitelist(ws, {"CLAUDE.md"})

    # ---- r9 P0-1 fail-closed toolchain tests -------------------------------

    def test_required_tool_missing_refuses(self):
        (self.toolsrc / "repo_map.py").unlink()
        r = self.run_gen("--fresh")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("REFUSE", r.stderr)
        self.assertIn("REQUIRED", r.stderr)
        # fail-closed means NOTHING was written (preflight precedes mkdir)
        self.assertFalse((self.wsroot / "arvo_1").exists())

    def test_safety_hook_missing_refuses(self):
        (self.toolsrc / "verify_gate.py").unlink()
        r = self.run_gen("--fresh")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("REFUSE", r.stderr)
        self.assertIn("SAFETY_HOOK", r.stderr)
        self.assertFalse((self.wsroot / "arvo_1").exists())

    def test_optional_tool_missing_degrades_not_refuses(self):
        (self.toolsrc / "directed_fuzz.py").unlink()
        r = self.run_gen("--fresh")
        self.assertEqual(r.returncode, 0, r.stderr)
        ws = self.wsroot / "arvo_1"
        manifest = json.load(open(ws / "ws_manifest.json", encoding="utf-8"))
        self.assertEqual(manifest["tools"]["optional"]["directed_fuzz.py"],
                         "unavailable")
        # prompt degradation: the workflow must not reference a missing tool
        self.assertNotIn("directed_fuzz", (ws / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertFalse((ws / "directed_fuzz.py").exists())
        self.assertIn("unavailable", r.stderr)

    def test_safety_hook_lands_in_workspace(self):
        self.assertEqual(self.run_gen("--fresh").returncode, 0)
        ws = self.wsroot / "arvo_1"
        hook = ws / ".claude" / "verify_gate.py"
        self.assertTrue(hook.is_file())
        settings = json.load(open(ws / ".claude" / "settings.json",
                                  encoding="utf-8"))
        self.assertIn("Stop", settings["hooks"])
        manifest = json.load(open(ws / "ws_manifest.json", encoding="utf-8"))
        self.assertEqual(len(manifest["tools"]["safety_hooks"]["verify_gate.py"]["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
