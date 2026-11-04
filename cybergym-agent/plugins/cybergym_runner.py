"""CyberGymRunner: adapter between our orchestrator and the CyberGym PoC server.

Contract mapping:
  - .run("vul"|"fix", poc_path) -> POST /submit-vul or /submit-fix (keyed)
  - exit codes: 0 = clean, 300 = server-side Timeout, anything else = crash candidate
  - server "output" goes to RunOutcome.stdout (parse_signature checks stdout too)
  - metadata (task_id/agent_id/checksum) is baked into the task's submit.sh by
    gen_task; we parse and reuse it verbatim (checksum is salted, cannot forge)

Zero external deps (urllib multipart) so it runs under the server's bare venv.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

from plugins.env_manager import RunOutcome

CLEAN_CODES = {0}
TIMEOUT_CODE = 300  # server_utils.Timeout


def parse_submit_sh(task_dir: str | Path) -> dict:
    """Extract {task_id, agent_id, checksum, require_flag} from the generated submit.sh."""
    text = (Path(task_dir) / "submit.sh").read_text(encoding="utf-8")
    m = re.search(r"-F 'metadata=(\{.*?\})'", text, re.S)
    if not m:
        raise ValueError(f"no metadata form field in {Path(task_dir) / 'submit.sh'}")
    meta = json.loads(m.group(1).replace("'", '"') if "'" in m.group(1) else m.group(1))
    for k in ("task_id", "agent_id", "checksum"):
        if k not in meta:
            raise ValueError(f"metadata missing {k}")
    meta.setdefault("require_flag", False)
    return meta


def _multipart(metadata: dict, poc_path: str | Path):
    """Build the multipart body; reads the PoC ONCE and hashes those bytes.

    The returned (sha256, size) describe exactly what goes on the wire, so the
    ledger never needs to re-read the path (the file could change between the
    upload and a later read -- TOCTOU).
    """
    boundary = uuid.uuid4().hex
    fields = ("\r\n".join([
        f"--{boundary}",
        'Content-Disposition: form-data; name="metadata"',
        "",
        json.dumps(metadata),
    ]) + "\r\n").encode()
    header = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{Path(poc_path).name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    data = Path(poc_path).read_bytes()
    from plugins.evidence import sha256_bytes
    return fields + header + data + tail, boundary, sha256_bytes(data), len(data)


class CyberGymRunner:
    def __init__(self, server_url: str, task_dir: str | Path,
                 api_key: Optional[str] = None,
                 http_timeout_s: int = 150,
                 post_fn: Optional[Callable] = None):   # injectable for tests
        self.server = server_url.rstrip("/")
        self.meta = parse_submit_sh(task_dir)
        self.api_key = api_key
        self.http_timeout_s = http_timeout_s
        self._post = post_fn or self._post_urllib
        self.last_poc_id: Optional[str] = None

    # ---- low level ----
    def _post_urllib(self, url: str, data: bytes, headers: dict) -> dict:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.http_timeout_s) as resp:
            return json.loads(resp.read().decode())

    def _submit(self, mode: str, poc_path: str) -> tuple[dict, str, int]:
        body, boundary, sha_hex, size = _multipart(self.meta, poc_path)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if mode == "fix":
            if not self.api_key:
                raise RuntimeError("submit-fix requires CYBERGYM_API_KEY")
            headers["X-API-Key"] = self.api_key
        url = f"{self.server}/submit-{mode}"
        try:
            res = self._post(url, body, headers)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")[:300]
            raise RuntimeError(f"{mode} submit failed: HTTP {e.code} {body}") from None
        return res, sha_hex, size

    # ---- runner contract ----
    def run(self, binary: str, poc_path: str, timeout_s=None) -> RunOutcome:
        mode = "vul" if "vul" in str(binary) else "fix"
        t0 = time.monotonic()
        res, sha_hex, size = self._submit(mode, poc_path)
        self.last_poc_id = res.get("poc_id")
        if mode == "vul":
            # stage-4: bind the UPLOADED bytes (hashed when the body was
            # built) to the server's poc_id at the instant of submission.
            # Never re-read the path.  A ledger failure must not abort the
            # agent's submit; scoring fails closed on a missing ledger.
            try:
                from plugins.evidence import record_submission
                record_submission(self.meta, res, sha_hex, size,
                                  server=self.server)
            except Exception as e:  # pragma: no cover - defensive
                print(f"[ledger] WARN: {e}", file=sys.stderr)
        exit_code = res.get("exit_code")
        return RunOutcome(
            ok=True,
            returncode=exit_code,
            stdout=(res.get("output") or "")[-20000:],
            timed_out=exit_code == TIMEOUT_CODE,
            duration_s=time.monotonic() - t0,
        )

    # ---- official gates ----
    def verify_agent(self) -> dict:
        """POST /verify-agent-pocs: server re-runs every crashed PoC (both modes)."""
        req = urllib.request.Request(
            f"{self.server}/verify-agent-pocs",
            data=json.dumps({"agent_id": self.meta["agent_id"]}).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": self.api_key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.http_timeout_s * 6) as resp:
            return json.loads(resp.read().decode())

    def query_poc(self, original_task_id: Optional[str] = None) -> list:
        """POST /query-poc: authoritative records. NB: submit metadata carries
        the hashed task id (e.g. 7fa395d7dac0) but poc.db keys on the original
        (e.g. arvo:10400) -- pass original_task_id, else we try both forms."""
        ids = [original_task_id, self.meta["task_id"]] if original_task_id \
            else [self.meta["task_id"]]
        last_err = None
        for tid in filter(None, ids):
            req = urllib.request.Request(
                f"{self.server}/query-poc",
                data=json.dumps({"agent_id": self.meta["agent_id"],
                                 "task_id": tid}).encode(),
                headers={"Content-Type": "application/json", "X-API-Key": self.api_key},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code != 404:
                    raise
        raise last_err

    @staticmethod
    def official_success(records: list) -> bool:
        """DEPRECATED compat probe -- NOT a scoring path.

        Returns True only when the final vul-side record can be ordered
        UNAMBIGUOUSLY by strictly parsed server ``created_at`` and satisfies
        the official predicate.  Missing/unparseable timestamps, ties,
        submit-fix rows (vul NULL) and empty input all return False: this
        helper would rather under-report than guess.  Official scoring uses
        ``plugins.evidence.select_final`` (ledger- and hash-bound).
        """
        if not records:
            return False
        from plugins.evidence import as_int, official, parse_ts
        ours = [r for r in records if as_int(r.get("vul_exit_code")) is not None]
        if not ours:
            return False
        stamped = [(r, parse_ts(r.get("created_at"))) for r in ours]
        if any(ts is None for _, ts in stamped):
            return False  # order cannot be established -> fail closed
        top = max(ts for _, ts in stamped)
        finals = [r for r, ts in stamped if ts == top]
        if len(finals) != 1:
            return False  # unresolvable same-instant tie
        return official(finals[0].get("vul_exit_code"),
                        finals[0].get("fix_exit_code")) is True
