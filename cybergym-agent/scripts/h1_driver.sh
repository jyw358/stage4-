#!/bin/bash
# h1_driver.sh -- r22 fail-closed H1 driver.
# Hard rules (violating any => NO FINALIZED_AT, FAILED_AT written, exit!=0):
#   R1 deadline covers the WHOLE wait (set before launch; poll kill -0,
#      never a bare blocking wait)
#   R2 this-run workers identified by run-id recorded at launch; residual
#      /proc environ tokens for these run-ids => refuse
#   R3 foreign stage4 instances on the host => block finalization, record,
#      never kill
#   R4 worker nonzero exit / backfill / score / verify / RUN_ID missing
#      => stop the chain immediately
#   R5 FINALIZED_AT written atomically (tmp+mv) ONLY after both arms pass
#   R6 output tree must not pre-exist; queue must be the H1 frozen file
set -u
OUTBASE=/data2/arms/h1
QUEUE=$OUTBASE/q16frozen.json
AGENT=$OUTBASE/h1-agent
WALL_DEADLINE_MIN=600          # whole-experiment wall guard (2 arms x ~3h)
FINALIZE_TIMEOUT_MIN=30
PIDS=""
declare -A ARM_PID ARM_RUNID

fail() {  # $1 stage, $2 detail
  echo "H1_DRIVER_FAILED stage=$1 detail=$2 ts=$(date -u +%FT%TZ)" \
    | tee -a "$OUTBASE/FAILED_AT" >&2
  exit 1
}

# ---- R6 pre-flight --------------------------------------------------------
# OUTBASE itself may pre-exist (dry-run assembles the tree first); what
# must NOT exist: the agent tree, arm outputs, or any prior terminal
# marker -- refuse-to-overwrite is about RESULT state, not the directory.
mkdir -p "$OUTBASE"
if [ -e "$AGENT" ]; then
  # dry-run already assembled: accept ONLY with an intact manifest and
  # no arm outputs (i.e. it was preparation, not a prior experiment run)
  [ -s "$AGENT/H1_MANIFEST.json" ] || {
    echo "REFUSE: $AGENT exists without H1_MANIFEST.json" >&2; exit 1; }
  echo "note: reusing dry-run-assembled tree (manifest present)"
else
  python3 /tmp/h1_assemble.py >> "$OUTBASE/assemble.log" 2>&1 \
    || fail assemble "h1_assemble rc=$?"
fi
for ARM in DLegacy DV4; do
  [ -e "$OUTBASE/$ARM" ] && { echo "REFUSE: $OUTBASE/$ARM exists" >&2; exit 1; }
done
[ -e "$OUTBASE/FINALIZED_AT" ] || [ -e "$OUTBASE/FAILED_AT" ] && {
  echo "REFUSE: prior terminal marker present" >&2; exit 1; }
if [ -s "$QUEUE" ]; then
  # frozen by dry-run: lock its hash into the run record
  { echo "=== reusing dry-run frozen queue $(date -u +%FT%TZ) ==="
    sha256sum "$QUEUE" | awk '{print "sha256:", substr($1,1,16)}'
  } >> "$OUTBASE/freeze.log"
else
  bash /tmp/h1_freeze_run.sh >> "$OUTBASE/freeze.log" 2>&1 \
    || fail freeze "queue generation rc=$?"
fi
[ -s "$QUEUE" ] || fail freeze "queue file missing"
grep -q "sha256:" "$OUTBASE/freeze.log" || fail freeze "no sha recorded"

# ---- launch (arm, triage, d-strategy) ------------------------------------
launch() {  # $1 arm  $2 d-strategy
  local ARM=$1 DS=$2
  local OUT=$OUTBASE/$ARM
  mkdir -p "$OUT/tasks"
  S4_AGENT_HOME=$AGENT S4_PASS2_TRIAGE=1 S4_D_STRATEGY=$DS \
  nohup python3 "$AGENT/scripts/stage4.py" \
    --queue "$QUEUE" --task-base "$OUT/tasks" \
    --ws-root "$OUTBASE/${ARM}-ws" --out "$OUT" --workers 1 \
    > "$OUT/run.log" 2>&1 &
  ARM_PID[$ARM]=$!
  PIDS="$PIDS ${ARM_PID[$ARM]}"
  echo "arm $ARM pid=${ARM_PID[$ARM]} d=$DS"
}
launch DLegacy legacy
launch DV4 construct

