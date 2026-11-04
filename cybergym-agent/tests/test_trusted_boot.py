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
import os
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


class TestUidRegistry(unittest.TestCase):
    """R3-B2 (review): a per-run alloc file only proves 'not reused in THIS
    run'.  The host-wide registry must keep a uid burned forever, so a new
    run with a different --out can never hand 30001 to another task."""

    def setUp(self):
        self.dir = scratch_dir()
        self.addCleanup(cleanup, self.dir)
        self._saved = stage4.UID_REGISTRY
        stage4.UID_REGISTRY = str(Path(self.dir) / "uid_registry.json")
        self.addCleanup(setattr, stage4, "UID_REGISTRY", self._saved)

    def _issue(self, out, name, run="s4-r"):
        return stage4.allocate_agent_uid(Path(out), name, run)

    def test_first_allocation_is_pool_base(self):
        uid = self._issue(Path(self.dir) / "outA", "t_A")
        self.assertEqual(uid, 30001)

    def test_second_run_cannot_reuse_the_uid(self):
        a = self._issue(Path(self.dir) / "outA", "t_A", "run-1")
        b = self._issue(Path(self.dir) / "outB", "t_B", "run-2")
        self.assertEqual(a, 30001)
        self.assertEqual(b, 30002)   # old uid stays burned cross-run

    def test_registry_records_run_attribution(self):
        self._issue(Path(self.dir) / "outA", "t_A", "run-1")
        reg = json.loads(Path(stage4.UID_REGISTRY).read_text(encoding="utf-8"))
        self.assertEqual(reg["30001"]["run_id"], "run-1")
        self.assertEqual(reg["30001"]["task"], "t_A")

    def test_corrupt_registry_fails_closed(self):
        Path(stage4.UID_REGISTRY).write_text("{broken", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self._issue(Path(self.dir) / "outA", "t_A")


class TestResolveAgentUid(unittest.TestCase):
    """R3-M2 (review): the control-plane uid is authoritative; an env uid
    that disagrees, uid 0, the prep uid, or anything outside the pool must be
    refused (a directly invoked root wrapper could otherwise pick its own)."""

    @classmethod
    def setUpClass(cls):
        from scripts import validate_agent_uid as v
        cls.v = v

    def test_control_uid_wins(self):
        uid, err = self.v.resolve_agent_uid("30001", "", -1, 30001, 4096)
        self.assertEqual(err, "")
        self.assertEqual(uid, 30001)

    def test_matching_env_uid_passes(self):
        self.assertEqual(self.v.resolve_agent_uid("30001", "30001", -1,
                                                  30001, 4096), (30001, ""))

    def test_env_mismatch_is_refused(self):
        uid, err = self.v.resolve_agent_uid("30001", "30002", -1, 30001, 4096)
        self.assertIsNone(uid)
        self.assertIn("!= control agent_uid", err)

    def test_uid_zero_is_refused(self):
        uid, err = self.v.resolve_agent_uid("0", "", -1, 30001, 4096)
        self.assertIsNone(uid)
        self.assertIn("root", err)

    def test_prep_uid_is_refused(self):
        uid, err = self.v.resolve_agent_uid("30001", "", 30001, 30001, 4096)
        self.assertIsNone(uid)
        self.assertIn("prep identity", err)

    def test_outside_pool_is_refused(self):
        uid, err = self.v.resolve_agent_uid("12345", "", -1, 30001, 4096)
        self.assertIsNone(uid)
        self.assertIn("outside the task pool", err)

    def test_missing_control_uid_is_refused(self):
        uid, err = self.v.resolve_agent_uid("", "", -1, 30001, 4096)
        self.assertIsNone(uid)
        self.assertIn("no agent_uid", err)


class TestSessionSweep(unittest.TestCase):
    """R3-M4 (review): proc.wait() only proves the direct child exited.
    Descendants that called setsid() escape the process GROUP but stay in the
    original SESSION, so the sweep must enumerate /proc by session id."""

    def _fake_proc(self, entries):
        root = Path(self.dir) / "proc"
        root.mkdir()
        for pid, line in entries.items():
            d = root / str(pid)
            d.mkdir()
            (d / "stat").write_text(line, encoding="utf-8")
        return str(root)

    def setUp(self):
        self.dir = scratch_dir()
        self.addCleanup(cleanup, self.dir)

    def test_finds_members_by_session_id(self):
        # fields after ')': state ppid pgrp session
        root = self._fake_proc({
            101: "101 (bash) S 1 101 5555 0 0",
            102: "102 (agent worker) S 101 101 5555 0 0",
            103: "103 (other session) S 1 103 7777 0 0",
        })
        self.assertEqual(stage4.session_members(5555, root), [101, 102])

    def test_comm_with_spaces_is_parsed(self):
        root = self._fake_proc({201: "201 (claude code) S 1 201 9001 0 0"})
        self.assertEqual(stage4.session_members(9001, root), [201])

    def test_missing_proc_root_returns_empty(self):
        self.assertEqual(stage4.session_members(1, str(Path(self.dir) / "nope")), [])


class TestTaskDirPrivacy(unittest.TestCase):
    """R3-B1: cross-task read must be closed for the PREPARED dir too."""

    def test_harden_is_posix_root_only_or_noop(self):
        # on the Windows sandbox this is a documented no-op; the real check
        # runs on the host (deploy manual section 2/4 negative tests)
        d = scratch_dir()
        self.addCleanup(cleanup, d)
        stage4.harden_task_dir(Path(d) / "tasks" / "t_1", 30001)  # no raise

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid")
                         and os.geteuid() == 0, "needs POSIX root")
    def test_harden_makes_task_dir_private(self):
        base = Path(scratch_dir())
        self.addCleanup(cleanup, base)
        tdir = base / "tasks" / "t_1"
        tdir.mkdir(parents=True)
        (tdir / "description.txt").write_text("d", encoding="utf-8")
        (tdir / "submit.sh").write_text("s", encoding="utf-8")
        stage4.harden_task_dir(tdir, 30001)
        st = os.lstat(tdir)
        self.assertEqual(st.st_uid, 30001)
        self.assertEqual(st.st_mode & 0o777, 0o700)
        self.assertEqual(os.lstat(base / "tasks").st_mode & 0o777, 0o711)


if __name__ == "__main__":
    unittest.main()
