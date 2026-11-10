#!/usr/bin/env python3
"""h1_assemble.py -- build the H1 experiment tree (r22 design, r23 hardening).

H1 unique variable = category-D prompt ONLY:
  arm DLegacy: S4_D_STRATEGY=legacy
  arm DV4:     S4_D_STRATEGY=construct
Both arms run files-only triage with identical v4.2 B/C/A2/GENERIC
prompts. Patch = same two anchors as ab (verified against stock sha),
REFUSE on any mismatch.

r23 P0 fix (destructive refuse): assembly now happens in a PRIVATE
temp dir (.h1-agent.tmp.<pid>, assembler-owned namespace). A
pre-existing DST is refused WITHOUT ANY modification or deletion;
only the temp dir this process created is ever cleaned up. Success
promotes the temp dir to DST with an atomic same-filesystem rename.

Test hooks (documented, default = production paths):
  H1_SRC / H1_DST / H1_STAGE environment overrides.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(os.environ.get("H1_SRC", "/data2/arms/pair/b-agent"))
DST = Path(os.environ.get("H1_DST", "/data2/arms/h1/h1-agent"))
STAGE = Path(os.environ.get("H1_STAGE", "/tmp/h1_deploy"))
STOCK_SHA = "be8f9b8a"
STAGED_FILES = ("pass2_triage.py", "h1_prompt_audit.py")

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

TMP = None          # assembler-owned temp dir (set once created)


def refuse(msg):
    """Fail loud. NEVER touches DST; cleans only the temp dir this
    process created (.h1-agent.tmp.<pid> namespace is ours alone)."""
    print(f"REFUSE(h1_assemble): {msg}", file=sys.stderr)
    if TMP is not None:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1)


def main():
    global TMP
    if DST.exists():
        # r23 P0: existing tree (experiment results!) -- refuse and leave
        # every byte untouched.
        refuse(f"{DST} exists (refusing to overwrite or delete)")
    for f in STAGED_FILES:
        if not (STAGE / f).is_file():
            refuse(f"missing staged {f}")
    DST.parent.mkdir(parents=True, exist_ok=True)
    TMP = DST.parent / f".h1-agent.tmp.{os.getpid()}"
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    shutil.copytree(SRC, TMP)
    w = TMP / "scripts" / "run_agent_task.sh"
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
    for f in ("pass2_triage.py", "h1_prompt_audit.py"):
        shutil.copyfile(STAGE / f, TMP / "scripts" / f)
    if subprocess.run(["python3", "-m", "py_compile",
                       str(TMP / "scripts" / "pass2_triage.py")
                       ]).returncode != 0:
        refuse("triage py_compile")
    if subprocess.run(["python3", "-m", "py_compile",
                       str(TMP / "scripts" / "h1_prompt_audit.py")
                       ]).returncode != 0:
        refuse("audit py_compile")
    # prompt-SHA manifest: render one prompt per (category, d-arm) and
    # hash -- proves B/C/A2/GENERIC identical across arms, D differs.
    sha_maps = {}
    for arm, env_ds in (("DLegacy", "legacy"), ("DV4", "construct")):
        env = dict(os.environ, S4_D_STRATEGY=env_ds)
        r = subprocess.run(
            ["python3", str(TMP / "scripts" / "pass2_triage.py"),
             "--render-prompts", str(STAGE / f"h1_prompts_{arm}.sha256")],
            capture_output=True, text=True, env=env)
        if r.returncode != 0:
            refuse(f"render {env_ds} failed: {r.stderr[-200:]}")
        m = {}
        for line in (STAGE / f"h1_prompts_{arm}.sha256").read_text().splitlines():
            sha, cat, _arm = line.split("\t")
            m[cat] = sha
        sha_maps[env_ds] = m
    pl, pv = sha_maps["legacy"], sha_maps["construct"]
    same = [c for c in pl if c != "D" and pl[c] == pv[c]]
    diff = [c for c in pl if pl[c] != pv[c]]
    if sorted(same) != ["A1", "A2", "B", "C", "GENERIC"]:
        refuse(f"shared prompts differ across arms: {pl} vs {pv}")
    if sorted(diff) != ["D"]:
        refuse(f"more than D differs across arms: {diff}")
    manifest = {
        "run_agent_task.sh": hashlib.sha256(
            w.read_bytes()).hexdigest()[:16],
        "pass2_triage.py": hashlib.sha256(
            (TMP / "scripts" / "pass2_triage.py").read_bytes()
        ).hexdigest()[:16],
        "h1_prompt_audit.py": hashlib.sha256(
            (TMP / "scripts" / "h1_prompt_audit.py").read_bytes()
        ).hexdigest()[:16],
        "shared_prompts": same,
        "d_legacy": [f"{pl['D']}\tD"], "d_v4": [f"{pv['D']}\tD"],
        "sha_map_legacy": pl, "sha_map_v4": pv,
        "source": str(SRC), "stock_sha": STOCK_SHA}
    (TMP / "H1_MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    # atomic promote: same filesystem, DST verified absent at entry;
    # os.rename fails loudly if a concurrent process created it meanwhile.
    try:
        os.rename(TMP, DST)
    except OSError as e:
        refuse(f"atomic promote failed ({e}); temp dir cleaned, DST untouched")
    print("H1_ASSEMBLE_OK", json.dumps(manifest)[:400])


main()
