#!/bin/bash
# h1_dryrun.sh -- r22 acceptance: assemble + freeze + unique-variable
# proof + fault-injection, WITHOUT launching any worker (no scoring).
set -u
mkdir -p /data2/arms/h1
python3 /tmp/h1_assemble.py 2>&1 | tee /data2/arms/h1/dryrun_assemble.log
[ -d /data2/arms/h1/h1-agent ] || { echo DRYRUN_FAIL; exit 1; }
bash /tmp/h1_freeze_run.sh 2>&1 | tee /data2/arms/h1/dryrun_freeze.log
Q=/data2/arms/h1/q16frozen.json
[ -s "$Q" ] || { echo DRYRUN_FAIL; exit 1; }
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
bash /tmp/h1_failinject.sh 2>&1 | tee /data2/arms/h1/dryrun_failinject.log | tail -2
grep -q H1_FAILINJECT_PASS /data2/arms/h1/dryrun_failinject.log || { echo DRYRUN_FAIL; exit 1; }
bash -n /tmp/h1_driver.sh && echo DRIVER_SYNTAX_OK
echo "H1_DRYRUN_PASS $(date -u +%FT%TZ)"
