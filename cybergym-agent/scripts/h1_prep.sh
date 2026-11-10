#!/bin/bash
set -u
cd /tmp
for f in h1_deploy/pass2_triage.py h1_assemble.py h1_driver.sh h1_freeze.py h1_freeze_run.sh h1_failinject.sh; do
  sed -i 's/\r$//' "$f"
done
python3 -m py_compile h1_deploy/pass2_triage.py h1_assemble.py h1_freeze.py \
  && bash -n h1_driver.sh && bash -n h1_failinject.sh && echo SYNTAX_OK
python3 h1_deploy/pass2_triage.py --render-prompts /tmp/rl.txt
S4_D_STRATEGY=legacy python3 h1_deploy/pass2_triage.py --render-prompts /tmp/rl2.txt
if diff -q /tmp/rl.txt /tmp/rl2.txt >/dev/null; then echo ENV_DEFAULT_BAD; else echo ENV_DEFAULT_DIFFERS; fi
echo "-- legacy manifest:"; cat /tmp/rl2.txt
echo "-- construct manifest:"; cat /tmp/rl.txt
# shared categories must be byte-identical
python3 - <<'EOF'
la = {l.split("\t")[1]: l.split("\t")[0] for l in open("/tmp/rl2.txt").read().splitlines()}
va = {l.split("\t")[1]: l.split("\t")[0] for l in open("/tmp/rl.txt").read().splitlines()}
same = [c for c in la if c != "D" and la[c] == va[c]]
diff = [c for c in la if la[c] != va[c]]
print("SHARED_IDENTICAL:", sorted(same))
print("DIFFERS (must be only D):", sorted(diff))
assert sorted(diff) == ["D"], diff
print("H1_UNIQUE_VARIABLE_OK")
EOF
