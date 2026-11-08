import importlib.util
import sys
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
    """P0-6 r10 audit: verdicts gated by execution sentinels, never by
    docker error strings; unknown states never fall through to CLEAN."""

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
        # exit 0 claimed by docker rc but WITHOUT sentinels -> not clean
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

    def test_timeout_124_137(self):
        self.assertEqual(lo.classify_exec(True, 124, out_with(124)), "TIMEOUT")
        self.assertEqual(lo.classify_exec(True, 137, out_with(137)), "TIMEOUT")

    def test_libfuzzer_timeout_line(self):
        body = "ERROR: libFuzzer: timeout after 10 seconds\n"
        self.assertEqual(lo.classify_exec(True, 1, out_with(1, body)), "TIMEOUT")

    def test_sanitizer_evidence_beats_launch_codes(self):
        # sanitizer text present with 127 would be contradictory; sanitizer
        # evidence wins over "unknown exit" only, not over launch errors
        body = "SUMMARY: AddressSanitizer: SEGV\n"
        self.assertEqual(lo.classify_exec(True, 1, out_with(1, body)), "FATAL")

    def test_sentinel_exit_regex(self):
        m = lo._TARGET_EXIT_RE.search(S + "x\n__ORACLE_TARGET_EXIT_V2__134\n")
        self.assertEqual(m.group(1), "134")

    def test_legacy_classify_shim_untouched(self):
        # the schema-1 shim still exists for old imports and is unchanged --
        # documented as NOT fail-closed (that is why new code uses classify_exec)
        self.assertEqual(lo.classify(1, "permission denied"), "CLEAN")


if __name__ == "__main__":
    unittest.main()
