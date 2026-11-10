#!/bin/bash
# h1_failinject.sh -- r23 fault-injection acceptance.
# KEY DIFFERENCE from r22 version: no extraction/slicing. Every full
# case below runs the REAL /tmp/h1_driver.sh unmodified, driving it
# through documented test hooks only (H1_STUB_DIR, H1_OUTBASE,
# H1_WALL_DEADLINE_MIN, H1_FINALIZE_TIMEOUT_MIN, H1_BUDGET_STOP_USD,
# H1_POLL_S) -- so launch, wait, budget, recovery, supervise and
# finalize paths are exercised as shipped.
#
# Reviewer matrix (r23 section 5) -> cases:
#   existing-dir assemble refusal        -> case 01
#   worker started then wall timeout     -> case 02
#   worker nonzero exit blocks finalize  -> case 03
#   finalize command hangs               -> case 08 (supervised kill)
#   budget stop with in-flight workers   -> case 09
#   dry-run step failure gates PASS      -> case 13
#   full success path                    -> case 10
#   runtime prompt audit                 -> cases 11/12
#   (kept from r22: backfill/score/verify rc + violations) -> 04-07
set -u
BASE=/tmp/h1_failinject
rm -rf "$BASE"; mkdir -p "$BASE/stubs" "$BASE/markers"
PASS=0; FAIL=0
ok()  { echo "case $1: PASS ($2)"; PASS=$((PASS+1)); }
bad() { echo "case $1: FAIL ($2)"; FAIL=$((FAIL+1)); }

marker_absent() { [ ! -e "$BASE/markers/$1" ]; }
worker_dead() {  # $1 pidfile -- process fully gone (not zombie)
  local P; P=$(cat "$1" 2>/dev/null) || return 0
  sleep 2
  ! ps -p "$P" -o stat= >/dev/null 2>&1
}

