#!/usr/bin/env python3
"""h1_assemble.py -- build the H1 experiment tree (r22-approved design).

H1 unique variable = category-D prompt ONLY:
  arm DLegacy: S4_D_STRATEGY=legacy  (triage reads env; wrapper hook
             unchanged from the ab patch -- env inheritance carries it)
  arm DV4:     S4_D_STRATEGY=construct
Both arms run files-only triage with identical v4.2 B/C/A2/GENERIC
prompts. Patch = same two anchors as ab (verified against stock sha),
REFUSE on any mismatch; output tree refuses to overwrite.
"""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path("/data2/arms/pair/b-agent")
DST = Path("/data2/arms/h1/h1-agent")
STAGE = Path("/tmp/h1_deploy")
STOCK_SHA = "be8f9b8a"

PASS2_OLD = (
    '  PASS2_PROMPT="A previous session on this task ended without ever '
    'running check.py (0 of ${CHECKS_BUDGET} checks used). Do that now: '
    'based on description.txt and at most 5 files from repo/, immediately '
    'write poc_gen.py, produce poc.bin, and run python3 check.py poc.bin. '
    'Iterate on its real output at most 3 times, changing approach each '
    'time. Stop when VERDICT: CRASH (then write SOLUTION.md) or after 3 '
    'failed checks."')
PASS2_NEW = (
    '  # h1 experiment patch: both arms use root-side triage; the D-arm\n'
    '  # variable arrives via S4_D_STRATEGY (legacy|construct) inherited\n'
    '  # through stage4 env -> wrapper -> triage --d-strategy default.\n'
    '  if [ -n "${S4_P2_PROMPT_FILE:-}" ] && [ -s "${S4_P2_PROMPT_FILE:-}" ]; then\n'
    '    PASS2_PROMPT="$(cat "$S4_P2_PROMPT_FILE")"\n'
    '  else\n'
    '    PASS2_PROMPT="A previous session on this task ended without ever running check.py (0 of ${CHECKS_BUDGET} checks used). Do that now: based on description.txt and at most 5 files from repo/, immediately write poc_gen.py, produce poc.bin, and run python3 check.py poc.bin. Iterate on its real output at most 3 times, changing approach each time. Stop when VERDICT: CRASH (then write SOLUTION.md) or after 3 failed checks."\n'
    '  fi')

HOOK_ANCHOR = '  echo "=== second pass (forced submit) $TID start $(date -u +%FT%TZ) ==="'
HOOK_NEW = HOOK_ANCHOR + '\n' + (
    '  # h1 triage hook: BOTH arms (files-only); D-arm via S4_D_STRATEGY\n'
    '  if [ "${S4_PASS2_TRIAGE:-0}" = "1" ]; then\n'
    '    _PF="$LOGDIR/${NAME}.p2prompt"\n'
    '    if python3 "$AG/scripts/pass2_triage.py" --ws "$WS" --task-id "$TID" \\\n'
    '        --logdir "$LOGDIR" --mode files-only --prompt-out "$_PF" \\\n'
    '        >> "$LOGDIR/${NAME}.triage.stdout" 2>&1 && [ -s "$_PF" ]; then\n'
    '      export S4_P2_PROMPT_FILE="$_PF"\n'
    '    else\n'
    '      echo "H1_WARN: triage failed; falling back to legacy prompt" >> "$LOGDIR/${NAME}.triage.stdout"\n'
    '    fi\n'
    '  fi')


def refuse(msg):
    print(f"REFUSE(h1_assemble): {msg}", file=sys.stderr)
    shutil.rmtree(DST, ignore_errors=True)
    sys.exit(1)


def main():
    if DST.exists():
        refuse(f"{DST} exists")
    for f in ("pass2_triage.py",):
        if not (STAGE / f).is_file():
            refuse(f"missing staged {f}")
    shutil.copytree(SRC, DST)
    w = DST / "scripts" / "run_agent_task.sh"
    if hashlib.sha256(w.read_bytes()).hexdigest()[:8] != STOCK_SHA:
        refuse("wrapper sha mismatch")
    text = w.read_text()
    if text.count(PASS2_OLD) != 1:
        refuse("PASS2_PROMPT anchor mismatch")
    text = text.replace(PASS2_OLD, PASS2_NEW)
    if text.count(HOOK_ANCHOR) != 1:
        refuse("pass2-start anchor mismatch")
    text = text.replace(HOOK_ANCHOR, HOOK_NEW)
    w.write_text(text)
    if subprocess.run(["bash", "-n", str(w)]).returncode != 0:
        refuse("patched wrapper fails bash -n")
    shutil.copyfile(STAGE / "pass2_triage.py",
                    DST / "scripts" / "pass2_triage.py")
    if subprocess.run(["python3", "-m", "py_compile",
                       str(DST / "scripts" / "pass2_triage.py")
                       ]).returncode != 0:
        refuse("triage py_compile")
    # prompt-SHA manifest: render one prompt per (category, d-arm) and
    # hash -- proves B/C/A2/GENERIC identical across arms, D differs.
    import os
    os.environ["S4_D_STRATEGY"] = "legacy"
    r = subprocess.run(["python3", str(DST / "scripts" / "pass2_triage.py"),
                        "--render-prompts",
                        str(STAGE / "h1_prompts_DLegacy.sha256")],
                       capture_output=True, text=True, env=os.environ)
    if r.returncode != 0:
        refuse(f"render legacy failed: {r.stderr[-200:]}")
    os.environ["S4_D_STRATEGY"] = "construct"
    r = subprocess.run(["python3", str(DST / "scripts" / "pass2_triage.py"),
                        "--render-prompts",
                        str(STAGE / "h1_prompts_DV4.sha256")],
                       capture_output=True, text=True, env=os.environ)
    if r.returncode != 0:
        refuse(f"render v4 failed: {r.stderr[-200:]}")
    la = (STAGE / "h1_prompts_DLegacy.sha256").read_text().splitlines()
    va = (STAGE / "h1_prompts_DV4.sha256").read_text().splitlines()

    def pairs(lines):
        d = {}
        for l in lines:
            sha, cat, _arm = l.split("\t")
            d[cat] = sha
        return d
    pl, pv = pairs(la), pairs(va)
    same = [c for c in pl if c != "D" and pl[c] == pv[c]]
    diff = [c for c in pl if pl[c] != pv[c]]
    if sorted(same) != ["A1", "A2", "B", "C", "GENERIC"]:
        refuse(f"shared prompts differ across arms: {pl} vs {pv}")
    if sorted(diff) != ["D"]:
        refuse(f"more than D differs across arms: {diff}")
    d_l = [f"{pl['D']}\tD"]
    d_v = [f"{pv['D']}\tD"]
    manifest = {
        "run_agent_task.sh": hashlib.sha256(
            w.read_bytes()).hexdigest()[:16],
        "pass2_triage.py": hashlib.sha256(
            (DST / "scripts" / "pass2_triage.py").read_bytes()
        ).hexdigest()[:16],
        "shared_prompts": same, "d_legacy": d_l, "d_v4": d_v,
        "source": str(SRC), "stock_sha": STOCK_SHA}
    (DST / "H1_MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    print("H1_ASSEMBLE_OK", json.dumps(manifest)[:400])


main()
