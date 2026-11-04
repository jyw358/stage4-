#!/usr/bin/env python3
"""validate_agent_uid.py -- the ONLY authority for "which uid runs this task".

R3-M2 (review): run_agent_task.sh used
``AGENT_UID="${S4_AGENT_UID:-$(cat control/agent_uid)}"`` and only checked
"is numeric".  A directly invoked root wrapper could therefore run the agent
under a uid the control plane never issued (including 0 = root).  This helper
reads the control-plane uid and, when the environment also provides one, they
must be EQUAL; uid 0, the prep identity, and any uid outside the task pool
are refused.

Exit 0 + the uid on stdout only when everything checks out; otherwise exit 4
with a reason on stderr.
"""
from __future__ import annotations

import argparse
import os
import sys


def resolve_agent_uid(control_uid: str, env_uid: str, prep_uid: int,
                      base: int, span: int) -> tuple:
    """(uid, error).  Pure; unit-tested.  control_uid is authoritative."""
    try:
        c = int(str(control_uid).strip()) if str(control_uid).strip() else None
    except ValueError:
        return None, f"control agent_uid {control_uid!r} is not numeric"
    try:
        e = int(str(env_uid).strip()) if str(env_uid).strip() else None
    except ValueError:
        return None, f"S4_AGENT_UID {env_uid!r} is not numeric"
    if c is None:
        return None, "control plane has no agent_uid (task was not prepared)"
    if e is not None and e != c:
        return None, (f"S4_AGENT_UID={e} != control agent_uid={c}; refusing to "
                      "run the agent under an unissued identity")
    if c == 0:
        return None, "agent uid 0 (root) is never allowed"
    if c == prep_uid:
        return None, f"agent uid {c} equals the prep identity uid; refused"
    if not (base <= c < base + span):
        return None, f"agent uid {c} is outside the task pool [{base}, {base + span})"
    return c, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--control", required=True,
                    help="path to <out>/control/<task>/agent_uid")
    ap.add_argument("--env-uid", default=os.environ.get("S4_AGENT_UID", ""),
                    help="S4_AGENT_UID from the driver (optional)")
    ap.add_argument("--prep-user", default=os.environ.get("S4_PREP_USER", "naco"))
    ap.add_argument("--base", type=int,
                    default=int(os.environ.get("S4_AGENT_UID_BASE", "30001")))
    ap.add_argument("--span", type=int,
                    default=int(os.environ.get("S4_AGENT_UID_SPAN", "4096")))
    args = ap.parse_args()

    control_uid = ""
    try:
        with open(args.control, encoding="utf-8") as fh:
            control_uid = fh.read().strip()
    except OSError as e:
        print(f"REFUSE: cannot read control agent_uid: {e}", file=sys.stderr)
        return 4
    prep_uid = -1
    try:
        import pwd
        prep_uid = pwd.getpwnam(args.prep_user).pw_uid
    except (ImportError, KeyError):
        pass   # prep account missing is a deploy problem, not a uid issue
    uid, err = resolve_agent_uid(control_uid, args.env_uid, prep_uid,
                                 args.base, args.span)
    if err:
        print(f"REFUSE: {err}", file=sys.stderr)
        return 4
    print(uid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
