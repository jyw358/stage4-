#!/usr/bin/env python3
"""h1_freeze.py -- H1 frozen queue: 16 fresh tasks, deterministic pick,
exclusion = formal50 + pair + p2 + y1 + ab round-1 (12) + prior freeze."""
import glob
import hashlib
import json
import os

used = set()
for f in (glob.glob("/data2/runs/formal50/rows/*.json")
          + glob.glob("/data2/arms/pair/*run*/rows/*.json")
          + glob.glob("/data2/arms/p2/[ABC]/rows/*.json")
          + glob.glob("/data2/arms/ab/*/rows/*.json")):
    used.add(json.load(open(f))["task_id"])
for f in glob.glob("/data2/runs/y1-diag/tasks/*/run.env"):
    for line in open(f):
        if line.startswith("export CG_TASK_ID="):
            used.add(line.split("=", 1)[1].strip())
print("excluded tasks:", len(used))

pool = sorted((p for p in os.listdir(
    "/data/cybergym/cybergym-server-data/arvo") if p.isdigit()), key=int)
fresh = [f"arvo:{t}" for t in pool if f"arvo:{t}" not in used]
print("fresh candidates:", len(fresh))

# deterministic rule (frozen BEFORE any run): take every k-th fresh id so
# both H1 round-1 (16) and a possible round-2 (next 16 under the SAME
# stride, offset 16) are fixed now -- no post-hoc selection.
stride = max(1, len(fresh) // 64)
selected = fresh[::stride]
round1, round2 = selected[:16], selected[16:32]
assert len(round1) == 16 and len(round2) == 16

out = "/data2/arms/h1"
os.makedirs(out, exist_ok=True)
json.dump(round1, open(f"{out}/q16frozen.json", "w"))
json.dump(round2, open(f"{out}/q16frozen_round2.json", "w"))
h1 = hashlib.sha256(open(f"{out}/q16frozen.json", "rb").read()).hexdigest()[:16]
h2 = hashlib.sha256(
    open(f"{out}/q16frozen_round2.json", "rb").read()).hexdigest()[:16]
print("round1:", round1)
print("sha256:", h1, "(round2:", h2, ")")
open(f"{out}/QFREEZE.txt", "w").write(
    f"round1 {h1} {round1}\nround2 {h2} {round2}\n"
    f"stride={stride} excluded={len(used)}\n")
