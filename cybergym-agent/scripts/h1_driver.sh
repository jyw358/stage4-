#!/bin/bash
# h1_driver.sh -- r23 fail-closed H1 driver.
# Hard rules (violating any => NO FINALIZED_AT, FAILED_AT written,
# this run's workers recovered via trusted control, exit != 0):
#   R1 wall deadline is set BEFORE launch and covers the WHOLE wait
#      (zombie-aware kill -0 polling, never a bare blocking wait);
#      budget stop trigger checked in the same loop ($85 default,
#      $90 ceiling documented with in-flight exposure in the prereg)
#   R2 this-run workers identified by run-id; residual /proc environ
#      tokens for these run-ids => refuse
#   R2.5 runtime prompt audit: every delivered .p2prompt must match the
#      manifest sha for its category+arm; triage fallbacks recorded
#      (never counted as arm application); mismatch => refuse
#   R3 foreign stage4 instances on the host => block finalization,
#      record, NEVER kill
#   R4 worker nonzero exit / backfill / score / verify / RUN_ID missing
#      => stop the chain immediately; each finalize command runs under
#      `timeout` so a HUNG command is actually killed (supervised
#      execution, not a post-hoc timestamp check)
#   R5 FINALIZED_AT written atomically (tmp+mv) ONLY after both arms pass
#   R6 output tree must not pre-exist; queue must be the H1 frozen file
# Failure containment: on ANY failure this driver terminates ONLY the
# worker process groups IT launched (TERM -> <=90s grace -> KILL,
# verify, then record cleanup=verified|inspection_incomplete in
# FAILED_AT). Foreign processes are never touched.
#
# Documented TEST hooks (production = unset):
#   H1_STUB_DIR           use $DIR/{worker.sh,backfill.sh,score.py,
#                         verify.py,audit.py} instead of real binaries
#   H1_OUTBASE            scratch output base (default /data2/arms/h1)
#   H1_WALL_DEADLINE_MIN / H1_FINALIZE_TIMEOUT_MIN /
#   H1_BUDGET_STOP_USD / H1_POLL_S   guard overrides
set -u -o pipefail
OUTBASE="${H1_OUTBASE:-/data2/arms/h1}"
QUEUE=$OUTBASE/q16frozen.json
AGENT=$OUTBASE/h1-agent
WALL_DEADLINE_MIN="${H1_WALL_DEADLINE_MIN:-600}"
FINALIZE_TIMEOUT_MIN="${H1_FINALIZE_TIMEOUT_MIN:-30}"
# r24 P0-A: budget semantics are a SOFT TARGET: stop trigger $80 leaves
# headroom for <=2 in-flight tasks (historical per-task peak $3.49, NOT a
# proven bound), so a strict $90 hard ceiling cannot be mathematically
# guaranteed -- the prereg states this explicitly.
BUDGET_STOP_USD="${H1_BUDGET_STOP_USD:-80}"
POLL_S="${H1_POLL_S:-30}"
PIDS=""
CLEANUP_STATE="cleanup=not_applicable"
declare -A ARM_PID ARM_RUNID

alive() {  # zombie-aware liveness (kill -0 alone succeeds on zombies)
  local S
  S=$(ps -o stat= -p "$1" 2>/dev/null) || return 1
  [ "${S:0:1}" != "Z" ]
}

grp_count() {  # r24 P0-B: members of process group $1 (ANY member counts,
  # zombies included -- leader death alone proves nothing about the group).
  # ps failure is detectable: mark inspection broken + report a huge count
  # (conservative: treated as "group alive", never as "group empty").
  local OUT
  OUT=$(ps -eo pgid= 2>/dev/null)
  if [ -z "$OUT" ]; then PS_OK=0; echo 999999; return; fi
  printf '%s\n' "$OUT" | awk -v g="$1" '$1==g' | wc -l
}

PS_OK=1   # flipped if the group/proc inspection itself fails
kill_this_run() {  # recover ONLY process groups this driver launched
  local ANY=0 P I
  for P in $PIDS; do
    if [ "$(grp_count "$P")" -gt 0 ]; then
      kill -TERM -- "-$P" 2>/dev/null || kill -TERM "$P" 2>/dev/null
      ANY=1
    fi
  done
  if [ "$ANY" = 1 ]; then
    for I in $(seq 1 18); do   # <=90s graceful window
      ANY=0
      for P in $PIDS; do [ "$(grp_count "$P")" -gt 0 ] && ANY=1; done
      [ "$ANY" = 0 ] && break
      sleep 5
    done
    if [ "$ANY" = 1 ]; then
      for P in $PIDS; do
        kill -KILL -- "-$P" 2>/dev/null; kill -KILL "$P" 2>/dev/null
      done
      sleep 2
    fi
  fi
  if [ "$PS_OK" != 1 ]; then
    CLEANUP_STATE="cleanup=inspection_incomplete"
  else
    ANY=0
    for P in $PIDS; do [ "$(grp_count "$P")" -gt 0 ] && ANY=1; done
    if [ "$ANY" = 0 ]; then
      CLEANUP_STATE="cleanup=verified"
    else
      CLEANUP_STATE="cleanup=residue_found"
    fi
  fi
  echo "H1_KILL_THIS_RUN $CLEANUP_STATE pids=$PIDS ts=$(date -u +%FT%TZ)"
}

