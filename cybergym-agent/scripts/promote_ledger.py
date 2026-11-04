#!/usr/bin/env python3
"""promote_ledger.py -- copy NEW workspace-ledger entries into the protected
ledger, as ROOT (R2/B11, hardened in R3).

Trust model (R3, per dedicated-user review): the protected ledger is a
root-copied and SEALED record of UNTRUSTED submission claims.  Root writing a
line down is NOT independent witnessing of the submission -- the agent owns
the workspace ledger it is copied from.  What makes scoring safe is the
SERVER-side final record + poc-hash gate (evidence.select_final fails closed
on ledger_mismatch / hash_mismatch): a forged claim can only lose a point,
never gain one.  A trusted submission proxy (a root/agent-separate service
that performs the upload itself) remains the staged end goal; until then this
file must be described as exactly what it is.

Hardening in R3 (review findings on the old version):
  - every bad line (unparseable JSON, non-dict, missing poc_id) is a HARD
    failure (exit 2): WARN-and-skip could silently launder a tampered ledger;
  - run_id / task_id / agent_id are validated against the root-provided
    values and OVERWRITTEN on output -- setdefault() previously kept whatever
    the untrusted entry pre-wrote (exit 5 on mismatch);
  - a poc_id already protected with a DIFFERENT poc_sha256 is not silently
    ignored: it is recorded in <protected>.conflicts.jsonl and exits 4, so a
    same-id/different-hash forgery is visible at acceptance time;
  - the sealed copy itself must parse cleanly (exit 2 otherwise).

Usage (from run_agent_task.sh, as root):
  python3 promote_ledger.py --ws-ledger <ws>/submissions.jsonl \
      --protected <out>/protected_ledger/<task>.jsonl \
      --run-id R --task-id T --agent-id A

Exit codes: 0 promoted/nothing-to-do; 1 bad usage; 2 corrupt/bad-line ledger;
3 not root or protected ledger missing; 4 poc_id hash conflict (recorded);
5 identity mismatch (run_id/task_id/agent_id disagrees with the caller).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins import evidence  # noqa: E402


class LedgerCorrupt(Exception):
    """A line in a ledger is unparseable / not a dict / missing poc_id."""


def load_entries(path: Path) -> list:
    """All entries from a JSONL ledger; ANY bad line raises LedgerCorrupt
    (R3: skipping bad lines would defeat tamper detection)."""
    try:
        raw = path.read_text(encoding="utf-8", errors="strict").splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as e:
        raise LedgerCorrupt(f"{path}: unreadable: {e}")
    entries = []
    for i, line in enumerate(raw, 1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as e:
            raise LedgerCorrupt(f"{path}: line {i} unparseable: {e}")
        if not isinstance(item, dict):
            raise LedgerCorrupt(f"{path}: line {i} is not a JSON object")
        if not item.get("poc_id"):
            raise LedgerCorrupt(f"{path}: line {i} has no poc_id")
        entries.append(item)
    return entries


def protected_index(prot: Path) -> dict:
    """poc_id -> poc_sha256 for the sealed copy; duplicates with different
    hashes inside the protected ledger itself are corruption."""
    idx = {}
    for e in load_entries(prot):
        pid = str(e["poc_id"])
        sha = str(e.get("poc_sha256") or "")
        if pid in idx and idx[pid] != sha:
            raise LedgerCorrupt(
                f"{prot}: poc_id {pid} appears twice with different hashes")
        idx[pid] = sha
    return idx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws-ledger", required=True,
                    help="agent-writable workspace ledger (source)")
    ap.add_argument("--protected", required=True,
                    help="root-owned append-only ledger (destination)")
    ap.add_argument("--run-id", default="",
                    help="root-provided run binding (validated + enforced)")
    ap.add_argument("--task-id", default="",
                    help="root-provided task binding (validated + enforced)")
    ap.add_argument("--agent-id", default="",
                    help="root-provided agent binding from the control plane")
    args = ap.parse_args()

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("promote_ledger.py must run as root (it is the only writer of "
              "the protected ledger)", file=sys.stderr)
        return 3
    prot = Path(args.protected)
    if not prot.exists():
        print(f"protected ledger does not exist: {prot}", file=sys.stderr)
        return 3

    try:
        known = protected_index(prot)
        ws_entries = load_entries(Path(args.ws_ledger))
    except LedgerCorrupt as e:
        print(f"[promote] REFUSE: {e}", file=sys.stderr)
        return 2

    # ---- identity validation (R3): an entry claiming a foreign run/task/
    # agent is tampering, not metadata to preserve
    identity_errors = []
    for e in ws_entries:
        for key, val in (("run_id", args.run_id), ("task_id", args.task_id),
                         ("agent_id", args.agent_id)):
            if val and e.get(key) not in (None, "", val):
                identity_errors.append(
                    f"poc_id={e['poc_id']}: entry {key}={e.get(key)!r} != "
                    f"caller {val!r}")
    if identity_errors:
        for msg in identity_errors[:20]:
            print(f"[promote] REFUSE identity mismatch: {msg}", file=sys.stderr)
        return 5

    fresh, conflicts = [], []
    for e in ws_entries:
        pid = str(e["poc_id"])
        sha = str(e.get("poc_sha256") or "")
        if pid in known:
            if known[pid] != sha:
                conflicts.append({"poc_id": pid, "protected_sha256": known[pid],
                                  "workspace_sha256": sha, "at": evidence.utcnow()})
            continue
        known[pid] = sha
        fresh.append(e)

    if conflicts:
        cpath = prot.with_name(prot.name + ".conflicts.jsonl")
        with open(cpath, "a", encoding="utf-8") as fh:
            for c in conflicts:
                fh.write(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        for c in conflicts:
            print(f"[promote] CONFLICT: poc_id {c['poc_id']} already protected "
                  f"with a different hash ({c['protected_sha256'][:12]}... != "
                  f"{c['workspace_sha256'][:12]}...); recorded in {cpath}",
                  file=sys.stderr)
        print(f"[promote] {len(conflicts)} conflict(s); refusing to append",
              file=sys.stderr)
        return 4

    if not fresh:
        print(f"[promote] nothing new ({len(known)} entries already protected)")
        return 0

    with open(prot, "a", encoding="utf-8") as fh:
        for e in fresh:
            out = dict(e)
            # R3: the caller-provided identity ALWAYS wins; the ws entry's
            # pre-written values were validated above and are overwritten so
            # a partially-forged line cannot smuggle metadata through.
            if args.run_id:
                out["run_id"] = args.run_id
            if args.task_id:
                out["task_id"] = args.task_id
            if args.agent_id:
                out["agent_id"] = args.agent_id
            out["promoted_by"] = "root"
            out["promoted_at"] = evidence.utcnow()
            fh.write(json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    print(f"[promote] +{len(fresh)} entr{'y' if len(fresh) == 1 else 'ies'} "
          f"-> {prot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
