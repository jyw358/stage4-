#!/bin/bash
# score_all.sh: HOST-SIDE scoring for a stage-4 run (thin wrapper).
#
# Fills official_success for every row of <out_root>/results.json using the
# FINAL submission only (plugins/evidence.py), binds the submission ledger
# (protected copy first, workspace copy as fallback) to the server's
# poc_records, and enforces the poc_hash == poc_sha256 gate.  POST /query-poc
# is a private route: this must run as root with /root/.cybergym_env.
#
# Usage:
#   score_all.sh <out_root> [<task_base>] [<ws_root>]
#     <out_root>   run root, e.g. /var/lib/cybergym/runs/r_engineer
#     <task_base>  prepared task dirs (default <out_root>/tasks)
#     <ws_root>    workspaces        (default <out_root>-ws)
#   S4_RUN_ID=<id> restricts scoring to that run's rows.
#
# Exit codes: 0 ok / 1 integrity failures / 2 infra errors.
# There is deliberately NO flag to score without a ledger; legacy data needs
# its own explicitly-marked tool.
set -u
OUT="${1:?usage: score_all.sh <out_root> [<task_base>] [<ws_root>]}"
TASKBASE="${2:-$OUT/tasks}"
WSROOT="${3:-$OUT-ws}"
AG="${S4_AGENT_HOME:-/opt/cybergym-agent}"
set -a; . /root/.cybergym_env; set +a
RUN_ARGS=()
[ -n "${S4_RUN_ID:-}" ] && RUN_ARGS=(--run-id "$S4_RUN_ID")

# shellcheck disable=SC2086
exec python3 "$AG/scripts/score_all.py" \
    --out "$OUT" --task-base "$TASKBASE" --ws-root "$WSROOT" \
    ${RUN_ARGS[@]+"${RUN_ARGS[@]}"} \
    ${S4_SCORE_EXTRA:-}