fail() {  # $1 stage, $2 detail -- contains this run, then marks failure
  if [ -n "$PIDS" ]; then kill_this_run; fi
  echo "H1_DRIVER_FAILED stage=$1 detail=$2 $CLEANUP_STATE \
ts=$(date -u +%FT%TZ)" | tee -a "$OUTBASE/FAILED_AT" >&2
  exit 1
}

spent_usd() {  # r24 P0-A: fail-closed cost poller. Prints "<float>" on
  # success, "BUDGET_UNKNOWN" when any completed row is missing/invalid/
  # non-finite/negative cost_usd or its JSON is corrupt, nonzero rc when
  # the poller itself fails. NEVER silently reports $0.
  if [ -n "${H1_SPENT_CMD:-}" ]; then   # documented fault-injection hook
    eval "$H1_SPENT_CMD"; return $?
  fi
  python3 - "$OUTBASE" <<'EOF'
import glob, json, math, sys
total = 0.0
for f in glob.glob(sys.argv[1] + "/*/rows/*.json"):
    try:
        d = json.load(open(f))
    except Exception:
        print("BUDGET_UNKNOWN"); sys.exit(0)
    c = d.get("cost_usd")
    if (c is None or isinstance(c, bool)
            or not isinstance(c, (int, float))
            or not math.isfinite(c) or c < 0):
        print("BUDGET_UNKNOWN"); sys.exit(0)
    total += c
print(f"{total:.2f}")
EOF
}

# ---- R6 pre-flight --------------------------------------------------------
mkdir -p "$OUTBASE"
if [ -e "$AGENT" ]; then
  # dry-run already assembled: accept ONLY with an intact manifest and
  # no arm outputs (preparation, not a prior experiment run)
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
if [ -e "$OUTBASE/FINALIZED_AT" ] || [ -e "$OUTBASE/FAILED_AT" ]; then
  echo "REFUSE: prior terminal marker present" >&2; exit 1
fi
if [ -s "$QUEUE" ]; then
  { echo "=== reusing dry-run frozen queue $(date -u +%FT%TZ) ==="
    sha256sum "$QUEUE" | awk '{print "sha256:", substr($1,1,16)}'
  } >> "$OUTBASE/freeze.log"
else
  bash /tmp/h1_freeze_run.sh >> "$OUTBASE/freeze.log" 2>&1 \
    || fail freeze "queue generation rc=$?"
fi
[ -s "$QUEUE" ] || fail freeze "queue file missing"
grep -q "sha256:" "$OUTBASE/freeze.log" || fail freeze "no sha recorded"

# ---- launch (arm, d-strategy) --------------------------------------------
# setsid => each worker owns a process group == its pid, so recovery
# can target exactly this run's tree (kill -- -PID), never foreigners.
launch() {  # $1 arm  $2 d-strategy
  local ARM=$1 DS=$2
  local OUT=$OUTBASE/$ARM
  mkdir -p "$OUT/tasks"
  if [ -n "${H1_STUB_DIR:-}" ]; then
    setsid nohup bash "$H1_STUB_DIR/worker.sh" "$ARM" \
      > "$OUT/run.log" 2>&1 &
  else
    S4_AGENT_HOME=$AGENT S4_PASS2_TRIAGE=1 S4_D_STRATEGY=$DS \
    setsid nohup python3 "$AGENT/scripts/stage4.py" \
      --queue "$QUEUE" --task-base "$OUT/tasks" \
      --ws-root "$OUTBASE/${ARM}-ws" --out "$OUT" --workers 1 \
      > "$OUT/run.log" 2>&1 &
  fi
  ARM_PID[$ARM]=$!
  PIDS="$PIDS ${ARM_PID[$ARM]}"
  echo "arm $ARM pid=${ARM_PID[$ARM]} pgid=${ARM_PID[$ARM]} d=$DS"
}

# R1: deadline covers launch + the whole wait (set BEFORE launch)
DEADLINE=$(( $(date +%s) + WALL_DEADLINE_MIN * 60 ))
launch DLegacy legacy
launch DV4 construct

