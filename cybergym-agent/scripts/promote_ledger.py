#!/usr/bin/env python3
"""promote_ledger.py -- copy NEW workspace-ledger entries into the protected
ledger, as ROOT (R2/B11).

Threat model: the agent process (naco) writes the workspace ledger, so an
agent can append arbitrary lines to it.  The protected ledger used by
scoring must therefore NOT be agent-writable: run_agent_task.sh creates it
root-owned 0644 (+chattr +a) and this promoter -- running as root between
agent passes -- is the ONLY writer.  An agent that forges workspace lines
still cannot touch the protected copy; and forged lines that reach it via
promotion are still subject to the server-side ordering + poc_hash gate at
scoring time (evidence.select_final fails closed on ledger_mismatch /
hash_mismatch), so forgery can only lose a point, never gain one.

Dedup key: poc_id (server-assigned, unique).  Entries already present in the
protected ledger are never re-appended.

Usage (from run_agent_task.sh, as root):
  python3 promote_ledger.py --ws-ledger <ws>/submissions.jsonl \
      --protected <out>/protected_ledger/<task>.jsonl \
      [--run-id R] [--task-id T]

Exit codes: 0 promoted/nothing-to-do; 1 bad usage/ledger unreadable;
3 not root or protected ledger missing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins import evidence  # noqa: E402


def load_entries(path: Path) -> list:
    """Parseable entries from a JSONL ledger (bad lines skipped, reported)."""
    entries = []
    bad = 0
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return entries
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(item, dict):
            entries.append(item)
    if bad:
        print(f"[promote] WARN: {path}: {bad} unparseable line(s) skipped",
              file=sys.stderr)
    return entries


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws-ledger", required=True,
                    help="agent-writable workspace ledger (source)")
    ap.add_argument("--protected", required=True,
                    help="root-owned append-only ledger (destination)")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--task-id", default="")
    args = ap.parse_args()

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("promote_ledger.py must run as root (it is the only writer of "
              "the protected ledger)", file=sys.stderr)
        return 3
    prot = Path(args.protected)
    if not prot.exists():
        print(f"protected ledger does not exist: {prot}", file=sys.stderr)
        return 3

    known = {e.get("poc_id") for e in load_entries(prot) if e.get("poc_id")}
    fresh = [e for e in load_entries(Path(args.ws_ledger))
             if e.get("poc_id") and e.get("poc_id") not in known]
    if not fresh:
        print(f"[promote] nothing new ({len(known)} entries already protected)")
        return 0

    with open(prot, "a", encoding="utf-8") as fh:
        for e in fresh:
            out = dict(e)
            out["promoted_by"] = "root"
            out["promoted_at"] = evidence.utcnow()
            if args.run_id:
                out.setdefault("run_id", args.run_id)
            if args.task_id:
                out.setdefault("task_id", args.task_id)
            fh.write(json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    print(f"[promote] +{len(fresh)} entr{'y' if len(fresh) == 1 else 'ies'} "
          f"-> {prot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
