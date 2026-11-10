#!/bin/bash
# h1_failinject.sh -- r22 fault-injection acceptance for h1_driver's
# finalize chain (fail() + R4 + R5 extracted VERBATIM from the driver).
set -u
BASE=/tmp/h1_failinject
rm -rf "$BASE"; mkdir -p "$BASE/stubs"

python3 - <<'EOF'
from pathlib import Path
src = Path("/tmp/h1_driver.sh").read_text()
# head: everything BEFORE R6 (fail(), vars) -- R6/R1/R2/R3/cgroup are not
# under test and must NOT execute (they touch the real host paths)
head = src.split("# ---- R6")[0]
tail = "# ---- R4:" + src.split("# ---- R4:")[1]
head = head.replace("OUTBASE=/data2/arms/h1",
                    "OUTBASE=/tmp/h1_failinject/out")
head = head.replace("QUEUE=$OUTBASE/q16frozen.json",
                    "QUEUE=/tmp/h1_failinject/q.json")
prep = '\nARM_RUNID[DLegacy]=s4-test-aaa\nARM_RUNID[DV4]=s4-test-bbb\nmkdir -p "$OUTBASE/DLegacy" "$OUTBASE/DV4"\necho x > "$QUEUE"\n'
Path("/tmp/h1_failinject/reduced.sh").write_text(head + prep + tail)
EOF
bash -n "$BASE/reduced.sh" || { echo "SYNTAX FAIL"; exit 1; }

PASS=0; FAIL=0
run_case() {  # name b_rc s_rc v_rc violations_line expect_failed expect_finalized
  local N=$1 BR=$2 SR=$3 VR=$4 VV=$5 EF=$6 EFin=$7
  rm -rf "$BASE/out"; mkdir -p "$BASE/out/DLegacy" "$BASE/out/DV4"
  printf '#!/bin/bash\nexit %s\n' "$BR" > "$BASE/stubs/backfill.sh"
  printf '#!/usr/bin/env python3\nimport sys\nprint("scored")\nsys.exit(%s)\n' "$SR" > "$BASE/stubs/score.py"
  printf '#!/usr/bin/env python3\nimport sys\nprint("%s")\nsys.exit(%s)\n' "$VV" "$VR" > "$BASE/stubs/verify.py"
  chmod +x "$BASE/stubs"/*
  H1_STUB_DIR="$BASE/stubs" bash "$BASE/reduced.sh" >/dev/null 2>&1
  local F=N FZ=N
  [ -e "$BASE/out/FAILED_AT" ] && F=Y
  [ -e "$BASE/out/FINALIZED_AT" ] && FZ=Y
  if [ "$F" = "$EF" ] && [ "$FZ" = "$EFin" ]; then
    echo "case $N: PASS (FAILED_AT=$F FINALIZED_AT=$FZ)"; PASS=$((PASS+1))
  else
    echo "case $N: FAIL (got FAILED_AT=$F want $EF; FINALIZED_AT=$FZ want $EFin)"
    FAIL=$((FAIL+1))
  fi
}

run_case backfill_fail     1 0 0 "violations=0" Y N
run_case score_fail        0 1 0 "violations=0" Y N
run_case verify_rcfail     0 0 1 "violations=0" Y N
run_case verify_violations 0 0 0 "violations=2 warnings=1" Y N
run_case all_pass          0 0 0 "violations=0" N Y

echo "passed=$PASS failed=$FAIL"
[ "$FAIL" = 0 ] && echo "H1_FAILINJECT_PASS" || { echo "H1_FAILINJECT_FAIL"; exit 1; }
