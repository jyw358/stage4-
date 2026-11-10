import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pass2_triage as pt  # noqa: E402

SCRATCH = Path(__file__).resolve().parents[1] / "tests" / "_scratch_p2"


def fresh_ws():
    if SCRATCH.exists():
        for p in sorted(SCRATCH.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
        for p in sorted(SCRATCH.rglob("*"), reverse=True):
            if p.is_dir():
                p.rmdir()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "ws_manifest.json").write_text(json.dumps(
        {"tools": {"required": {"repo_map.py": {}, "try_local.sh": {}},
                   "optional": {"directed_fuzz.py": "unavailable"},
                   "safety_hooks": {"verify_gate.py": {}}}}),
        encoding="utf-8")
    (SCRATCH / "repo").mkdir(exist_ok=True)
    return SCRATCH


class TestCandidateGate(unittest.TestCase):
    def test_symlink_rejected(self):
        self.assertEqual(pt.candidate_ok({"is_regular": True, "nlink": 1,
                                          "size": 10}, True), "symlink")

    def test_hardlink_rejected(self):
        self.assertEqual(pt.candidate_ok({"is_regular": True, "nlink": 2,
                                          "size": 10}, False), "hardlink")

    def test_size_bounds(self):
        self.assertEqual(pt.candidate_ok(
            {"is_regular": True, "nlink": 1, "size": 0}, False),
            "size_out_of_range")
        self.assertEqual(pt.candidate_ok(
            {"is_regular": True, "nlink": 1, "size": 3 * 1024 * 1024},
            False), "size_out_of_range")
        self.assertEqual(pt.candidate_ok(
            {"is_regular": True, "nlink": 1, "size": 128}, False), "")


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.ws = fresh_ws()

    def test_trusted_and_tier_ranking(self):
        for name, body in (("poc.bin", b"A" * 8), ("crash-1", b"B" * 8),
                           ("t9.bin", b"C" * 8), ("repo_map.py", b"x")):
            (self.ws / name).write_bytes(body)
        fz = self.ws / "fz"
        fz.mkdir(exist_ok=True)
        (fz / "t1.bin").write_bytes(b"D" * 8)
        cands, issues = pt.discover_candidates(self.ws,
                                               pt.load_trusted_tools(self.ws))
        names = [c["file"] for c in cands]
        self.assertIn("poc.bin", names)
        self.assertNotIn("repo_map.py", names)   # trusted tool excluded
        self.assertLess(names.index("poc.bin"), names.index("crash-1"))
        self.assertLess(names.index("crash-1"), names.index("fz/t1.bin"))
        self.assertEqual(issues, [])

    def test_generators_marked_possible(self):
        (self.ws / "poc_gen.py").write_text(
            "from pathlib import Path\nPath('poc.bin').write_bytes(b'x')\n",
            encoding="utf-8")
        (self.ws / "reader.py").write_text("print(open('a.txt').read())",
                                           encoding="utf-8")
        # r19 P0: unsafe filenames never become prompt-interpolated commands
        for bad in ("weird name.py", "semi;colon.py",
                    "u'nicode\U0001f600.py"):
            (self.ws / bad).write_text("open('poc.bin','wb')",
                                       encoding="utf-8")
        gens = pt.discover_generators(self.ws,
                                      pt.load_trusted_tools(self.ws))
        names = [g["file"] for g in gens]
        self.assertEqual(names, ["poc_gen.py"])
        self.assertEqual(gens[0]["confidence"], "possible_generator")
        self.assertIn("write_bytes", gens[0]["matched"])

    def test_gen_name_safe(self):
        self.assertTrue(pt.gen_name_safe("poc_gen.py"))
        self.assertTrue(pt.gen_name_safe("gen-2_v3.py"))
        for bad in ("", "a b.py", "a;b.py", "a'b.py", "a\nb.py", "../x.py",
                    "a|b.py", "a$(x).py", "脚本.py", "-lead.py",
                    "x" * 200 + ".py"):
            self.assertFalse(pt.gen_name_safe(bad), repr(bad))


