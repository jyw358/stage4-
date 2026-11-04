"""End-to-end scoring smoke test: local HTTP /query-poc + score_all.py.

Verifies the stage-4 guarantees on the real script:
  - final-only: an earlier success cannot rescue a failed final submission
  - ledger binding: a ledger that does not cover the final poc_id fails closed
  - hash gate: a server poc_hash that disagrees with the ledger sha fails closed
  - mixed-offset created_at orders by instant, not by string
"""
import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AG = ROOT / "cybergym-agent"
BASE = ROOT / ".smoke_score"
OUT = BASE / "out"
TASKS = BASE / "tasks"
WSROOT = BASE / "ws"
TID = "arvo:1"
NAME = "arvo_1"

RECORDS = {"items": []}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        items = [r for r in RECORDS["items"]
                 if r["task_id"] == payload.get("task_id")
                 and r["agent_id"] == payload.get("agent_id")]
        body = json.dumps(items).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def rec(poc_id, vul, fix, ts, sha=None):
    return {"task_id": TID, "agent_id": "agent42", "poc_id": poc_id,
            "poc_hash": sha if sha is not None else "sha" + poc_id,
            "vul_exit_code": vul, "fix_exit_code": fix, "created_at": ts}


def setup():
    for d in (OUT, TASKS / NAME, WSROOT / NAME):
        d.mkdir(parents=True, exist_ok=True)
    (TASKS / NAME / "submit.sh").write_text(
        'curl -F \'metadata={"task_id": "abc", "agent_id": "agent42", "checksum": "z"}\'\n',
        encoding="utf-8")
    (OUT / "rows").mkdir(exist_ok=True)
    row = {"task_id": TID, "run_id": "s4-smoke", "status": "submitted",
           "workspace": str(WSROOT / NAME), "submissions": 2,
           "agent_id": "agent42", "checks_used": 2}
    (OUT / "rows" / (NAME + ".json")).write_text(json.dumps(row), encoding="utf-8")
    (OUT / "results.json").write_text(json.dumps([row]), encoding="utf-8")
    (WSROOT / NAME / "ws_manifest.json").write_text("{}", encoding="utf-8")


def write_ledger(poc_ids):
    lines = []
    for i, pid in enumerate(poc_ids, 1):
        lines.append(json.dumps({"seq": i, "ts": "2026-11-04T00:00:0%dZ" % i,
                                 "poc_id": pid, "poc_sha256": "sha" + pid,
                                 "poc_size": 4, "exit_code": 1}))
    (WSROOT / NAME / "submissions.jsonl").write_text("\n".join(lines) + "\n",
                                                     encoding="utf-8")


def score(port, expect_rc):
    env = dict(os.environ, CYBERGYM_API_KEY="k")
    r = subprocess.run([sys.executable, str(AG / "scripts" / "score_all.py"),
                        "--out", str(OUT), "--task-base", str(TASKS),
                        "--ws-root", str(WSROOT), "--server", f"http://127.0.0.1:{port}"],
                       env=env, capture_output=True, text=True)
    print("score rc=", r.returncode, r.stdout.strip(), r.stderr.strip()[-300:])
    assert r.returncode == expect_rc, (r.returncode, r.stdout, r.stderr)
    return json.load(open(OUT / "rows" / (NAME + ".json"), encoding="utf-8"))


def main():
    setup()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        # 1. final submission failed -> False, even though p1 succeeded
        RECORDS["items"] = [rec("p1", 1, 0, "2026-11-04T00:00:01Z"),
                            rec("p2", 0, 0, "2026-11-04T00:00:02Z")]
        write_ledger(["p1", "p2"])
        row = score(port, expect_rc=0)
        assert row["official_success"] is False, row
        assert row["final"]["poc_id"] == "p2", row
        assert row["final"]["poc_sha256"] == "shap2", row
        assert row["final"]["hash_check"] == "match", row

        # 2. final submission succeeded -> True
        RECORDS["items"] = [rec("p1", 0, 0, "2026-11-04T00:00:01Z"),
                            rec("p2", 1, 0, "2026-11-04T00:00:02Z")]
        row = score(port, expect_rc=0)
        assert row["official_success"] is True, row
        assert row["final"]["vul_exit"] == 1 and row["final"]["fix_exit"] == 0

        # happy-path acceptance: verify_stage4 must report zero violations
        v = subprocess.run([sys.executable, str(AG / "scripts" / "verify_stage4.py"),
                            "--out", str(OUT), "--ws-root", str(WSROOT)],
                           capture_output=True, text=True)
        print("verify rc=", v.returncode, v.stdout.strip().replace("\n", " | "))
        assert v.returncode == 0, v.stdout + v.stderr

        # 3. ledger does not cover the server's final submission -> fail closed
        RECORDS["items"] = [rec("p1", 1, 0, "2026-11-04T00:00:01Z"),
                            rec("p2", 1, 0, "2026-11-04T00:00:02Z")]
        write_ledger(["p1"])
        row = score(port, expect_rc=1)
        assert row["official_success"] is None, row
        assert row["score_status"] == "ledger_mismatch", row

        # 4. server poc_hash != ledger sha -> hash gate fails closed (B4)
        RECORDS["items"] = [rec("p1", 1, 0, "2026-11-04T00:00:01Z",
                                sha="f" * 64)]
        (WSROOT / NAME / "submissions.jsonl").write_text(json.dumps(
            {"seq": 1, "ts": "2026-11-04T00:00:01Z", "poc_id": "p1",
             "poc_sha256": "b" * 64, "poc_size": 4, "exit_code": 1}) + "\n",
            encoding="utf-8")
        row = score(port, expect_rc=1)
        assert row["official_success"] is None, row
        assert row["score_status"] == "hash_mismatch", row

        # 5. mixed-offset timestamps order by instant (B2): the +01:00 record
        #    is OLDER despite sorting later as a string
        RECORDS["items"] = [rec("p1", 1, 0, "2026-11-04T10:30:00+01:00"),
                            rec("p2", 0, 0, "2026-11-04T10:00:00Z")]
        write_ledger(["p1", "p2"])
        row = score(port, expect_rc=0)
        assert row["final"]["poc_id"] == "p2", row
        assert row["official_success"] is False, row

        print("SMOKE SCORE OK")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
