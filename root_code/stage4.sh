#!/bin/bash
# stage4.sh: TRUSTED root entry point for a clean stage-4 run (queue driver).
#
#   /root/stage4.sh --queue /data/cybergym/stage4/queue.json \
#       --out /data/cybergym/stage4/r_engineer --workers 3
#
# R3 (dedicated-user review): trust in the code tree is established HERE,
# before the driver's Python process starts -- a check inside the same tree
# it validates can never establish trust, because importing the plugins is
# already executing the code an attacker may have rewritten.
#
# Sequence:
#   1. must run as root;
#   2. remove stale __pycache__ dirs (bytecode caches owned by an untrusted
#      account could shadow the validated sources);
#   3. audit the whole tree + every ancestor from assert_code_tree.py, which
#      is deployed NEXT TO THIS SCRIPT (outside the audited tree) and imports
#      nothing from the tree;
#   4. exec the driver with -B (never write bytecode caches as root either).
set -eu
[ "$(id -u)" -eq 0 ] || { echo "stage4.sh must run as root" >&2; exit 3; }

# R3.4: refuse the DEV-only degraded (unprotected-ledger) escape hatch HERE,
# at the trusted entry, before the driver queues anything.  Renaming the
# variable is not enough to make a run formal; the protected ledger is the
# precondition for spending model budget at all.
if [ "${S4_ALLOW_UNPROTECTED:-0}" = "1" ]; then
  echo "REFUSE: S4_ALLOW_UNPROTECTED=1 is a DEV-only escape hatch (unprotected ledger); the formal stage-4 entry point will not run with it. Unset it (dev runs call run_agent_task.sh directly)." >&2
  exit 4
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
AG="${S4_AGENT_HOME:-/opt/cybergym-agent}"
UNTRUSTED="${S4_UNTRUSTED_USERS:-${S4_AGENT_USER:-s4agent},${S4_PREP_USER:-naco}}"

find "$AG" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
python3 "$HERE/assert_code_tree.py" --base "$AG" --untrusted-users "$UNTRUSTED"

S4_AGENT_HOME="$AG" exec python3 -B "$AG/scripts/stage4.py" "$@"
