"""Local smoke test (Windows): build a fake task dir, generate a workspace,
record a row and run the acceptance checker.  Not used in production."""
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AG = ROOT / "cybergym-agent"
SMOKE = ROOT / ".smoke"
TASK = SMOKE / "tasks" / "arvo_1"
WSROOT = SMOKE / "ws"
OUT = SMOKE / "out"
RUN_ID = "s4-smoke"


def build():
    for d in (TASK, WSROOT, OUT):
        d.mkdir(parents=True, exist_ok=True)
    (TASK / "description.txt").write_text("heap overflow in foo.c\n", encoding="utf-8")
    (TASK / "submit.sh").write_text(
        'curl -X POST http://x/submit-vul '
        '-F \'metadata={"task_id": "abc", "agent_id": "agent42", "checksum": "z"}\'\n',
        encoding="utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"int main(void){return 0;}\n"
        info = tarfile.TarInfo("repo/foo.c")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    (TASK / "repo-vul.tar.gz").write_bytes(buf.getvalue())


def main():
    build()
    env = dict(os.environ, S4_RUN_ID=RUN_ID)
    r = subprocess.run([sys.executable, str(AG / "scripts" / "make_agent_ws.py"),
                        "--task-dir", str(TASK), "--task-id", "arvo:1",
                        "--ws-root", str(WSROOT), "--fresh",
                        "--max-turns", "45", "--checks", "25"],
                       env=env, capture_output=True, text=True)
    print("make_agent_ws rc=", r.returncode, r.stdout.strip(), r.stderr.strip()[-300:])
    ws = WSROOT / "arvo_1"
    print("ws files:", sorted(p.name for p in ws.iterdir()))
    claude = (ws / "CLAUDE.md").read_text(encoding="utf-8")
    assert "45" in claude and "25 check.py" in claude
    manifest = json.load(open(ws / "ws_manifest.json", encoding="utf-8"))
    assert manifest["run_id"] == RUN_ID and manifest["reference_intel"] == []
    assert not (ws / "reference-intel.md").exists()

    (ws / ".checks").write_text("3", encoding="utf-8")
    with open(ws / "submissions.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 1, "ts": "2026-11-04T00:00:00Z", "poc_id": "p1",
                             "poc_sha256": "deadbeef", "poc_size": 4, "exit_code": 1}) + "\n")
    j = {"result": "VERDICT: CRASH", "total_cost_usd": 1.25, "num_turns": 7,
         "usage": {"input_tokens": 100, "output_tokens": 20}}
    jf = SMOKE / "session.json"
    jf.write_text(json.dumps(j), encoding="utf-8")
    r = subprocess.run([sys.executable, str(AG / "scripts" / "record_task_row.py"),
                        "--out", str(OUT), "--task-id", "arvo:1", "--run-id", RUN_ID,
                        "--ws", str(ws), "--stage", "pass1", "--json", str(jf),
                        "--agent-id", "agent42"],
                       capture_output=True, text=True)
    print("record rc=", r.returncode, r.stdout.strip(), r.stderr.strip()[-300:])
    row = json.load(open(OUT / "rows" / "arvo_1.json", encoding="utf-8"))
    print("row:", {k: row.get(k) for k in ("status", "submissions", "turns", "cost_usd",
                                          "agent_id", "run_id")})
    assert row["status"] == "submitted" and row["submissions"] == 1

    r = subprocess.run([sys.executable, str(AG / "scripts" / "verify_stage4.py"),
                        "--out", str(OUT), "--ws-root", str(WSROOT)],
                       capture_output=True, text=True)
    print("verify rc=", r.returncode)
    print(r.stdout.strip())
    # R2/B9+B10: a submitted-but-unscored row (and no protected ledger) must
    # FAIL acceptance -- the round is not complete until scoring + promotion
    assert r.returncode == 1, r.stdout + r.stderr


if __name__ == "__main__":
    main()
