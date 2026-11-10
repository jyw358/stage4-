#!/bin/bash
# stab_run.sh <poc_file> [runs] -- reliability probe (Stage-1 S1.4).
#   Runs the candidate N times (default 3) against the LOCAL vulnerable build:
#   each pass under a different heap state -- MALLOC_PERTURB_ cycled over
#   (unset, 85, 170) as an EXPLORATION probe for stability / hang-conversion
#   behaviour. It is NOT a MemorySanitizer converter and does not touch the
#   official check budget. Prints per-run verdicts and a STABLE/FLAKY call.
#   STABLE (>=2 identical fatal signatures) is the input to submit; FLAKY --
#   keep mutating for a deterministic trigger.
set -u
[ $# -ge 1 ] || { echo "usage: stab_run.sh <poc_file> [runs]"; exit 2; }
TID="${CG_TASK_ID:?CG_TASK_ID not set}"
N="${2:-3}"
# PR-A (r9 P0-2): resolve local_oracle from THIS script's directory (the
# workspace), never from a hardcoded /home/naco developer path.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 - "$SCRIPT_DIR" "$TID" "$1" "$N" <<'PYEOF'
import collections, os, sys
sys.path.insert(0, sys.argv[1])
from local_oracle import run_side, side_info

tid, poc, n = sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 3
vi = side_info(tid, "vul")
if not vi:
    print("NO_LOCAL_BUILD: no local vul binaries; use check.py"); sys.exit(4)
PERTURB = [None, "85", "170", "1", "255"]
sigs = collections.Counter()
rows = []
verified = 0
for i in range(n):
    r = run_side(vi, [poc], perturb=PERTURB[i % len(PERTURB)], tag="stab")
    if r["verdict"] not in ("CLEAN", "FATAL", "NONFATAL", "TIMEOUT"):
        # r14 C1: any non-verdict state (ORACLE_ERROR, launch error, UNKNOWN,
        # NO_INPUT, INPUT_ERROR, ...) is unavailable, never a flaky signal
        print(f"run {i+1}/{n} -> {r['verdict']} kind={r.get('runner_error_kind')}")
        print("--- VERIFICATION-UNAVAILABLE: no valid local verdict could be")
        print("formed. This is NOT a flaky/no-crash signal. Stop probing;")
        print("treat local verification as unavailable.")
        sys.exit(7)
    verified += 1
    sigs[(r["verdict"], r["sig"])] += 1
    rows.append((i + 1, PERTURB[i % len(PERTURB)], r["verdict"], r["sig"][:70], r["rc"]))
    print(f"run {i+1}/{n} MALLOC_PERTURB_={PERTURB[i % len(PERTURB)] or 'unset':>5} "
          f"-> {r['verdict']:<8} rc={r['rc']:<4} sig={r['sig'][:70]}")
if verified == 0:
    # r14 C1: a stability call must aggregate VERIFIED runs only
    print("--- VERIFICATION-UNAVAILABLE: zero verified runs; no stability call")
    sys.exit(7)
best, cnt = (sigs.most_common(1) or ((("NONE", ""), 0)))[0]
stable = cnt >= 2 and best[0] in ("FATAL", "NONFATAL")
print(f"--- {'STABLE' if stable else 'UNSTABLE'}: {cnt}/{n} identical signature "
      f"({best[0]}) ---")
if stable:
    print("reliable trigger -- use this input for minsep.py / check.py")
else:
    print("flaky or silent: vary fields with mutate_seed.py / boundary_synth.py "
          "for a deterministic trigger")
sys.exit(0 if stable else 1)
PYEOF
