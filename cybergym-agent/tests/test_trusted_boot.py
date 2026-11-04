"""R3 regression tests for the dedicated-user isolation work:

- root_code/assert_code_tree.py classification rules (the trusted boot-time
  audit; loaded BY PATH because it must live outside the audited tree);
- stage4.py control-plane binding (write/read roundtrip, reuse validation);
- per-task uid pool selection.

The classification and control-plane tests are OS-independent (pure logic or
POSIX-guarded internals); on the Windows sandbox the real-mode/lstat paths
are skipped, so the POSIX-only tests are guarded explicitly.
"""
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path

AG = Path(__file__).resolve().parents[1]
ROOT = AG.parent
sys.path.insert(0, str(AG))

from scripts import stage4  # noqa: E402
from tests._scratch import cleanup, scratch_dir  # noqa: E402


def load_assert_module():
    path = ROOT / "root_code" / "assert_code_tree.py"
    spec = importlib.util.spec_from_file_location("assert_code_tree", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def submit_sh_bytes(agent_id="agent42"):
    return ('curl -X POST http://x/submit-vul '
            f'-F \'metadata={{"task_id": "abc", "agent_id": "{agent_id}", '
            '"checksum": "z"}\'\n').encode("utf-8")


class TestAssertCodeTreeRules(unittest.TestCase):
    """The trusted audit's classification rules, exercised with synthetic
    lstat results (no filesystem needed)."""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_assert_module()

    def test_clean_entry_passes(self):
        self.assertIsNone(self.mod.entry_violation("a.py", 0o100644, 0, False))
        self.assertIsNone(self.mod.entry_violation("dir", 0o40755, 0, False))

    def test_symlink_is_refused(self):
        self.assertIn("symlink",
                      self.mod.entry_violation("evil.py", 0o120777, 0, True))

    def test_group_other_write_is_refused(self):
        self.assertIn("writable",
                      self.mod.entry_violation("a.py", 0o100666, 0, False))
        self.assertIn("writable",
                      self.mod.entry_violation("d", 0o40775, 0, False))

    def test_non_root_owner_is_refused(self):
        self.assertIn("owner uid=1000",
                      self.mod.entry_violation("a.py", 0o100644, 1000, False))

    def test_ancestor_rules(self):
        untrusted = frozenset({10001, 1000})
        self.assertIsNone(self.mod.ancestor_violation("/opt", 0o40755, 0, untrusted))
        self.assertIn("other-writable",
                      self.mod.ancestor_violation("/opt", 0o40777, 0, untrusted))
        self.assertIn("group-writable",
                      self.mod.ancestor_violation("/opt", 0o40770, 0, untrusted))
        self.assertIn("untrusted uid=10001",
                      self.mod.ancestor_violation("/home/naco", 0o40755, 10001,
                                                  untrusted))
        self.assertIn("owner uid=1005",
                      self.mod.ancestor_violation("/srv", 0o40755, 1005, untrusted))

    def test_module_imports_nothing_from_the_tree(self):
        # the audit must stay usable from /root/ even if the audited tree is
        # garbage: importing it must not pull plugins/ or scripts/
        src = (ROOT / "root_code" / "assert_code_tree.py").read_text(
            encoding="utf-8")
        self.assertNotIn("from plugins", src)
        self.assertNotIn("import plugins", src)
        self.assertNotIn("scripts.", src)


class TestControlPlane(unittest.TestCase):
    """<out>/control/<task>/ binding: write/read roundtrip + trust gating."""

    def setUp(self):
        self.dir = scratch_dir()
        self.addCleanup(cleanup, self.dir)
        self.out = Path(self.dir) / "out"

    def test_control_write_read_roundtrip(self):
        stage4.control_write(self.out, "t_1", "run_id", "s4-r")
        stage4.control_write(self.out, "t_1", "submit_sha256", "ab" * 32)
        self.assertEqual(stage4.control_read(self.out, "t_1"),
                         {"run_id": "s4-r", "submit_sha256": "ab" * 32})

    def test_control_write_overwrites_atomically(self):
        stage4.control_write(self.out, "t_1", "run_id", "s4-old")
        stage4.control_write(self.out, "t_1", "run_id", "s4-new")
        self.assertEqual(stage4.control_read(self.out, "t_1")["run_id"],
                         "s4-new")
        leftovers = [p.name for p in
                     (self.out / "control" / "t_1").iterdir()
                     if p.name.startswith(".")]
        self.assertEqual(leftovers, [])   # no temp file debris

    def test_missing_fields_read_as_absent(self):
        stage4.control_write(self.out, "t_1", "run_id", "s4-r")
        self.assertEqual(stage4.control_read(self.out, "t_2"), {})

    def test_defensive_tree_check_never_raises_off_posix(self):
        stage4._defensive_tree_check()   # returns early on non-root/Windows


class TestValidateReuse(unittest.TestCase):
    """R3/review Q5-C: reuse requires the control-plane binding to still
    match submit.sh's content -- the workspace marker alone is not trust."""

    def setUp(self):
        self.dir = scratch_dir()
        self.addCleanup(cleanup, self.dir)
        self.tdir = Path(self.dir) / "tasks" / "t_1"
        self.tdir.mkdir(parents=True)
        (self.tdir / "submit.sh").write_bytes(submit_sh_bytes())
        self.sha = hashlib.sha256(submit_sh_bytes()).hexdigest()
        self.binding = {"run_id": "s4-r", "submit_sha256": self.sha,
                        "agent_id": "agent42"}

    def test_matching_binding_reuses(self):
        ok, why = stage4.validate_reuse(self.tdir, self.binding, "s4-r")
        self.assertTrue(ok, why)

    def test_no_binding_is_refused(self):
        ok, why = stage4.validate_reuse(self.tdir, {}, "s4-r")
        self.assertFalse(ok)
        self.assertIn("no control-plane binding", why)

    def test_foreign_run_binding_is_refused(self):
        ok, why = stage4.validate_reuse(
            self.tdir, dict(self.binding, run_id="s4-OLD"), "s4-r")
        self.assertFalse(ok)
        self.assertIn("belongs to run", why)

    def test_modified_submit_sh_is_refused(self):
        (self.tdir / "submit.sh").write_bytes(submit_sh_bytes("agent-EVIL"))
        ok, why = stage4.validate_reuse(self.tdir, self.binding, "s4-r")
        self.assertFalse(ok)
        self.assertIn("changed since prep", why)

    def test_agent_id_mismatch_is_refused(self):
        (self.tdir / "submit.sh").write_bytes(submit_sh_bytes("agent-EVIL"))
        forged = dict(self.binding, submit_sha256=hashlib.sha256(
            submit_sh_bytes("agent-EVIL")).hexdigest())
        ok, why = stage4.validate_reuse(self.tdir, forged, "s4-r")
        self.assertFalse(ok)
        self.assertIn("agent_id", why)


class TestUidPool(unittest.TestCase):
    """R3: one numeric uid per task, drawn from a never-reused pool."""

    def test_skips_taken_uids(self):
        self.assertEqual(stage4.pick_free_uid(30001, {30001}), 30002)
        self.assertEqual(stage4.pick_free_uid(30001, {30001, 30002}), 30003)

    def test_skips_existing_passwd_uids(self):
        uid = stage4.pick_free_uid(30001, set(), exists=lambda u: u == 30001)
        self.assertEqual(uid, 30002)

    def test_pool_exhaustion_raises(self):
        with self.assertRaises(RuntimeError):
            stage4.pick_free_uid(30001, set(), exists=lambda u: True, span=4)

    def test_start_value_from_env(self):
        self.assertEqual(stage4.AGENT_UID_BASE, 30001)


if __name__ == "__main__":
    unittest.main()
