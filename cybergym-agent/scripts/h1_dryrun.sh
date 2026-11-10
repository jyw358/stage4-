#!/bin/bash
# h1_dryrun.sh -- r23 acceptance: assemble + freeze + unique-variable
# proof + fault-injection, WITHOUT launching any real worker.
# Every step's exit code gates the final PASS marker (pipefail on).
# Test hook: H1_DRYRUN_SELFTEST_FAIL=<assemble|freeze|fresh|failinject>
# forces that step to fail (used by h1_failinject v2 to prove gating).
set -u -o pipefail
OUTBASE="${H1_OUTBASE:-/data2/arms/h1}"
mkdir -p "$OUTBASE"
die() { echo "DRYRUN_FAIL step=$1"; exit 1; }

if [ "${H1_DRYRUN_SELFTEST_FAIL:-}" = "assemble" ]; then
  echo "SELFTEST_INJECTED_FAIL assemble"; die assemble
fi
python3 /tmp/h1_assemble.py 2>&1 | tee "$OUTBASE/dryrun_assemble.log" \
  || die assemble
[ -d "$OUTBASE/h1-agent" ] || die assemble_tree

if [ "${H1_DRYRUN_SELFTEST_FAIL:-}" = "freeze" ]; then
  echo "SELFTEST_INJECTED_FAIL freeze"; die freeze
fi
bash /tmp/h1_freeze_run.sh 2>&1 | tee "$OUTBASE/dryrun_freeze.log" \
  || die freeze
Q="$OUTBASE/q16frozen.json"
[ -s "$Q" ] || die freeze_queue

if [ "${H1_DRYRUN_SELFTEST_FAIL:-}" = "fresh" ]; then
  echo "SELFTEST_INJECTED_FAIL fresh"; die fresh
fi
python3 - <<'EOF'
import json
q = json.load(open("/data2/arms/h1/q16frozen.json"))
q2 = json.load(open("/data2/arms/h1/q16frozen_round2.json"))
assert len(q) == 16 and len(set(q)) == 16, len(q)
assert not (set(q) & set(q2)), "round1/round2 overlap"
used = set()
import glob
for f in (glob.glob("/data2/runs/formal50/rows/*.json")
          + glob.glob("/data2/arms/pair/*run*/rows/*.json")
          + glob.glob("/data2/arms/p2/[ABC]/rows/*.json")
          + glob.glob("/data2/arms/ab/*/rows/*.json")):
    used.add(json.load(open(f))["task_id"])
assert not (set(q) & used), "round1 overlaps history"
assert not (set(q2) & used), "round2 overlaps history"
print("QUEUE_FRESH_OK round1=%d round2=%d excluded=%d" % (len(q), len(q2), len(used)))
EOF
RC=$?; [ "$RC" = 0 ] || die fresh_check

if [ "${H1_DRYRUN_SELFTEST_FAIL:-}" = "failinject" ]; then
  echo "SELFTEST_INJECTED_FAIL failinject"; die failinject
fi
bash /tmp/h1_failinject.sh 2>&1 | tee "$OUTBASE/dryrun_failinject.log"
grep -q H1_FAILINJECT_PASS "$OUTBASE/dryrun_failinject.log" || die failinject

bash -n /tmp/h1_driver.sh || die driver_syntax
echo "DRIVER_SYNTAX_OK"
echo "H1_DRYRUN_PASS $(date -u +%FT%TZ)"
