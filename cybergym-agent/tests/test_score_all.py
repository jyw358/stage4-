"""Regression tests for score_all credential resolution and 404 semantics.

Covers the PR-#1 review matrix (B1/B2/B3):
  - env unset + valid root file            -> root key
  - env stale key + valid root file        -> root key (formal mode)
  - explicit authorized env override       -> env key
  - all sources empty/missing              -> RuntimeError (infra abort, exit 2)
  - legacy naco-writable .env              -> ignored in formal mode, dev-only
  - 404 body {"detail":"Record not found"} -> no_records
  - 404 body {"detail":"Not found"} (auth) -> propagates (query_error, exit 2)
  - 404 with empty/non-JSON body           -> propagates (fail closed)
"""
import io
import os
import unittest
import urllib.error
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
