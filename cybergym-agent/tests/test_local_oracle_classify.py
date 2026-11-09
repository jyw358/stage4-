import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ORACLE = Path(__file__).resolve().parents[1] / "scripts" / "local_oracle.py"


def load_oracle():
    spec = importlib.util.spec_from_file_location("local_oracle_u", ORACLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lo = load_oracle()

S = lo.RUNNER_SENTINEL + "\n"


def out_with(target_exit, body=""):
    return S + body + f"\n__ORACLE_TARGET_EXIT_V2__{target_exit}\n"


class TestStateFirstClassification(unittest.TestCase):
    """P0-6 r10 / r14 / r14-review: verdicts gated by execution proof
    (stdout-only sentinels, docker-rc trust gate), unknown states never
    fall through to CLEAN, text evidence alone cannot fake FATAL/NONFATAL."""

    def test_docker_permission_lowercase_not_clean(self):
        out = ("permission denied while trying to connect to the Docker API "
               "at unix:///var/run/docker.sock\n")
        v = lo.classify_exec(False, None, out, 1)
        self.assertEqual(v, "ORACLE_ERROR")
        self.assertEqual(lo.runner_error_kind(out), "docker_permission")

    def test_docker_permission_mixed_case(self):
        for variant in ("Permission denied while ...", "PERMISSION DENIED"):
            self.assertEqual(
                lo.classify_exec(False, None, variant, 1), "ORACLE_ERROR")

    def test_daemon_unreachable(self):
        out = "Cannot connect to the Docker daemon. Is the docker daemon running?"
        self.assertEqual(lo.classify_exec(False, None, out, 1), "ORACLE_ERROR")
        self.assertEqual(lo.runner_error_kind(out), "daemon_unreachable")

    def test_image_missing(self):
        out = "Unable to find image 'cybergym/nope:latest' locally"
        self.assertEqual(lo.classify_exec(False, None, out, 125), "ORACLE_ERROR")
        self.assertEqual(lo.runner_error_kind(out), "image_unavailable")

    def test_foreign_language_error_still_oracle_error(self):
        self.assertEqual(
            lo.classify_exec(False, None, "\u6743\u9650\u88ab\u62d2\u7edd", 1),
            "ORACLE_ERROR")

    def test_silent_failure_still_oracle_error(self):
        self.assertEqual(lo.classify_exec(False, None, "", 1), "ORACLE_ERROR")
        self.assertEqual(lo.runner_error_kind(""), "other")

    def test_runner_started_but_target_echo_lost(self):
        self.assertEqual(lo.classify_exec(True, None, S, 0), "ORACLE_ERROR")

    def test_target_launch_error_126_127(self):
        self.assertEqual(
            lo.classify_exec(True, 127, out_with(127), 0),
            "TARGET_LAUNCH_ERROR")
        self.assertEqual(
            lo.classify_exec(True, 126, out_with(126), 0),
            "TARGET_LAUNCH_ERROR")

    def test_clean_requires_sentinels_and_zero(self):
        self.assertEqual(lo.classify_exec(True, 0, out_with(0), 0), "CLEAN")
        self.assertEqual(
            lo.classify_exec(False, None, "all fine rc=0", 0), "ORACLE_ERROR")

    def test_nonzero_exit_without_sanitizer_is_unknown(self):
        self.assertEqual(lo.classify_exec(True, 3, out_with(3), 0), "UNKNOWN")

    def test_asan_fatal(self):
        body = ("==1==ERROR: AddressSanitizer: heap-buffer-overflow\n"
                "SUMMARY: AddressSanitizer: heap-buffer-overflow\n")
        self.assertEqual(
            lo.classify_exec(True, 1, out_with(1, body), 0), "FATAL")

    def test_msan_exit77_semantics(self):
        body = "==1==WARNING: MemorySanitizer: use-of-uninitialized-value\n"
        self.assertEqual(
            lo.classify_exec(True, 77, out_with(77, body), 0), "NONFATAL")
        # 77 without any sanitizer text keeps the benchmark semantics
        self.assertEqual(
            lo.classify_exec(True, 77, out_with(77), 0), "NONFATAL")

    def test_msan_text_with_exit0_is_clean_not_nonfatal(self):
        # r14-review A2 regression: sanitizer TEXT alone (target exited 0)
        # must not produce NONFATAL -- text is not crash evidence
        body = "==1==WARNING: MemorySanitizer: use-of-uninitialized-value\n"
        v = lo.classify_exec(True, 0, out_with(0, body), 0)
        self.assertEqual(v, "CLEAN")

    def test_msan_text_with_nonzero_exit_is_nonfatal(self):
        # real MSan-style report with a non-zero exit keeps its meaning
        body = "==1==WARNING: MemorySanitizer: use-of-uninitialized-value\n"
        self.assertEqual(
            lo.classify_exec(True, 1, out_with(1, body), 0), "NONFATAL")

    def test_timeout_124(self):
        self.assertEqual(
            lo.classify_exec(True, 124, out_with(124), 0), "TIMEOUT")

    def test_137_without_timeout_evidence_is_unknown(self):
        self.assertEqual(
            lo.classify_exec(True, 137, out_with(137), 0), "UNKNOWN")

    def test_137_with_libfuzzer_timeout_line_is_timeout(self):
        body = "ERROR: libFuzzer: timeout after 10 seconds\n"
        self.assertEqual(
            lo.classify_exec(True, 137, out_with(137, body), 0), "TIMEOUT")

    def test_libfuzzer_timeout_line_with_other_exit_not_timeout(self):
        body = "ERROR: libFuzzer: timeout after 10 seconds\n"
        v = lo.classify_exec(True, 1, out_with(1, body), 0)
        self.assertNotEqual(v, "TIMEOUT")

    def test_fake_fatal_text_with_exit0_is_not_fatal(self):
        body = "==1==ERROR: AddressSanitizer: heap-buffer-overflow\n"
        self.assertEqual(
            lo.classify_exec(True, 0, out_with(0, body), 0), "CLEAN")

    def test_sanitizer_evidence_with_nonzero_exit(self):
        body = "SUMMARY: AddressSanitizer: SEGV\n"
        self.assertEqual(
            lo.classify_exec(True, 1, out_with(1, body), 0), "FATAL")

    def test_abnormal_container_rc_invalidates_parse(self):
        self.assertEqual(
            lo.classify_exec(True, 0, out_with(0), runner_rc=137),
            "ORACLE_ERROR")

    def test_unknown_runner_rc_never_classifies(self):
        # r14-review A1: runner_rc=None (unproven completion) must refuse
        # classification even with a perfectly formed exit marker
        self.assertEqual(
            lo.classify_exec(True, 0, out_with(0), runner_rc=None),
            "ORACLE_ERROR")
        self.assertEqual(
            lo.classify_exec(True, 1, out_with(1), runner_rc=None),
            "ORACLE_ERROR")


class TestParseExecution(unittest.TestCase):
    """r14 A1: stdout-only, first-line runner proof, last full-line exit
    marker, trusted only when the docker CLI itself exited 0."""

    def test_normal_run(self):
        stdout = S + "target output\n__ORACLE_TARGET_EXIT_V2__0\n"
        p = lo.parse_execution(stdout, 0)
        self.assertTrue(p["runner_started"])
        self.assertEqual(p["target_exit"], 0)

    def test_spoof_cannot_beat_real_last_marker(self):
        stdout = (S + "__ORACLE_TARGET_EXIT_V2__0\n"
                  "real run\n__ORACLE_TARGET_EXIT_V2__139\n")
        p = lo.parse_execution(stdout, 0)
        self.assertEqual(p["target_exit"], 139)

    def test_spoof_with_abnormal_container_rc_distrusted(self):
        stdout = S + "__ORACLE_TARGET_EXIT_V2__0\n"
        p = lo.parse_execution(stdout, 137)
        self.assertTrue(p["runner_started"])
        self.assertIsNone(p["target_exit"])
        self.assertTrue(p["exit_marker_seen"])

    def test_marker_with_unknown_runner_rc_distrusted(self):
        # r14-review A1: the former rc=None best-effort branch is gone
        stdout = S + "__ORACLE_TARGET_EXIT_V2__42\n"
        p = lo.parse_execution(stdout, None)
        self.assertTrue(p["runner_started"])
        self.assertTrue(p["exit_marker_seen"])
        self.assertIsNone(p["target_exit"])

    def test_runner_sentinel_must_be_first_line(self):
        p = lo.parse_execution("target noise\n" + S, 0)
        self.assertFalse(p["runner_started"])

    def test_partial_marker_not_matched(self):
        stdout = (S + "x__ORACLE_TARGET_EXIT_V2__0y\n"
                  "__ORACLE_TARGET_EXIT_V2__7\n")
        p = lo.parse_execution(stdout, 0)
        self.assertEqual(p["target_exit"], 7)

    def test_stderr_only_never_parsed(self):
        p = lo.parse_execution(S, 0)
        self.assertTrue(p["runner_started"])
        self.assertIsNone(p["target_exit"])

    def test_fix_path_guard_realpath(self):
        self.assertFalse(lo.vul_path_ok("/data/x/fix"))
        self.assertFalse(lo.vul_path_ok("/data/x//fix//out"))
        self.assertTrue(lo.vul_path_ok("/data/x/vul"))
        self.assertTrue(lo.vul_path_ok("/data/arvo/1348/vul"))
        import unittest.mock as mock
        import os as _os
        with mock.patch.object(_os.path, "realpath",
                               return_value="/data/arvo/1348/fix"):
            self.assertFalse(lo.vul_path_ok("/data/alias/vul"))


class TestAgentIdentifierGuards(unittest.TestCase):
    """r14-review C3/C4: agent-supplied identifiers are validated against
    allowlist grammars before any shell/GDB command construction."""

    def test_focus_ok(self):
        self.assertTrue(lo.focus_ok("parse_thing"))
        self.assertTrue(lo.focus_ok("ns::Fn_like.this"))
        for bad in ("a;rm -rf", "$(x)", "`x`", "a b", "a\nb", "a'bk",
                    'a"bk', "函数", "", "a|b", "a&b", "a>b"):
            self.assertFalse(lo.focus_ok(bad), repr(bad))

    def test_dict_name_ok(self):
        self.assertTrue(lo.dict_name_ok("fuzz.dict"))
        self.assertTrue(lo.dict_name_ok("a-b_c.d"))
        for bad in ("", "..", "../x", "a b", "a;b", "a'b", 'a"b',
                    "a\nb", "$(x)", "字典"):
            self.assertFalse(lo.dict_name_ok(bad), repr(bad))

    def test_gdb_loc_ok(self):
        good = ("main", "_ZN3foo3barEv", "src/file.c:42",
                "dir/src-file.c:7", "*0x4012ab", "func:if x == 1"[:0] or
                "MyFunc", "file.c:42/2")
        for g in good:
            self.assertTrue(lo.gdb_loc_ok(g), repr(g))
        bad = ("'; rm -rf /", "$(reboot)", "`id`", "a b", "a;b", "a\nb",
               "a'b", 'a"b', "break *0", "函数:1", "", "x y:3", "a|b")
        for b in bad:
            self.assertFalse(lo.gdb_loc_ok(b), repr(b))


class TestCopyInput(unittest.TestCase):
    """r14-review B1: open-then-fstat, symlink refusal, size caps,
    never a silent partial."""

    def setUp(self):
        import shutil
        self.d = Path(tempfile.mkdtemp(prefix="ci-"))
        self.addCleanup(lambda: shutil.rmtree(self.d, ignore_errors=True))

    def test_regular_file_copies(self):
        src = self.d / "in"
        src.write_bytes(b"hello")
        dst = self.d / "out"
        ok, reason = lo._copy_input(src, dst)
        self.assertTrue(ok)
        self.assertEqual(dst.read_bytes(), b"hello")

    def test_directory_refused(self):
        sub = self.d / "adir"
        sub.mkdir()
        ok, reason = lo._copy_input(sub, self.d / "o2")
        self.assertFalse(ok)
        # Linux: open succeeds, fstat refuses (not_regular_file);
        # Windows: open(3) on a dir fails at open time -- either way the
        # non-regular input is REFUSED, never silently used.
        self.assertTrue(reason.startswith("not_regular")
                        or reason.startswith("open_failed"), reason)

    def test_size_cap_refused(self):
        src = self.d / "big"
        src.write_bytes(b"x" * 2048)
        dst = self.d / "o3"
        old = lo.MAX_INPUT_BYTES
        lo.MAX_INPUT_BYTES = 1024
        try:
            ok, reason = lo._copy_input(src, dst)
        finally:
            lo.MAX_INPUT_BYTES = old
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("too_large"))
        self.assertFalse(dst.exists())

    def test_missing_file_refused(self):
        ok, reason = lo._copy_input(self.d / "nope", self.d / "o4")
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("open_failed"))


if __name__ == "__main__":
    unittest.main()
