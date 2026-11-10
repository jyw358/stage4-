#!/usr/bin/env python3
"""minsep.py -- minimise a locally-crashing input (VUL side only).

Before spending an official check.py submission, shrink the candidate
(classic delta debugging on bytes, guarded to the SAME crash signature).
There is NO local fix-side access (stage3 contract, D4 strip): the official
check.py fix_exit is the only differential.

  python3 minsep.py poc.bin [--max-calls 40]

Writes <poc>.min.bin (atomic tmp+rename) only after a final full re-run of
the published bytes reproduces the crash signature; budget-stopped partial
results are explicitly labelled in the output and re-verified the same way.
Oracle = local vulnerable build (docker, ~2-10 s/call).
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
    ap = argparse.ArgumentParser(
        description="ddmin minimisation of a crashing input (vul side only; "
                    "official check.py is the only differential)")
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

    # r14-review C2: final full re-verification of the EXACT bytes we are
    # about to publish; publish atomically (tmp + os.replace) only on a
    # reproduced signature.
    workdir = Path(tempfile.mkdtemp(prefix="minsep-pub-"))
    try:
        cand = workdir / "publish.bin"
        cand.write_bytes(mini)
        rf = run_side(vi, [str(cand)], tag="minsep_reverify")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    reverified = (rf.get("verdict") in ("FATAL", "NONFATAL")
                  and rf.get("sig") == r0["sig"])

    out = Path(str(a.poc) + ".min.bin")
    if reverified:
        tmp_out = out.with_name(out.name + ".tmp")
        tmp_out.write_bytes(mini)
        os.replace(tmp_out, out)             # atomic publish
        print(f"{'BUDGET-STOP (partial result) ' if budget_stopped else ''}"
              f"MINIMISED {len(data)} -> {len(mini)} bytes in {calls} oracle "
              f"calls (sig: {r0['sig'][:80]})")
        print(f" wrote {out} (final re-verify: {rf['verdict']}, same signature)")
        if budget_stopped:
            print(" NOTE: budget_stopped=true -- this is a PARTIAL result, "
                  "not a fully converged minimisation")
        print("RESULT: MINIMISED-CANDIDATE -- vul crashes with stable "
              "signature; submit via check.py (official fix_exit will filter)")
        return 1
    # re-verify failed or was unavailable: do NOT publish as a verified
    # candidate; keep the bytes under an explicit unverified name.
    uv = Path(str(a.poc) + ".min.unverified.bin")
    uv.write_bytes(mini)
    print(f"REVERIFY-FAILED: final re-run verdict={rf.get('verdict')} "
          f"kind={rf.get('runner_error_kind')} "
          f"sig_match={rf.get('sig') == r0['sig']}")
    print(f" bytes kept as {uv} -- NOT a verified candidate; do not submit "
          "on this signal")
    return 7


if __name__ == "__main__":
    sys.exit(main())
