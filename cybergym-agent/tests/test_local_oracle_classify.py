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

    def test_open_uses_nonblocking_flag(self):
        # r15: a FIFO must not be able to BLOCK open() before the fstat
        # type check -- the open flags must include O_NONBLOCK (POSIX).
        import unittest.mock as mock
        if not hasattr(lo.os, "O_NONBLOCK"):
            self.skipTest("O_NONBLOCK not available on this platform")
        seen = {}
        real_open = lo.os.open

        def spy(path, flags, *a, **k):
            seen["flags"] = flags
            return real_open(path, flags, *a, **k)

        src = self.d / "nb"
        src.write_bytes(b"z")
        with mock.patch.object(lo.os, "open", side_effect=spy):
            ok, _ = lo._copy_input(src, self.d / "nb_out")
        self.assertTrue(ok)
        self.assertTrue(seen["flags"] & lo.os.O_NONBLOCK)


class TestPostAbnormalCleanupThreeWay(unittest.TestCase):
    """r15 P0 regression: the tri-state inspection result must NEVER map
    an UNKNOWN (None) to cleanup_verified."""

    def _patch_gone(self, value):
        import unittest.mock as mock
        return mock.patch.object(lo, "_container_gone", return_value=value)

    def test_inspection_failure_is_incomplete(self):
        # docker ps failed / timed out / errored -> inspection_incomplete
        with self._patch_gone(None):
            cleanup, kill_rc = lo._post_abnormal_cleanup("c1")
        self.assertEqual(cleanup, "inspection_incomplete")

    def test_gone_is_verified(self):
        with self._patch_gone(True):
            cleanup, _ = lo._post_abnormal_cleanup("c2")
        self.assertEqual(cleanup, "cleanup_verified")

    def test_alive_triggers_kill_and_verify(self):
        import unittest.mock as mock
        with self._patch_gone(False), \
                mock.patch.object(lo, "_kill_and_verify",
                                  return_value=("cleanup_verified", 0)) as kv:
            cleanup, kill_rc = lo._post_abnormal_cleanup("c3")
        self.assertEqual(cleanup, "cleanup_verified")
        kv.assert_called_once_with("c3")

    def test_alive_kill_unverified_propagates(self):
        import unittest.mock as mock
        with self._patch_gone(False), \
                mock.patch.object(lo, "_kill_and_verify",
                                  return_value=("cleanup_failed", 1)):
            cleanup, _ = lo._post_abnormal_cleanup("c4")
        self.assertEqual(cleanup, "cleanup_failed")


class TestSweepLabel(unittest.TestCase):
    """r16: orphaned-container convergence sweep (age-filtered, verified)."""

    def test_age_minutes_parsing(self):
        self.assertLess(lo._age_minutes("About a minute"), 2.0)
        self.assertGreaterEqual(lo._age_minutes("About a minute"), 0.9)
        self.assertEqual(lo._age_minutes("30 seconds"), 0.5)
        self.assertEqual(lo._age_minutes("2 hours"), 120.0)
        self.assertEqual(lo._age_minutes("3 days"), 3 * 1440.0)
        self.assertEqual(lo._age_minutes(""), 0.0)
        self.assertEqual(lo._age_minutes("weird text"), 999.0)

    def _ps_result(self, stdout=""):
        import types
        r = types.SimpleNamespace(returncode=0, stdout=stdout)
        import unittest.mock as mock
        return mock.patch.object(
            lo.subprocess, "run",
            side_effect=lambda *a, **k: r if a and a[0][:2] == ["docker", "ps"]
            else mock.DEFAULT)

    def test_young_and_exited_skipped_old_killed(self):
        import unittest.mock as mock
        lines = ("aaa\t30 seconds\tUp 30 seconds\n"
                 "bbb\tAbout a minute\tUp About a minute\n"   # young: skip
                 "ccc\t2 hours\tUp 2 hours\n"                  # old: kill
                 "ddd\t3 hours\tExited (0) 3 hours ago\n")    # exited: skip
        with self._ps_result(lines), \
                mock.patch.object(lo, "_kill_and_verify",
                                  return_value=("cleanup_verified", 0)) as kv:
            res = lo.sweep_label("cybergym.local_oracle=1")
        self.assertEqual([r["id"] for r in res], ["ccc"])
        kv.assert_called_once_with("ccc")

    def test_inspection_failure_reported(self):
        import unittest.mock as mock
        with mock.patch.object(
                lo.subprocess, "run",
                side_effect=lo.subprocess.TimeoutExpired("docker", 1)):
            res = lo.sweep_label("x=1")
        self.assertEqual(res[0]["cleanup"], "inspection_incomplete")


class TestContainerGoneMatching(unittest.TestCase):
    """r16 battery fix: gone-check must match container ID **or** name
    (the old `--filter name=` never matched a bare ID -> false positive)."""

    def _ps(self, stdout):
        import types
        import unittest.mock as mock
        r = types.SimpleNamespace(returncode=0, stdout=stdout)
        return mock.patch.object(lo.subprocess, "run", return_value=r)

    def test_gone_by_id_and_by_name(self):
        listing = "aaa111 lo-abc\nddd222 dfz-9\n"
        # present by id
        with self._ps(listing):
            self.assertIs(lo._container_gone("aaa111", tries=1), False)
        # present by name
        with self._ps(listing):
            self.assertIs(lo._container_gone("dfz-9", tries=1), False)
        # absent (old bug: id filter never matched -> spurious True)
        with self._ps(listing):
            self.assertIs(lo._container_gone("zzz999", tries=1), True)
        with self._ps(""):
            self.assertIs(lo._container_gone("anything", tries=1), True)

    def test_inspection_error_is_none(self):
        import types
        import unittest.mock as mock
        with mock.patch.object(
                lo.subprocess, "run",
                side_effect=lo.subprocess.TimeoutExpired("docker", 1)):
            self.assertIsNone(lo._container_gone("x", tries=1))
        with mock.patch.object(
                lo.subprocess, "run",
                return_value=types.SimpleNamespace(returncode=1,
                                                   stdout="")):
            self.assertIsNone(lo._container_gone("x", tries=1))


if __name__ == "__main__":
    unittest.main()
