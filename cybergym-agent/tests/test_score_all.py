"""Regression tests for score_all credential resolution and 404 semantics.

Covers the PR-#1 review matrix (B1/B2/B3) and re-review round 2 (M1-M4):
  - env unset + valid root file            -> root key
  - env stale key + valid root file        -> root key (formal mode)
  - explicit authorized env override       -> env key
  - all sources empty/missing              -> RuntimeError (infra abort, exit 2)
  - legacy naco-writable .env              -> ignored in formal mode, dev-only
  - 404 body {"detail":"Record not found"} -> no_records
  - 404 body {"detail":"Not found"} (auth) -> propagates (query_error, exit 2)
  - 404 with empty/non-JSON body           -> propagates (fail closed)
  - REAL file parsing for _key_from_file (M3): plain/quoted/CRLF accepted;
    export-prefix/BOM/extra-keys refused (format is pinned, not general)
  - runner dual-id sequences (M4/M1): auth-404 first must propagate even if
    the second id form would return business-404; business+business re-raise
    is marked s4_business_404 and maps to no_records despite consumed body
  - integrity vs query split (M2): evidence-binding exceptions become
    integrity_error, not query_error
"""
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from plugins import cybergym_runner
from scripts import score_all


ROOT_KEY = "r" * 32
ENV_KEY = "e" * 32
LEGACY_KEY = "n" * 32


def fake_key_files(files):
    return lambda path: files.get(path)


class FakeRunner:
    def __init__(self, error):
        self.error = error

    def query_poc(self, original_task_id=None):
        raise self.error


def http_error(code, body=b""):
    return urllib.error.HTTPError(
        url="http://stub/query-poc", code=code, msg="err",
        hdrs=None, fp=io.BytesIO(body))


class TestApiKeyPrecedence(unittest.TestCase):
    def env(self, **kw):
        return mock.patch.dict(os.environ, kw, clear=True)

    def test_root_file_wins_over_stale_env_key(self):
        # B2 review row 2: env old key + valid root file -> root key
        with self.env(CYBERGYM_API_KEY=ENV_KEY), \
             mock.patch.object(score_all, "_key_from_file",
                               fake_key_files({score_all.ROOT_KEY_FILE: ROOT_KEY})):
            self.assertEqual(score_all.api_key(), ROOT_KEY)

    def test_env_key_used_when_root_file_missing(self):
        with self.env(CYBERGYM_API_KEY=ENV_KEY), \
             mock.patch.object(score_all, "_key_from_file", fake_key_files({})):
            self.assertEqual(score_all.api_key(), ENV_KEY)

    def test_root_file_alone_suffices(self):
        with self.env(), \
             mock.patch.object(score_all, "_key_from_file",
                               fake_key_files({score_all.ROOT_KEY_FILE: ROOT_KEY})):
            self.assertEqual(score_all.api_key(), ROOT_KEY)

    def test_authorized_env_override(self):
        with self.env(CYBERGYM_API_KEY=ENV_KEY, S4_KEY_ENV_OVERRIDE="1"), \
             mock.patch.object(score_all, "_key_from_file",
                               fake_key_files({score_all.ROOT_KEY_FILE: ROOT_KEY})):
            self.assertEqual(score_all.api_key(), ENV_KEY)

    def test_all_sources_empty_raises(self):
        with self.env(), \
             mock.patch.object(score_all, "_key_from_file", fake_key_files({})):
            with self.assertRaises(RuntimeError):
                score_all.api_key()

    def test_empty_env_and_empty_root_file_raises(self):
        with self.env(CYBERGYM_API_KEY=""), \
             mock.patch.object(score_all, "_key_from_file",
                               fake_key_files({score_all.ROOT_KEY_FILE: ""})):
            with self.assertRaises(RuntimeError):
                score_all.api_key()

    def test_legacy_naco_file_ignored_in_formal_mode(self):
        # B3: a file the untrusted prep account can write is not a formal
        # credential source
        with self.env(), \
             mock.patch.object(score_all, "_key_from_file",
                               fake_key_files({score_all.LEGACY_KEY_FILE: LEGACY_KEY})):
            with self.assertRaises(RuntimeError):
                score_all.api_key()

    def test_legacy_naco_file_dev_only(self):
        with self.env(S4_DEV_LEGACY_KEYFILE="1"), \
             mock.patch.object(score_all, "_key_from_file",
                               fake_key_files({score_all.LEGACY_KEY_FILE: LEGACY_KEY})):
            self.assertEqual(score_all.api_key(), LEGACY_KEY)


