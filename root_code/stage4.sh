#!/bin/bash
# stage4.sh: entry point for a clean stage-4 run (queue driver).
#
#   /root/stage4.sh --queue /data/cybergym/stage4/queue.json \
#       --out /data/cybergym/stage4/r_engineer --workers 3
#
# Must run as root (the per-task wrapper is root-only; agent/workspace still
# run as naco).  Scores and verifies the run afterwards unless --no-score.
set -u
AG="${S4_AGENT_HOME:-/home/naco/work/cybergym-agent}"
exec python3 "$AG/scripts/stage4.py" "$@"
