import importlib.util
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
    """P0-6 r10 / r14 external review: verdicts gated by execution proof
    (stdout-only sentinels, docker-rc trust gate), unknown states never
    fall through to CLEAN, text evidence alone cannot fake FATAL."""

    def test_docker_permission_lowercase_not_clean(self):
        out = ("permission denied while trying to connect to the Docker API "
               "at unix:///var/run/docker.sock\n")
        v = lo.classify_exec(False, None, out)
        self.assertEqual(v, "ORACLE_ERROR")
        self.assertEqual(lo.runner_error_kind(out), "docker_permission")

    def test_docker_permission_mixed_case(self):
        for variant in ("Permission denied while ...", "PERMISSION DENIED"):
            self.assertEqual(lo.classify_exec(False, None, variant), "ORACLE_ERROR")

    def test_daemon_unreachable(self):
        out = "Cannot connect to the Docker daemon. Is the docker daemon running?"
        self.assertEqual(lo.classify_exec(False, None, out), "ORACLE_ERROR")
        self.assertEqual(lo.runner_error_kind(out), "daemon_unreachable")

    def test_image_missing(self):
        out = "Unable to find image 'cybergym/nope:latest' locally"
        self.assertEqual(lo.classify_exec(False, None, out), "ORACLE_ERROR")
        self.assertEqual(lo.runner_error_kind(out), "image_unavailable")

    def test_foreign_language_error_still_oracle_error(self):
        # verdict must not depend on the error TEXT at all
        self.assertEqual(lo.classify_exec(False, None, "\u6743\u9650\u88ab\u62d2\u7edd"), "ORACLE_ERROR")

    def test_silent_failure_still_oracle_error(self):
        self.assertEqual(lo.classify_exec(False, None, ""), "ORACLE_ERROR")
        self.assertEqual(lo.runner_error_kind(""), "other")

    def test_runner_started_but_target_echo_lost(self):
        self.assertEqual(lo.classify_exec(True, None, S), "ORACLE_ERROR")

    def test_target_launch_error_126_127(self):
        self.assertEqual(lo.classify_exec(True, 127, out_with(127)), "TARGET_LAUNCH_ERROR")
        self.assertEqual(lo.classify_exec(True, 126, out_with(126)), "TARGET_LAUNCH_ERROR")

    def test_clean_requires_sentinels_and_zero(self):
        self.assertEqual(lo.classify_exec(True, 0, out_with(0)), "CLEAN")
        self.assertEqual(lo.classify_exec(False, None, "all fine rc=0"), "ORACLE_ERROR")

    def test_nonzero_exit_without_sanitizer_is_unknown(self):
        self.assertEqual(lo.classify_exec(True, 3, out_with(3)), "UNKNOWN")

    def test_asan_fatal(self):
        body = ("==1==ERROR: AddressSanitizer: heap-buffer-overflow\n"
                "SUMMARY: AddressSanitizer: heap-buffer-overflow\n")
        self.assertEqual(lo.classify_exec(True, 1, out_with(1, body)), "FATAL")

    def test_msan_nonfatal_and_exit77(self):
        body = "==1==WARNING: MemorySanitizer: use-of-uninitialized-value\n"
        self.assertEqual(lo.classify_exec(True, 77, out_with(77, body)), "NONFATAL")
        self.assertEqual(lo.classify_exec(True, 77, out_with(77)), "NONFATAL")

    def test_timeout_124(self):
        self.assertEqual(lo.classify_exec(True, 124, out_with(124)), "TIMEOUT")

    def test_137_without_timeout_evidence_is_unknown(self):
        # r14 A2: SIGKILL alone proves nothing about the outcome
        self.assertEqual(lo.classify_exec(True, 137, out_with(137)), "UNKNOWN")

    def test_137_with_libfuzzer_timeout_line_is_timeout(self):
        body = "ERROR: libFuzzer: timeout after 10 seconds\n"
        self.assertEqual(lo.classify_exec(True, 137, out_with(137, body)), "TIMEOUT")

    def test_libfuzzer_timeout_line_with_other_exit_not_timeout(self):
        # text alone is only candidate evidence (r14 A2)
        body = "ERROR: libFuzzer: timeout after 10 seconds\n"
        v = lo.classify_exec(True, 1, out_with(1, body))
        self.assertNotEqual(v, "TIMEOUT")

    def test_fake_fatal_text_with_exit0_is_not_fatal(self):
        # r14 A2: a target echoing ASan text and exiting 0 cannot fake a crash
        body = "==1==ERROR: AddressSanitizer: heap-buffer-overflow\n"
        self.assertEqual(lo.classify_exec(True, 0, out_with(0, body)), "CLEAN")

    def test_sanitizer_evidence_with_nonzero_exit(self):
        body = "SUMMARY: AddressSanitizer: SEGV\n"
        self.assertEqual(lo.classify_exec(True, 1, out_with(1, body)), "FATAL")

    def test_abnormal_container_rc_invalidates_parse(self):
        # r14 A1: docker rc != 0 -> the parsed exit cannot be trusted
        self.assertEqual(
            lo.classify_exec(True, 0, out_with(0), runner_rc=137),
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
        stdout = (S + "__ORACLE_TARGET_EXIT_V2__0\n"     # target echoes spoof
                  "real run\n__ORACLE_TARGET_EXIT_V2__139\n")  # real echo
        p = lo.parse_execution(stdout, 0)
        self.assertEqual(p["target_exit"], 139)          # last wins

    def test_spoof_with_abnormal_container_rc_distrusted(self):
        # container killed before the final echo: only the spoof remains,
        # runner_rc != 0 -> target_exit must be None (-> ORACLE_ERROR)
        stdout = S + "__ORACLE_TARGET_EXIT_V2__0\n"
        p = lo.parse_execution(stdout, 137)
        self.assertTrue(p["runner_started"])
        self.assertIsNone(p["target_exit"])
        self.assertTrue(p["exit_marker_seen"])

    def test_runner_sentinel_must_be_first_line(self):
        p = lo.parse_execution("target noise\n" + S, 0)
        self.assertFalse(p["runner_started"])

    def test_partial_marker_not_matched(self):
        # embedded (non-full-line) echoes must not count
        stdout = S + "x__ORACLE_TARGET_EXIT_V2__0y\n__ORACLE_TARGET_EXIT_V2__7\n"
        p = lo.parse_execution(stdout, 0)
        self.assertEqual(p["target_exit"], 7)

    def test_stderr_is_never_parsed(self):
        # parse_execution receives stdout only; make sure a stderr-style
        # combined string is not what we feed it (guard the contract)
        p = lo.parse_execution(S, None)                  # rc unknown
        self.assertTrue(p["runner_started"])
        self.assertIsNone(p["target_exit"])

    def test_fix_path_guard_realpath(self):
        # r14 F1: string + realpath component check
        self.assertFalse(lo.vul_path_ok("/data/x/fix"))
        self.assertFalse(lo.vul_path_ok("/data/x//fix//out"))
        self.assertTrue(lo.vul_path_ok("/data/x/vul"))
        self.assertTrue(lo.vul_path_ok("/data/arvo/1348/vul"))
        # a path whose REALPATH resolves under a fix tree is refused even
        # when the literal string looks like a vul dir (symlink smuggle)
        import unittest.mock as mock
        import os as _os
        with mock.patch.object(_os.path, "realpath",
                               return_value="/data/arvo/1348/fix"):
            self.assertFalse(lo.vul_path_ok("/data/alias/vul"))


if __name__ == "__main__":
    unittest.main()