class TestQueryRecords404Semantics(unittest.TestCase):
    def test_business_404_is_no_records(self):
        e = http_error(404, b'{"detail": "Record not found"}')
        self.assertEqual(score_all.query_records(FakeRunner(e), "arvo:10628"), [])

    def test_auth_gate_404_propagates(self):
        # the review's own negative case: gateway 404 must NOT become
        # no_records
        e = http_error(404, b'{"detail": "Not found"}')
        with self.assertRaises(urllib.error.HTTPError):
            score_all.query_records(FakeRunner(e), "arvo:10628")

    def test_empty_body_404_propagates(self):
        e = http_error(404, b"")
        with self.assertRaises(urllib.error.HTTPError):
            score_all.query_records(FakeRunner(e), "arvo:10628")

    def test_non_json_404_body_propagates(self):
        e = http_error(404, b"<html>proxy error</html>")
        with self.assertRaises(urllib.error.HTTPError):
            score_all.query_records(FakeRunner(e), "arvo:10628")

    def test_non_404_propagates(self):
        e = http_error(500, b'{"detail": "boom"}')
        with self.assertRaises(urllib.error.HTTPError):
            score_all.query_records(FakeRunner(e), "arvo:10628")


class TestRealFileParsing(unittest.TestCase):
    """M3: pin the ACCEPTED format of the root credential file by parsing real
    files (the precedence tests mock _key_from_file and prove nothing here)."""

    def write(self, text, binary=False):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = Path(d) / "cred.env"
        if binary:
            p.write_bytes(text)
        else:
            p.write_text(text, encoding="utf-8")
        return str(p)

    def test_plain(self):
        p = self.write("CYBERGYM_API_KEY=abc.def123\n")
        self.assertEqual(score_all._key_from_file(p), "abc.def123")

    def test_quoted(self):
        p = self.write('CYBERGYM_API_KEY="abc.def123"\n')
        self.assertEqual(score_all._key_from_file(p), "abc.def123")

    def test_crlf(self):
        p = self.write("X=1\r\nCYBERGYM_API_KEY=abc\r\n")
        self.assertEqual(score_all._key_from_file(p), "abc")

    def test_export_prefix_refused(self):
        # the format is pinned: `export KEY=...` is NOT accepted here (the
        # GLM pool files use that form and are parsed by the bash-side strict
        # parser instead -- two deliberately different mechanisms)
        p = self.write("export CYBERGYM_API_KEY=abc\n")
        self.assertIsNone(score_all._key_from_file(p))

    def test_bom_refused(self):
        p = self.write("﻿CYBERGYM_API_KEY=abc\n".encode("utf-8"), binary=True)
        self.assertIsNone(score_all._key_from_file(p))

    def test_missing_key_refused(self):
        p = self.write("OTHER_KEY=x\n")
        self.assertIsNone(score_all._key_from_file(p))

    def test_empty_value(self):
        p = self.write("CYBERGYM_API_KEY=\n")
        self.assertEqual(score_all._key_from_file(p), "")


SUBMIT_SH = (
    "#!/bin/bash\ncurl -s -X POST http://stub/submit-vul "
    "-F 'metadata={\"task_id\": \"stub:1\", \"agent_id\": \"ag1\", "
    "\"checksum\": \"deadbeef\"}' -F file=@poc.bin\n"
)


def make_task_dir(subdir=None):
    d = Path(tempfile.mkdtemp())
    if subdir:
        d = d / subdir
        d.mkdir()
    (d / "submit.sh").write_text(SUBMIT_SH, encoding="utf-8")
    return d


def http_error_fresh(code, body):
    # a FRESH error per request: each read consumes that request's body only
    return urllib.error.HTTPError(
        url="http://stub/query-poc", code=code, msg="err",
        hdrs=None, fp=io.BytesIO(body))


