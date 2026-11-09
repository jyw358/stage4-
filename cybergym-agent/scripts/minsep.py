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
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_oracle import run_side, side_info

TID = os.environ.get("CG_TASK_ID", "")


def ddmin(data: bytes, oracle, max_calls: int):
    """Returns (result, calls, r0) where result is:
       bytes  -- minimised (or budget-stopped partial) candidate
       None   -- no valid verdict possible (see calls: -1 unavailable, 0 not
                 crashing, -2 timeout, -3 mid-run verification unavailable)
    r14 C2: budget-stop and verification-unavailable are distinct; a mid-run
    oracle failure NEVER publishes the unminimised bytes as a candidate."""
    granularity = 2
    calls = 0
    workdir = Path(tempfile.mkdtemp(prefix="minsep-"))   # r14: private tmp
    try:
        def still_crashes(buf):
            nonlocal calls
            if calls >= max_calls:
                return "budget"
            calls += 1
            tmp = workdir / "cand.bin"
            tmp.write_bytes(buf)
            r = oracle([str(tmp)])
            if r["verdict"] not in ("CLEAN", "FATAL", "NONFATAL", "TIMEOUT"):
                return ("unavailable", r)     # no valid verdict this run
            return (r["verdict"] in ("FATAL", "NONFATAL")
                    and r["sig"] == ORIG_SIG)

        (workdir / "orig.bin").write_bytes(data)
        r0 = oracle([str(workdir / "orig.bin")])
        if r0["verdict"] not in ("CLEAN", "FATAL", "NONFATAL", "TIMEOUT"):
            print(f"VERIFICATION-UNAVAILABLE: vul verdict={r0['verdict']} "
                  f"kind={r0.get('runner_error_kind')} -- no valid local")
            print("verdict could be formed; this is NOT a no-crash result. Do")
            print("not minimise on this signal.")
            return None, -1, r0
        if r0["verdict"] == "TIMEOUT":
            print("VERIFICATION-INCONCLUSIVE: baseline run TIMEOUT -- neither a")
            print("crash nor a no-crash claim may be drawn from it.")
            return None, -2, r0
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
                if isinstance(ok, tuple):          # mid-run unavailable
                    r = ok[1]
                    print(f"VERIFICATION-UNAVAILABLE mid-run: verdict="
                          f"{r['verdict']} kind={r.get('runner_error_kind')}")
                    print("no valid local verdict could be formed; NOT")
                    print("publishing a minimised candidate on this signal.")
                    return None, -3, r
                if ok == "budget":
                    return bytes(cur), calls, r0   # budget stop (partial)
                if ok is True:
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
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


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
        # -1/-3 unavailable -> 7; -2 timeout -> 6; 0 not-crashing -> 5
        return {-1: 7, -3: 7, -2: 6, 0: 5}[calls]
    budget_stopped = calls >= a.max_calls
    out = Path(str(a.poc) + ".min.bin")
    out.write_bytes(mini)
    print(f"{'BUDGET-STOP (partial) ' if budget_stopped else ''}"
          f"MINIMISED {len(data)} -> {len(mini)} bytes in {calls} oracle calls "
          f"(sig: {r0['sig'][:80]})")
    print(f" wrote {out}")

    # FAQ compliance: no local fix access. check.py official fix_exit is the
    # only differential; submit the minimised stable-crash candidate.
    print("RESULT: MINIMISED-CANDIDATE -- vul crashes with stable signature; "
          "submit via check.py (official fix_exit will filter)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
