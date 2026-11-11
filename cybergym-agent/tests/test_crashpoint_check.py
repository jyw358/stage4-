import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "crashpoint_check.py"
DESC = ("A use-of-uninitialized-value exists in LibRaw::phase_one_flat_field. "
        "When read_shorts() fails the buffer is left uninitialised.")


class CrashpointCheckTest(unittest.TestCase):
    def run_tool(self, crashpoint, description=DESC):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        if description is not None:
            (tmp / "description.txt").write_text(description, encoding="utf-8")
        if crashpoint is not None:
            (tmp / "crashpoint.json").write_text(json.dumps(crashpoint),
                                                 encoding="utf-8")
        r = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp,
                           capture_output=True, text=True, timeout=30)
        return r

    def test_declared_site_matches_description(self):
        r = self.run_tool({"func": "LibRaw::phase_one_flat_field"})
        self.assertEqual(r.returncode, 0)
        self.assertIn("ANCHOR: match", r.stdout)

    def test_site_outside_description_is_a_note_not_a_verdict(self):
        # arvo:35727 and arvo:7171: the function named in description.txt is
        # not a frame of the official crash stack, so a declared site that
        # differs from it is normal and must never be reported as wrong.
        r = self.run_tool({"func": "LibRaw::phase_one_load_raw_c",
                           "fault_func": "LibRaw::phase_one_flat_field"})
        self.assertEqual(r.returncode, 0)
        self.assertIn("ANCHOR: unmatched", r.stdout)
        self.assertIn("FAULT_FUNC: LibRaw::phase_one_flat_field", r.stdout)
        self.assertIn("NOTE:", r.stdout)
        self.assertIn("phase_one_flat_field", r.stdout)
        self.assertNotIn("wrong", r.stdout.lower())

    def test_matching_the_described_function_is_called_out(self):
        # arvo:7171: the agent declared exactly the described function, which
        # is where the bug starts, while the sanitizer fired elsewhere.
        r = self.run_tool({"func": "LibRaw::phase_one_flat_field",
                           "fault_func": "LibRaw::phase_one_flat_field"})
        self.assertEqual(r.returncode, 0)
        self.assertIn("ANCHOR: match", r.stdout)
        self.assertIn("where the bug originates", r.stdout)

    def test_missing_declaration_is_reported(self):
        r = self.run_tool(None)
        self.assertEqual(r.returncode, 0)
        self.assertIn("ANCHOR: missing_crashpoint", r.stdout)


if __name__ == "__main__":
    unittest.main()