class TestRunnerDualIdSequences(unittest.TestCase):
    """M4/M1: exercise the REAL query_poc dual-id fallback via a stubbed
    urlopen; FakeRunner (above) bypasses it entirely."""

    def setUp(self):
        self.tdir = make_task_dir()
        self.addCleanup(shutil.rmtree, self.tdir, True)
        self.runner = cybergym_runner.CyberGymRunner(
            "http://stub", self.tdir, api_key="k")

    def run_query(self, side_effects):
        with mock.patch.object(cybergym_runner.urllib.request, "urlopen",
                               side_effect=side_effects):
            return self.runner.query_poc(original_task_id="arvo:10628")

    def test_business_then_business_marks_and_maps_to_no_records(self):
        err = self.assertRaisesHttpError([
            http_error_fresh(404, b'{"detail": "Record not found"}'),
            http_error_fresh(404, b'{"detail": "Record not found"}'),
        ])
        self.assertEqual(err.code, 404)
        # the runner marked it, so the scorer classifies without a body
        self.assertTrue(getattr(err, "s4_business_404", False))
        self.assertEqual(score_all.query_records(_Raiser(err), "arvo:10628"), [])

    def test_auth_first_propagates_immediately(self):
        # M1 core case: an auth-gate 404 on the FIRST id must propagate and
        # never be masked by a business 404 on the second id
        err = self.assertRaisesHttpError([
            http_error_fresh(404, b'{"detail": "Not found"}'),
            http_error_fresh(404, b'{"detail": "Record not found"}'),
        ])
        self.assertFalse(getattr(err, "s4_business_404", False))
        with self.assertRaises(urllib.error.HTTPError):
            score_all.query_records(_Raiser(err), "arvo:10628")

    def test_business_then_success_returns_records(self):
        resp = io.BytesIO(json.dumps([{"poc_id": "p1"}]).encode())
        with mock.patch.object(
                cybergym_runner.urllib.request, "urlopen",
                side_effect=[http_error_fresh(404, b'{"detail": "Record not found"}'),
                             _FakeResponse(resp)]):
            recs = self.runner.query_poc(original_task_id="arvo:10628")
        self.assertEqual(recs[0]["poc_id"], "p1")

    def assertRaisesHttpError(self, side_effects):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.run_query(side_effects)
        return ctx.exception


class _FakeResponse:
    def __init__(self, fp):
        self._fp = fp

    def read(self):
        return self._fp.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Raiser:
    """Minimal runner stand-in that re-raises a prepared error (body already
    consumed by the real runner)."""

    def __init__(self, err):
        self.err = err

    def query_poc(self, original_task_id=None):
        raise self.err


class TestIntegrityVsQuerySplit(unittest.TestCase):
    """M2: evidence-binding exceptions must surface as integrity_error
    (exit 1), not query_error (exit 2)."""

    def test_binding_failure_is_integrity_error(self):
        from plugins import results_store

        out = Path(tempfile.mkdtemp())
        tdir = make_task_dir("stub_1")          # main() looks up <task-base>/stub_1
        ws = Path(tempfile.mkdtemp())
        self.addCleanup(ws.rmdir)
        results_store.write_row(out, "stub:1", {
            "task_id": "stub:1", "run_id": "R1", "status": "submitted",
            "workspace": str(ws)})

        class _StubRunner:
            def __init__(self, *a, **k):
                pass

            def query_poc(self, original_task_id=None):
                return []

        argv = ["score_all.py", "--out", str(out), "--task-base", str(tdir.parent),
                "--ws-root", str(ws), "--run-id", "R1"]
        env = {"S4_KEY_ENV_OVERRIDE": "1", "CYBERGYM_API_KEY": "k"}
        with mock.patch.object(score_all, "CyberGymRunner", _StubRunner), \
             mock.patch.object(score_all.evidence, "bind_evidence",
                               side_effect=RuntimeError("boom")), \
             mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("sys.argv", argv):
            rc = score_all.main()
        row = results_store.read_row(out, "stub:1")
        self.assertEqual(row.get("score_status"), "integrity_error")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