# ---- R1: bounded zombie-aware wait + fail-closed budget stop -----------
while :; do
  ALIVE=0
  for P in $PIDS; do alive "$P" && ALIVE=1; done
  [ "$ALIVE" = 0 ] && break
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    fail wall_guard "workers still alive after ${WALL_DEADLINE_MIN}min: $PIDS"
  fi
  SPENT=$(spent_usd)
  RC=$?
  [ "$RC" = 0 ] || fail budget_inspection "cost poller rc=$RC"
  [ "$SPENT" != "BUDGET_UNKNOWN" ] \
    || fail budget_inspection "completed row missing/invalid cost_usd (BUDGET_UNKNOWN)"
  if awk -v c="$SPENT" -v s="$BUDGET_STOP_USD" 'BEGIN{exit !(c>=s)}'; then
    # stop scheduling now: terminate this run's workers; in-flight
    # exposure (<=2 tasks, historical peak $3.49/task, NOT a bound) is
    # documented in H1_H2_PREREG.md -- $90 is a SOFT target
    fail budget "spent=$SPENT >= stop trigger $BUDGET_STOP_USD (soft-target semantics per prereg)"
  fi
  sleep "$POLL_S"
done
for P in $PIDS; do
  # reap and require rc=0 (stage4 exits 0 on controlled completion)
  if ! wait "$P"; then fail worker "pid $P exited nonzero"; fi
done
# r24 P0-B: leader death proves nothing -- require BOTH process groups to
# be fully empty (no surviving grandchildren) before any finalization
for P in $PIDS; do
  N=$(grp_count "$P")
  [ "$N" = 0 ] || fail residue "process group $P still has $N member(s) after worker exit"
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

# ---- R2.5: runtime unique-variable prompt audit ---------------------------
STUB="${H1_STUB_DIR:-}"
AUDIT="${STUB:+$STUB/audit.py}"
[ -n "$STUB" ] || AUDIT="$AGENT/scripts/h1_prompt_audit.py"
python3 "$AUDIT" --manifest "$AGENT/H1_MANIFEST.json" \
  --arm "DLegacy:legacy:${ARM_RUNID[DLegacy]}" \
  --arm "DV4:construct:${ARM_RUNID[DV4]}" \
  --out "$OUTBASE/H1_RUNTIME_PROMPT_AUDIT.json" \
  >> "$OUTBASE/prompt_audit.log" 2>&1 \
  || fail prompt_audit "runtime prompt audit rc=$? (see prompt_audit.log)"

# ---- R4: supervised finalize chain, first failure stops ------------------
# Each command runs under `timeout`: a HUNG command is actually
# terminated (TERM at FINALIZE_TIMEOUT_MIN, KILL +30s), rc=124 => fail.
BACKFILL="${STUB:+$STUB/backfill.sh}"; [ -n "$STUB" ] || BACKFILL=/root/backfill_fix_side.sh
SCORER="${STUB:+$STUB/score.py}";      [ -n "$STUB" ] || SCORER="$AGENT/scripts/score_all.py"
VERIFIER="${STUB:+$STUB/verify.py}";   [ -n "$STUB" ] || VERIFIER="$AGENT/scripts/verify_stage4.py"
FDEADLINE=$(( $(date +%s) + FINALIZE_TIMEOUT_MIN * 60 * 4 ))
for ARM in DLegacy DV4; do
  R=${ARM_RUNID[$ARM]}
  timeout -k 30 "${FINALIZE_TIMEOUT_MIN}m" bash "$BACKFILL" \
    >> "$OUTBASE/$ARM/finalize.log" 2>&1
  RC=$?; [ "$RC" = 0 ] || fail backfill "$ARM rc=$RC (124/137=hung-killed)"
  timeout -k 30 "${FINALIZE_TIMEOUT_MIN}m" python3 "$SCORER" --out "$OUTBASE/$ARM" \
      --task-base "$OUTBASE/$ARM/tasks" --ws-root "$OUTBASE/${ARM}-ws" \
      --run-id "$R" >> "$OUTBASE/$ARM/finalize.log" 2>&1
  RC=$?; [ "$RC" = 0 ] || fail score "$ARM rc=$RC (124/137=hung-killed)"
  timeout -k 30 "${FINALIZE_TIMEOUT_MIN}m" python3 "$VERIFIER" --out "$OUTBASE/$ARM" \
      --queue "$QUEUE" --ws-root "$OUTBASE/${ARM}-ws" \
      >> "$OUTBASE/$ARM/finalize.log" 2>&1
  RC=$?; [ "$RC" = 0 ] || fail verify "$ARM rc=$RC (124/137=hung-killed)"
  grep -q "violations=0" "$OUTBASE/$ARM/finalize.log" \
    || fail verify "$ARM violations>0 or missing"
  [ "$(date +%s)" -lt "$FDEADLINE" ] || fail finalize_timeout "$ARM"
done

# ---- R5: atomic completion marker -----------------------------------------
echo "H1_DRIVER_OK arms=DLegacy,DV4 queue=$(sha256sum "$QUEUE" | cut -c1-16) \
ts=$(date -u +%FT%TZ)" > "$OUTBASE/.finalized.tmp" \
  && mv "$OUTBASE/.finalized.tmp" "$OUTBASE/FINALIZED_AT"
echo "H1_DRIVER_DONE $(date -u +%FT%TZ)"
