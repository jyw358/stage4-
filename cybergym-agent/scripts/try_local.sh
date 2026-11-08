#!/bin/bash
# try_local.sh <poc_file> [more files...] -- FREE local vul-side crash oracle.
#   Runs candidates ONCE against the prebuilt VULNERABLE binary locally (same
#   mount scheme as the scoring image). NO fix-side access (benchmark FAQ:
#   the agent must not touch the -fix image; the submission server is the
#   only differential). check.py official fix_exit is the sole filter.
#   ~5-15s per call, unlimited, does NOT touch the 25-check budget.
# Exit: 1 CRASH (sanitizer evidence) / 0 CLEAN / 6 TIMEOUT / 4 no build
set -u
[ $# -ge 1 ] || { echo "usage: try_local.sh <poc_file> [more files...]"; exit 2; }
TID="${CG_TASK_ID:?CG_TASK_ID not set}"
# PR-A (r9 P0-2): resolve local_oracle from THIS script's directory (the
# workspace), never from a hardcoded /home/naco developer path.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 - "$SCRIPT_DIR" "$TID" "$@" <<'PYEOF'
import os, sys
sys.path.insert(0, sys.argv[1])
from local_oracle import run_side, side_info

tid, files = sys.argv[2], sys.argv[3:]
vi = side_info(tid, "vul")
if not vi:
    print("NO_LOCAL_BUILD: no prebuilt vul binaries; use check.py")
    sys.exit(4)
v = run_side(vi, files, tag="try_local_vul")
print(f"VUL: verdict={v['verdict']} rc={v['rc']} sig={v['sig'][:90]!r}")
for l in v["tail"].splitlines():
    if "ERROR:" in l or "SUMMARY:" in l or "MemorySanitizer" in l:
        print("  ", l.strip()[:160])
if v["verdict"] == "TIMEOUT":
    print("RESULT: TIMEOUT (10 s server-aligned kill)")
    sys.exit(6)
if v["verdict"] in ("FATAL", "NONFATAL"):
    print("RESULT: CRASH on the VULNERABLE build -- candidate worth check.py;")
    print("the official fix_exit from check.py is the only differential")
    sys.exit(1)
print("RESULT: NO_TRIGGER -- adjust fields (mutate_seed.py / boundary_synth.py)")
sys.exit(0)
PYEOF