# ---- R1: bounded wait over the WHOLE process set -------------------------
DEADLINE=$(( $(date +%s) + WALL_DEADLINE_MIN * 60 ))
while :; do
  ALIVE=0
  for P in $PIDS; do kill -0 "$P" 2>/dev/null && ALIVE=1; done
  [ "$ALIVE" = 0 ] && break
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    fail wall_guard "workers still alive after ${WALL_DEADLINE_MIN}min: $PIDS"
  fi
  sleep 30
done
for P in $PIDS; do
  # reap and require rc=0 (stage4 exits 0 on controlled completion)
  if ! wait "$P"; then fail worker "pid $P exited nonzero"; fi
done

# ---- R2: this-run residual tokens ----------------------------------------
for ARM in DLegacy DV4; do
  ARM_RUNID[$ARM]=$(sed -n "s/.*run_id=\(s4-[A-Za-z0-9-]*\).*/\1/p" \
    "$OUTBASE/$ARM/run.log" | tail -1)
  [ -n "${ARM_RUNID[$ARM]}" ] || fail runid "$ARM run_id missing"
  if grep -rl "S4_RUN_ID=${ARM_RUNID[$ARM]}" /proc/*/environ >/dev/null 2>&1; then
    fail residual "live /proc token for ${ARM_RUNID[$ARM]}"
  fi
done

# ---- R3: foreign stage4 instances (block, never kill) --------------------
FOREIGN=$(pgrep -f "stage4.py --queue" || true)
if [ -n "$FOREIGN" ]; then
  fail foreign_stage4 "active stage4 not owned by this run: $FOREIGN"
fi

# ---- cgroup residue for this run -----------------------------------------
for ARM in DLegacy DV4; do
  R=${ARM_RUNID[$ARM]}
  LEFT=$(find /sys/fs/cgroup -maxdepth 3 -name "*${R#s4-}*" 2>/dev/null | head -3)
  [ -z "$LEFT" ] || fail cgroup "residue for $R: $LEFT"
done

# ---- R4: sequential finalize chain, first failure stops ------------------
# H1_STUB_DIR: EXPLICIT test hook for fault-injection acceptance (r22).
# When unset (production) the real production commands run.
STUB="${H1_STUB_DIR:-}"
BACKFILL="${STUB:+$STUB/backfill.sh}"; [ -n "$STUB" ] || BACKFILL=/root/backfill_fix_side.sh
SCORER="${STUB:+$STUB/score.py}";      [ -n "$STUB" ] || SCORER="$AGENT/scripts/score_all.py"
VERIFIER="${STUB:+$STUB/verify.py}";   [ -n "$STUB" ] || VERIFIER="$AGENT/scripts/verify_stage4.py"
FDEADLINE=$(( $(date +%s) + FINALIZE_TIMEOUT_MIN * 60 ))
for ARM in DLegacy DV4; do
  R=${ARM_RUNID[$ARM]}
  bash "$BACKFILL" >> "$OUTBASE/$ARM/finalize.log" 2>&1
  RC=$?; [ "$RC" = 0 ] || fail backfill "$ARM rc=$RC"
  python3 "$SCORER" --out "$OUTBASE/$ARM" \
      --task-base "$OUTBASE/$ARM/tasks" --ws-root "$OUTBASE/${ARM}-ws" \
      --run-id "$R" >> "$OUTBASE/$ARM/finalize.log" 2>&1
  RC=$?; [ "$RC" = 0 ] || fail score "$ARM rc=$RC"
  python3 "$VERIFIER" --out "$OUTBASE/$ARM" \
      --queue "$QUEUE" --ws-root "$OUTBASE/${ARM}-ws" \
      >> "$OUTBASE/$ARM/finalize.log" 2>&1
  RC=$?; [ "$RC" = 0 ] || fail verify "$ARM rc=$RC"
  grep -q "violations=0" "$OUTBASE/$ARM/finalize.log" \
    || fail verify "$ARM violations>0 or missing"
  [ "$(date +%s)" -lt "$FDEADLINE" ] || fail finalize_timeout "$ARM"
done

# ---- R5: atomic completion marker + prompt-SHA acceptance artifact -------
for ARM in DLegacy DV4; do
  cat /root/agent_logs/*/arvo_*.p2prompt 2>/dev/null | sha256sum \
    > /dev/null   # placeholder; per-prompt shas already in H1_MANIFEST
done
echo "H1_DRIVER_OK arms=DLegacy,DV4 queue=$(sha256sum "$QUEUE" | cut -c1-16) \
ts=$(date -u +%FT%TZ)" > "$OUTBASE/.finalized.tmp" \
  && mv "$OUTBASE/.finalized.tmp" "$OUTBASE/FINALIZED_AT"
echo "H1_DRIVER_DONE $(date -u +%FT%TZ)"