class TestDecideAndRelevance(unittest.TestCase):
    def test_files_only_decision(self):
        self.assertEqual(pt.decide([{"file": "poc.bin"}], [], None)[0], "B")
        self.assertEqual(pt.decide([], [{"file": "poc_gen.py"}], None)[0],
                         "C")
        self.assertEqual(pt.decide([], [], None)[0], "D")

    def test_a1_vs_a2_by_target_frame(self):
        target = "cmsIT8_load_fuzzer"
        with_frame = ("==ERROR: AddressSanitizer\n"
                      f"    #0 0x55 in {target} /src/lcms/src/cmsit8.c:99\n"
                      "    #1 0x66 in main")
        self.assertEqual(pt.relevance_of(with_frame, target),
                         "target_frame")
        no_frame = ("==ERROR: AddressSanitizer: allocation-size-too-big\n"
                    "    #0 0x11 in malloc (/lib/x86_64/…)\n"
                    "    #1 0x22 in operator new")
        self.assertEqual(pt.relevance_of(no_frame, target),
                         "no_target_frame")
        # r12: project frame alone does NOT confirm the target bug -- any
        # stable dynamic FATAL is CRASH_TARGET_UNKNOWN -> A2, never A1
        exec_r = [{"stable_fatal": True, "relevance": "target_frame"}]
        cat, primary = pt.decide(["poc.bin"], [], exec_r)
        self.assertEqual(cat, "A2")
        self.assertEqual(primary["crash_label"], "CRASH_TARGET_UNKNOWN")
        exec_r2 = [{"stable_fatal": True, "relevance": "no_target_frame"}]
        self.assertEqual(pt.decide(["poc.bin"], [], exec_r2)[0], "A2")
        # oracle failure with candidates present stays B, never A
        exec_r3 = [{"stable_fatal": False, "verdict": "ORACLE_ERROR"}]
        self.assertEqual(pt.decide(["poc.bin"], [], exec_r3)[0], "B")


