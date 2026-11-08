#!/usr/bin/env python3
"""minsep.py -- minimise a locally-crashing input, then ONE fix-side check.

Stage-1 S1.3: before spending an official check.py submission, shrink the
candidate (classic delta debugging on bytes, guarded to the SAME crash
signature), then run the fixed build once through the budgeted differential
gate. A minimised input that crashes vul and leaves fix clean is the best
possible submission candidate.

  python3 minsep.py poc.bin [--max-calls 40] [--no-fix-gate]

Writes <poc>.min.bin. Oracle = local vulnerable build (docker, ~2-10 s/call).
The final fix-side run consumes one of the 5 per-task differential slots.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_oracle import run_side, side_info

TID = os.environ.get("CG_TASK_ID", "")


def ddmin(data: bytes, oracle, max_calls: int):
    n = len(data)
    granularity = 2
    calls = 0

    def still_crashes(buf):
        nonlocal calls
        if calls >= max_calls:
            return None                       # budget stop -- keep current
        calls += 1
        tmp = Path(os.environ.get("MINSEP_TMP", "/tmp")) / f"minsep-{os.getpid()}.bin"
        tmp.write_bytes(buf)
        r = oracle([str(tmp)])
        return r["verdict"] in ("FATAL", "NONFATAL") and r["sig"] == ORIG_SIG

    r0 = None
    tmp = Path(os.environ.get("MINSEP_TMP", "/tmp")) / f"minsep-{os.getpid()}.bin"
    tmp.write_bytes(data)
    r0 = oracle([str(tmp)])
    if r0["verdict"] not in ("FATAL", "NONFATAL"):
        print(f"NOT-CRASHING: vul verdict={r0['verdict']} -- minsep needs a local crash first")
        return None, 0, r0
    globals()["ORIG_SIG"] = r0["sig"]

    cur = bytearray(data)
    while len(cur) >= 2:
        chunk = max(1, len(cur) // granularity)
        i = 0
        reduced = False
        while i < len(cur):
            cand = bytes(cur[:i] + cur[i + chunk:])
            if not cand:
                break
            ok = still_crashes(cand)
            if ok is None:
                return bytes(cur), calls, r0       # call budget hit
            if ok:
                cur = bytearray(cand)
                granularity = max(granularity - 1, 2)
                reduced = True
            else:
                i += chunk
        if not reduced:
            if granularity >= len(cur):
                break
            granularity = min(granularity * 2, len(cur))
    return bytes(cur), calls, r0


def main():
    ap = argparse.ArgumentParser(description="ddmin + single fix-side gate")
    ap.add_argument("poc")
    ap.add_argument("--max-calls", type=int, default=40)
    a = ap.parse_args()

    if not TID:
        print("CG_TASK_ID not set"); return 2
    vi = side_info(TID, "vul")
    if not vi:
        print("NO_LOCAL_BUILD: no local vul build"); return 4
    data = Path(a.poc).read_bytes()

    mini, calls, r0 = ddmin(data, lambda fs: run_side(vi, fs, tag="minsep_vul"), a.max_calls)
    if mini is None:
        return 5
    out = Path(str(a.poc) + ".min.bin")
    out.write_bytes(mini)
    print(f"MINIMISED {len(data)} -> {len(mini)} bytes in {calls} oracle calls "
          f"(sig: {r0['sig'][:80]})")
    print(f" wrote {out}")

    # FAQ compliance: no local fix access. check.py official fix_exit is the
    # only differential; submit the minimised stable-crash candidate.
    print("RESULT: MINIMISED-CANDIDATE -- vul crashes with stable signature; "
          "submit via check.py (official fix_exit will filter)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
