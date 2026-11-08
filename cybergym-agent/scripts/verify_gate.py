#!/usr/bin/env python3
"""Claude Code Stop hook: a session may not end before its first check.py run
(PR-A recovery of the control guarantee that formal50 ran WITHOUT -- the hook
source never existed anywhere on the deployment host, so make_agent_ws.py
wrote settings.json pointing at a dead file).

No historical source exists; this file is newly authored for PR-A (see
docs/TOOLCHAIN_PROVENANCE.md). Semantics follow the make_agent_ws.py design
comment: "a session may not end before its first check.py run (prompt rules
alone did not hold on hard tasks)".

Exit codes (Claude Code Stop-hook contract):
  0  allow the session to stop
  2  BLOCK the stop; stderr is shown to the agent as the reason

Allow-stop conditions (any one):
  - .checks > 0                      at least one check.py attempt happened
  - SOLUTION.md exists               agent wrote its closing statement
  - S4_HOOK_RELAX=1 in the env       driver-side escape (e.g. pass2 forced
                                     submit already queued, or diag harness)

The hook runs with cwd = the task workspace (Claude Code sets it to the
project dir). It is read-only: it never mutates workspace state.
"""
import os
import sys
from pathlib import Path


def main() -> int:
    if os.environ.get("S4_HOOK_RELAX") == "1":
        return 0
    checks = Path(".checks")
    try:
        n = int(checks.read_text(encoding="utf-8", errors="replace").strip() or "0")
    except OSError:
        n = 0
    if n > 0:
        return 0
    if Path("SOLUTION.md").is_file():
        return 0
    print("STOP-BLOCKED (verify_gate): the session has not run check.py even "
          "once. Construct a best-effort poc.bin, gate it through "
          "./try_local.sh, then run `python3 check.py poc.bin` before "
          "stopping. If the task is truly unsolvable, write SOLUTION.md with "
          "'STATUS: UNSOLVED' and stop again.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
