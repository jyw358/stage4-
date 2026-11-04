#!/bin/bash
# score_all.sh: HOST-SIDE scoring for a stage-4 run (thin wrapper).
#
# Fills official_success for every row of <out_root>/results.json using the
# FINAL submission only (plugins/evidence.py), and binds the submission ledger
# written by check.py to the server's poc_records.  POST /query-poc is a
# private route: this must run as root with /root/.cybergym_env.
#
# Usage:
#   /root/score_all.sh <out_root> [<task_base>] [<ws_root>]
#     <out_root>   run root, e.g. /data/cybergym/stage4/r_engineer
#     <task_base>  prepared task dirs (default /data/cybergym/stage4/r_engineer/tasks)
#     <ws_root>    workspaces        (default /data/cybergym/stage4/r_engineer-ws)
#
# Extra flags can be passed through S4_SCORE_EXTRA, e.g.
#   S4_SCORE_EXTRA="--allow-missing-ledger" /root/score_all.sh <out>
set -u
OUT="${1:?usage: score_all.sh <out_root> [<task_base>] [<ws_root>]}"
TASKBASE="${2:-/data/cybergym/stage4/r_engineer/tasks}"
WSROOT="${3:-/data/cybergym/stage4/r_engineer-ws}"
AG="${S4_AGENT_HOME:-/home/naco/work/cybergym-agent}"
set -a; . /root/.cybergym_env; set +a

# shellcheck disable=SC2086
exec python3 "$AG/scripts/score_all.py" \
    --out "$OUT" --task-base "$TASKBASE" --ws-root "$WSROOT" \
    ${S4_SCORE_EXTRA:-}