class TestPrompts(unittest.TestCase):
    def _all_prompts(self):
        a1 = pt.build_prompt("A1", {"file": "poc.bin", "sha256": "ab" * 32})
        a2 = pt.build_prompt("A2", {"file": "poc.bin", "sha256": "ab" * 32})
        b = pt.build_prompt("B", {"candidates": [{"file": "poc.bin"}]})
        c = pt.build_prompt("C", {"generators": [
            {"file": "poc_gen.py", "confidence": "possible_generator",
             "matched": ["write_bytes"]}]})
        d = pt.build_prompt("D", None)
        g = pt.build_prompt("TRIAGE_ERROR", None)
        return {"A1": a1, "A2": a2, "B": b, "C": c, "D": d,
                "GENERIC": g}

    def test_no_host_bare_run_anywhere(self):
        # r11 HOLD red line: no prompt may teach host-side bare execution
        for name, text in self._all_prompts().items():
            self.assertNotIn("LD_LIBRARY_PATH", text, name)
            self.assertNotRegex(text, r"/out/|oss-fuzz-base-runner", name)

    def test_v4_submit_early_discipline(self):
        # 1A postmortem: v3 'hypothesis-first' wording converted 0/7 into
        # check intents; v4 makes the FIRST action the submission itself
        p = self._all_prompts()
        for key in ("A2", "B", "C"):
            self.assertIn("STEP 1", p[key], key)
        self.assertIn("your FIRST action, no pre-reading", p["A2"])
        self.assertIn("check.py poc.bin", p["A2"])
        self.assertIn("BEFORE studying source", p["B"])
        self.assertIn("2 checks total", p["B"])
        self.assertIn("execute the generator NOW", p["C"])
        self.assertIn("do NOT read any source", p["C"])
        for key in ("A1", "A2", "B", "C"):
            self.assertIn("SUBMIT FIRST, RESEARCH LATER", p[key], key)
            self.assertIn("first 3 actions", p[key], key)

    def test_v4_d_strategy_arms(self):
        construct = pt.build_prompt("D", None)
        unsolved = pt.build_prompt("D", None, d_strategy="unsolved")
        self.assertIn("8 turns", construct)
        self.assertIn("UNSOLVED", construct)          # honest stop fallback
        self.assertIn("STOP immediately", unsolved)
        self.assertIn("Do not run", unsolved)
        self.assertNotIn("8 turns", unsolved)

    def test_h1_legacy_d_arm(self):
        # H1 unique variable: legacy wording rescues via explicit
        # write-produce-check, r19 validity constraints kept
        legacy = pt.build_prompt("D", None, d_strategy="legacy")
        self.assertIn("write poc_gen.py", legacy)
        self.assertIn("produce poc.bin", legacy)
        self.assertIn("python3 check.py poc.bin", legacy)
        self.assertIn("never an empty file", legacy)      # r19 constraints
        self.assertNotIn("MOMENT", legacy)                # not v4 wording
        self.assertNotIn("8 turns", legacy)               # legacy: 3 checks
        self.assertIn("at most 3", legacy)
        # H1 unique-variable proof: shared categories byte-identical
        demo = {"A1": {"file": "poc.bin", "sha256": "ab" * 32},
                "A2": {"file": "poc.bin"},
                "B": {"candidates": [{"file": "poc.bin"}]},
                "C": {"generators": [{"file": "poc_gen.py"}]},
                "GENERIC": None}
        for cat, data in demo.items():
            self.assertEqual(
                pt.build_prompt(cat, data, d_strategy="legacy"),
                pt.build_prompt(cat, data, d_strategy="construct"),
                cat)

    def test_content_requirements(self):
        p = self._all_prompts()
        self.assertIn("check.py poc.bin", p["A1"])
        self.assertIn("CRASH_TARGET_UNKNOWN", p["A2"])   # r12: no fake A1
        self.assertIn("possible generators", p["C"])
        for key, text in p.items():
            # r19 P1: precise final-only semantics in every prompt
            self.assertIn("final-only rule", text, key)
            self.assertIn("no best-of", text, key)

    def test_r19_revisions(self):
        p = self._all_prompts()
        # P1 preamble: action definition + minimum candidate validity
        for key in ("A1", "A2", "B", "C"):
            self.assertIn("substantive tool or file operations", p[key])
            self.assertIn("never an empty file", p[key])
        # P1 A2/B: second check needs a stated structural change
        self.assertIn("ONLY if you can state the specific structural "
                      "change", p["A2"])
        self.assertIn("do not\nmerely rename".replace("\n", " ")
                      if False else "merely rename, re-save", p["B"])
        # P0 C: sandbox + workspace-local-only + failure handling
        self.assertIn("session sandbox", p["C"])
        self.assertIn("never run files from outside the workspace", p["C"])
        self.assertIn("do NOT\nsubmit an unrelated".replace("\n", " ")
                      if False else "submit an unrelated file", p["C"])
        # P1 D-construct: non-empty task-format gate
        self.assertIn("concrete, non-empty,", self._d_construct())
        self.assertIn("Never submit intermediate", self._d_construct())
        # P1 GENERIC: own discipline, classification failure != validation
        g = p["GENERIC"]
        self.assertIn("NOT validation", g)
        self.assertIn("If candidate\nintegrity".replace("\n", " ")
                      if False else "cannot be confirmed", g)
        self.assertNotIn("SUBMIT FIRST, RESEARCH LATER", g)

    def _d_construct(self):
        return pt.build_prompt("D", None, d_strategy="construct")


class TestCheckDouble(unittest.TestCase):
    def setUp(self):
        fresh_ws()
        self.double = Path(__file__).resolve().parents[1] / "scripts" / \
            "check_double.py"

    def test_records_intent_and_mimics_contract(self):
        (SCRATCH / "poc.bin").write_bytes(b"x" * 16)
        r = subprocess.run([sys.executable, str(self.double), "poc.bin"],
                           cwd=SCRATCH, capture_output=True, text=True,
                           env={**os.environ, "CG_CHECKS_BUDGET": "25"})
        self.assertEqual(r.returncode, 0)
        self.assertIn("VERDICT: NO_TRIGGER", r.stdout)
        self.assertIn("vul_exit=0", r.stdout)
        self.assertEqual((SCRATCH / ".checks").read_text(), "1")
        row = json.loads(
            (SCRATCH / ".checkdouble.log").read_text().splitlines()[0])
        self.assertEqual(row["size"], 16)
        self.assertEqual(len(row["sha256"]), 64)
        self.assertEqual(row["checks_after"], 1)
        self.assertTrue(row["simulated"])   # r12: mark post-feedback as sim

    def test_budget_exhausted_path(self):
        (SCRATCH / ".checks").write_text("25", encoding="utf-8")
        r = subprocess.run([sys.executable, str(self.double)],
                           cwd=SCRATCH, capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)
        self.assertIn("BUDGET_EXHAUSTED", r.stdout)


if __name__ == "__main__":
    unittest.main()