# scratch OUTBASE the real driver can run against
prep_base() {
  rm -rf "$BASE/out"; mkdir -p "$BASE/out/h1-agent"
  echo '{"sha_map_legacy":{"D":"l"},"sha_map_v4":{"D":"v"}}' \
    > "$BASE/out/h1-agent/H1_MANIFEST.json"
  printf '["arvo:1"]\n' > "$BASE/out/q16frozen.json"
  rm -rf "$BASE/markers"; mkdir -p "$BASE/markers"
}
# finalize stubs; each records a marker so we can prove NOT-invocation
mk_finalize() {  # $1=backfill_rc  $2=score_rc  $3=verify_rc  $4=violations_line  $5=backfill_sleep
  if [ "${5:-0}" = "0" ]; then
    printf '#!/bin/bash\necho backfill > %s/markers/backfill\nexit %s\n' \
      "$BASE" "$1" > "$BASE/stubs/backfill.sh"
  else
    # exec sleep: when the supervisor TERMs the stub, the sleeper dies
    # with it (no orphaned child outside timeout's reach)
    printf '#!/bin/bash\necho backfill > %s/markers/backfill\nexec sleep %s\n' \
      "$BASE" "$5" > "$BASE/stubs/backfill.sh"
  fi
  printf '#!/usr/bin/env python3\nimport sys\nopen("%s/markers/score","w").write("x")\nprint("scored")\nsys.exit(%s)\n' \
    "$BASE" "$2" > "$BASE/stubs/score.py"
  printf '#!/usr/bin/env python3\nimport sys\nopen("%s/markers/verify","w").write("x")\nprint("%s")\nsys.exit(%s)\n' \
    "$BASE" "$4" "$3" > "$BASE/stubs/verify.py"
  printf '#!/usr/bin/env python3\nimport sys\no=sys.argv[sys.argv.index("--out")+1]\nopen(o,"w").write(chr(123)+chr(34)+"stub"+chr(34)+":true"+chr(125))\nopen("%s/markers/audit","w").write("x")\nsys.exit(0)\n' \
    "$BASE" > "$BASE/stubs/audit.py"
  chmod +x "$BASE/stubs"/*
}
mk_worker() {  # $1=variant: exit0|exit1|sleep|budget
  cat > "$BASE/stubs/worker.sh" <<EOF
#!/bin/bash
ARM=\$1
echo "arm \$ARM worker starting"
echo "run_id=s4-fit\${ARM}1"
case "$1" in
  exit0) exit 0 ;;
  exit1) exit 1 ;;
  sleep) echo \$\$ > "$BASE/worker-\$ARM.pid"; exec sleep 3600 ;;
  budget) mkdir -p "\$H1_OUTBASE/\$ARM/rows"
          echo '{"task_id":"arvo:1","cost_usd":99.99}' > "\$H1_OUTBASE/\$ARM/rows/fake.json"
          echo \$\$ > "$BASE/worker-\$ARM.pid"; exec sleep 3600 ;;
esac
exit 1
EOF
  chmod +x "$BASE/stubs/worker.sh"
}
run_driver() { H1_OUTBASE="$BASE/out" H1_STUB_DIR="$BASE/stubs" H1_POLL_S=1 bash /tmp/h1_driver.sh > "$BASE/driver.out" 2>&1; }
markers_state() { ls "$BASE/markers" 2>/dev/null | tr '\n' ',' ; }

# ---- 01: assembler refuses an existing DST and deletes NOTHING -----------
prep_base
D="$BASE/existing-dst"; mkdir -p "$D"
head -c 4096 /dev/urandom > "$D/sentinel.bin"
S1=$(sha256sum "$D/sentinel.bin" | cut -d' ' -f1)
H1_DST="$D" H1_SRC=/data2/arms/pair/b-agent H1_STAGE=/tmp/h1_deploy \
  python3 /tmp/h1_assemble.py > "$BASE/asm.out" 2>&1
RC=$?
S2=$(sha256sum "$D/sentinel.bin" | cut -d' ' -f1)
LEFT=$(ls -A "$D" | grep -v '^sentinel.bin$' | head -3)
if [ "$RC" != 0 ] && [ "$S1" = "$S2" ] && [ -z "$LEFT" ] \
   && ! ls -d "$BASE"/out/.h1-agent.tmp.* >/dev/null 2>&1 \
   && grep -q "REFUSE" "$BASE/asm.out"; then
  ok 01 "REFUSE rc=$RC sentinel intact"
else
  bad 01 "rc=$RC s1s2=$([ "$S1" = "$S2" ] && echo same || echo DIFF) extra=$LEFT"
fi

# ---- 02: wall deadline (set BEFORE launch) fires -> controlled recovery --
prep_base; mk_worker sleep; mk_finalize 0 0 0 "violations=0" 0
H1_WALL_DEADLINE_MIN=0 run_driver; RC=$?
if [ "$RC" != 0 ] \
   && grep -q "stage=wall_guard" "$BASE/out/FAILED_AT" \
   && grep -q "cleanup=verified" "$BASE/out/FAILED_AT" \
   && [ ! -e "$BASE/out/FINALIZED_AT" ] \
   && worker_dead "$BASE/worker-DLegacy.pid" \
   && worker_dead "$BASE/worker-DV4.pid" \
   && marker_absent backfill; then
  ok 02 "wall_guard + workers recovered + no finalize"
else
  bad 02 "rc=$RC FAILED=$(cat "$BASE/out/FAILED_AT" 2>/dev/null) markers=$(markers_state)"
fi

# ---- 03: worker nonzero exit -> finalize chain never runs ----------------
prep_base; mk_worker exit1; mk_finalize 0 0 0 "violations=0" 0
run_driver; RC=$?
if [ "$RC" != 0 ] && grep -q "stage=worker" "$BASE/out/FAILED_AT" \
   && [ ! -e "$BASE/out/FINALIZED_AT" ] \
   && marker_absent backfill && marker_absent score && marker_absent verify \
   && marker_absent audit; then
  ok 03 "worker fail blocks backfill/score/verify/audit"
else
  bad 03 "rc=$RC markers=$(markers_state)"
fi

# ---- 04..07: finalize rc / violations gating (kept from r22, full driver)
fin_case() {  # name brc src vrc vline want_stage
  prep_base; mk_worker exit0; mk_finalize "$2" "$3" "$4" "$5" 0
  run_driver; RC=$?
  if [ "$RC" != 0 ] && grep -q "stage=$6" "$BASE/out/FAILED_AT" \
     && [ ! -e "$BASE/out/FINALIZED_AT" ]; then
    ok "$1" "stage=$6"
  else
    bad "$1" "rc=$RC want=$6 got=$(cat "$BASE/out/FAILED_AT" 2>/dev/null)"
  fi
}
fin_case 04_backfill_rc    1 0 0 "violations=0" backfill
fin_case 05_score_rc       0 1 0 "violations=0" score
fin_case 06_verify_rc      0 0 1 "violations=0" verify
fin_case 07_verify_violations 0 0 0 "violations=2 warnings=1" verify

# ---- 08: a HUNG finalize command is actually killed by the supervisor ----
prep_base; mk_worker exit0; mk_finalize 0 0 0 "violations=0" 3600
H1_FINALIZE_TIMEOUT_MIN=1 run_driver; RC=$?
if [ "$RC" != 0 ] && grep -q "stage=backfill" "$BASE/out/FAILED_AT" \
   && grep -q "rc=124\|rc=137" "$BASE/out/FAILED_AT" \
   && [ ! -e "$BASE/out/FINALIZED_AT" ]; then
  ok 08 "hung backfill supervised-killed (rc=124/137), driver returned"
else
  bad 08 "rc=$RC $(cat "$BASE/out/FAILED_AT" 2>/dev/null)"
fi

# ---- 09: budget stop terminates in-flight workers, no finalize ----------
prep_base; mk_worker budget; mk_finalize 0 0 0 "violations=0" 0
H1_BUDGET_STOP_USD=0.01 run_driver; RC=$?
if [ "$RC" != 0 ] && grep -q "stage=budget" "$BASE/out/FAILED_AT" \
   && grep -q "cleanup=verified" "$BASE/out/FAILED_AT" \
   && [ ! -e "$BASE/out/FINALIZED_AT" ] \
   && worker_dead "$BASE/worker-DLegacy.pid" \
   && worker_dead "$BASE/worker-DV4.pid"; then
  ok 09 "budget stop + in-flight workers recovered"
else
  bad 09 "rc=$RC $(cat "$BASE/out/FAILED_AT" 2>/dev/null)"
fi

# ---- 10: full success path -> FINALIZED_AT only after BOTH arms ----------
prep_base; mk_worker exit0; mk_finalize 0 0 0 "violations=0" 0
run_driver; RC=$?
if [ "$RC" = 0 ] && [ -e "$BASE/out/FINALIZED_AT" ] \
   && [ ! -e "$BASE/out/FAILED_AT" ] \
   && [ -e "$BASE/markers/audit" ] \
   && [ -e "$BASE/out/H1_RUNTIME_PROMPT_AUDIT.json" ]; then
  ok 10 "both arms finalized; audit artifact present"
else
  bad 10 "rc=$RC finalized=$([ -e "$BASE/out/FINALIZED_AT" ] && echo Y || echo N) markers=$(markers_state)"
fi

# ---- 11/12: runtime prompt audit unit (real h1_prompt_audit.py) ----------
AUD="$BASE/auditcase"; rm -rf "$AUD"; mkdir -p "$AUD/logs/s4-audittest"
python3 - "$AUD" <<'EOF'
import hashlib, json, sys, os
base = sys.argv[1]
d = os.path.join(base, "logs", "s4-audittest")
def w(n, s): open(os.path.join(d, n), "w").write(s)
def sha(s): return hashlib.sha256(s.encode()).hexdigest()
pb, dl, dv = "PROMPT_B_SHARED", "PROMPT_D_LEGACY", "PROMPT_D_V4"
w("t1.triage.json", json.dumps({"category": "B"}))
w("t1.p2prompt", pb)                      # verified (shared sha)
w("t2.triage.json", json.dumps({"category": "D"}))
w("t2.triage.stdout", "H1_WARN: triage failed; falling back\n")  # fallback
json.dump({"sha_map_legacy": {"B": sha(pb), "D": sha(dl), "GENERIC": sha("g")},
           "sha_map_v4": {"B": sha(pb), "D": sha(dv), "GENERIC": sha("g")}},
          open(os.path.join(base, "manifest.json"), "w"))
EOF
python3 /tmp/h1_prompt_audit.py --manifest "$AUD/manifest.json" \
  --logroot "$AUD/logs" --arm "A:legacy:s4-audittest" \
  --arm "B:construct:s4-audittest" --out "$AUD/audit.json" > "$AUD/run1.out" 2>&1
RC1=$?
V=$(python3 -c "import json;a=json.load(open('$AUD/audit.json'));print(len(a['arms']['A']['verified']),len(a['arms']['A']['fallback']),len(a['arms']['A']['violations']))" 2>/dev/null)
if [ "$RC1" = 0 ] && [ "$V" = "1 1 0" ] \
   && grep -q "violations=0" "$AUD/run1.out"; then
  ok 11 "audit: verified=1 fallback=1 violations=0"
else
  bad 11 "rc=$RC1 v=$V out=$(tail -1 "$AUD/run1.out" 2>/dev/null)"
fi
# poison: a B-category prompt whose sha is in NO allowed set
cp "$AUD/logs/s4-audittest/t1.p2prompt" "$AUD/.bak"
echo "EVIL_PROMPT_NOT_IN_ANY_SET" > "$AUD/logs/s4-audittest/t1.p2prompt"
python3 /tmp/h1_prompt_audit.py --manifest "$AUD/manifest.json" \
  --logroot "$AUD/logs" --arm "A:legacy:s4-audittest" \
  --out "$AUD/audit2.json" > "$AUD/run2.out" 2>&1
RC2=$?
grep -q "prompt_sha_mismatch" "$AUD/audit2.json" && MM=1 || MM=0
mv "$AUD/.bak" "$AUD/logs/s4-audittest/t1.p2prompt"
if [ "$RC2" != 0 ] && [ "$MM" = 1 ]; then
  ok 12 "audit: poisoned prompt -> rc!=0 + violation recorded"
else
  bad 12 "rc=$RC2 mismatch_recorded=$MM"
fi

# ---- 13: a failing dry-run step gates H1_DRYRUN_PASS ---------------------
DRA="$BASE/dryrun"; rm -rf "$DRA"; mkdir -p "$DRA"
H1_DRYRUN_SELFTEST_FAIL=assemble H1_OUTBASE="$DRA" \
  bash /tmp/h1_dryrun.sh > "$DRA/out.log" 2>&1
RC=$?
if [ "$RC" != 0 ] && grep -q "DRYRUN_FAIL" "$DRA/out.log" \
   && ! grep -q "H1_DRYRUN_PASS" "$DRA/out.log"; then
  ok 13 "dry-run failure -> nonzero rc, no PASS marker"
else
  bad 13 "rc=$RC log=$(tail -1 "$DRA/out.log" 2>/dev/null)"
fi

echo "passed=$PASS failed=$FAIL"
[ "$FAIL" = 0 ] && echo "H1_FAILINJECT_PASS" || { echo "H1_FAILINJECT_FAIL"; exit 1; }
