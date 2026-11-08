import subprocess
import sys
import unittest
from pathlib import Path

from tests._scratch import cleanup, scratch_dir

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "verify_gate.py"


def run_hook(cwd: Path, relax: bool = False):
    env = {"S4_HOOK_RELAX": "1"} if relax else {}
    return subprocess.run([sys.executable, str(HOOK)], capture_output=True,
                          text=True, cwd=str(cwd), env=env)


class TestVerifyGate(unittest.TestCase):
    def setUp(self):
        self.dir = scratch_dir()
        self.addCleanup(cleanup, self.dir)

    def test_blocks_stop_with_zero_checks(self):
        (self.dir / ".checks").write_text("0", encoding="utf-8")
        r = run_hook(self.dir)
        self.assertEqual(r.returncode, 2)
        self.assertIn("STOP-BLOCKED", r.stderr)

    def test_allows_stop_after_first_check(self):
        (self.dir / ".checks").write_text("1", encoding="utf-8")
        self.assertEqual(run_hook(self.dir).returncode, 0)

    def test_allows_stop_with_solution_md(self):
        (self.dir / "SOLUTION.md").write_text("STATUS: UNSOLVED\n",
                                              encoding="utf-8")
        self.assertEqual(run_hook(self.dir).returncode, 0)

    def test_relax_env_allows_stop(self):
        (self.dir / ".checks").write_text("0", encoding="utf-8")
        self.assertEqual(run_hook(self.dir, relax=True).returncode, 0)

    def test_missing_checks_file_blocks(self):
        # no .checks at all -> treat as 0 -> block (fail-closed)
        r = run_hook(self.dir)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
