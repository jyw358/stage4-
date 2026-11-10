#!/bin/bash
# ab_run_v2.sh -- r21 P0-C: global-worker-wait driver for formal A/B runs.
# Fix over the naive two-nohup driver whose arm-A backfill raced with
# arm-B workers (rc=2). Order is now strictly:
#   all workers terminate (pid wait + pgrep sweep + timeout guard)
#   -> per-arm backfill -> score -> verify -> freeze report marker
set -u
OUTBASE=/data2/arms/ab
QUEUE=/data2/arms/ab/q12frozen.json
AGENT=/data2/arms/ab/ab-agent
PIDS=""

launch() {  # $1 = arm, $2 = triage 0/1
  local ARM=$1 TRI=$2
  local OUT=$OUTBASE/$ARM
  mkdir -p "$OUT/tasks"
  S4_AGENT_HOME=$AGENT S4_PASS2_TRIAGE=$TRI \
  nohup python3 "$AGENT/scripts/stage4.py" \
    --queue "$QUEUE" --task-base "$OUT/tasks" \
    --ws-root "$OUTBASE/${ARM}-ws" --out "$OUT" --workers 1 \
    > "$OUT/run.log" 2>&1 &
  PIDS="$PIDS $!"
  echo "arm $ARM pid=$!"
}

launch A 0
launch B 1

# 1) wait for OUR pids (covers normal + nonzero exit)
for P in $PIDS; do wait "$P"; echo "pid $P exited rc=$?"; done

# 2) sweep: any stage4 worker still alive on this host (crash-orphan,
#    other-terminal instances)? bounded 30-min guard, then hard stop.
DEADLINE=$(( $(date +%s) + 1800 ))
while :; do
  LEFT=$(pgrep -fc "stage4.py --queue" || true)
  [ "${LEFT:-0}" -le 0 ] && break
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "DRIVER_TIMEOUT: stage4 workers still alive after 30min; refusing to score" >&2
    exit 3
  fi
  sleep 30
done

# 3) frozen: sequential per-arm finalize (backfill is global-refuse safe
#    because no worker is alive anywhere on the host)
for ARM in A B; do
  RUN_ID=$(sed -n "s/.*run_id=\(s4-[A-Za-z0-9-]*\).*/\1/p" \
    "$OUTBASE/$ARM/run.log" | tail -1)
  bash /root/backfill_fix_side.sh \
    >> "$OUTBASE/$ARM/finalize.log" 2>&1 || echo "backfill rc=$? ($ARM)" \
    >> "$OUTBASE/$ARM/finalize.log"
  python3 "$AGENT/scripts/score_all.py" --out "$OUTBASE/$ARM" \
    --task-base "$OUTBASE/$ARM/tasks" --ws-root "$OUTBASE/${ARM}-ws" \
    --run-id "$RUN_ID" >> "$OUTBASE/$ARM/finalize.log" 2>&1
  python3 "$AGENT/scripts/verify_stage4.py" --out "$OUTBASE/$ARM" \
    --queue "$QUEUE" --ws-root "$OUTBASE/${ARM}-ws" \
    >> "$OUTBASE/$ARM/finalize.log" 2>&1 || \
    echo "VERIFY FAILED ($ARM)" >> "$OUTBASE/$ARM/finalize.log"
done
date -u +%FT%TZ > "$OUTBASE/FINALIZED_AT"
echo "AB_DRIVER_V2_DONE $(cat "$OUTBASE/FINALIZED_AT")"
