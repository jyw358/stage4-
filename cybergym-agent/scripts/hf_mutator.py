#!/usr/bin/env python3
"""In-container mini mutation loop for honggfuzz-style file-feeding targets
(the oss-fuzz base runner ships no honggfuzz binary). Bounded by wall seconds;
saves only sanitizer-fatal artifacts. Usage (inside container):
  python3 hf_mutator.py /out/target /corpus /artifacts SECONDS [dictfile]
"""
import hashlib
import os
import random
import re
import subprocess
import sys
import time

target, corp, arts, seconds = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
dictfile = sys.argv[5] if len(sys.argv) > 5 else None
rng = random.Random(0xC0FFEE)
FATAL = re.compile(rb"ERROR: (AddressSanitizer|UndefinedBehaviorSanitizer|MemorySanitizer)"
                   rb"|SUMMARY: (Address|Undefined|Memory)Sanitizer|deadly signal")
seeds = [os.path.join(corp, f) for f in os.listdir(corp)
         if f.startswith("seed") and os.path.isfile(os.path.join(corp, f))]
words = [bytes.fromhex(w) for w in re.findall(r'"([0-9a-fA-F]{2,16})"',
          open(dictfile, errors="replace").read())] if dictfile else []
if not seeds:
    print("no seeds"); sys.exit(2)
os.makedirs(arts, exist_ok=True)
deadline = time.time() + seconds
execs = crashes = 0
seen = set()
while time.time() < deadline:
    data = bytearray(rng.choice(seeds) and open(rng.choice(seeds), "rb").read() or b"")
    for _ in range(rng.randint(1, 8)):
        if not data:
            data += rng.randbytes(16)
            continue
        op = rng.randint(0, 5 if words else 4)
        i = rng.randrange(len(data))
        if op == 0:
            data[i] ^= 1 << rng.randrange(8)
        elif op == 1:
            data[i] = rng.randrange(256)
        elif op == 2:
            del data[i:i + rng.randint(1, 32)]
        elif op == 3:
            data[i:i] = rng.randbytes(rng.randint(1, 32))
        elif op == 4:
            data[i:i] = bytes([rng.choice(b"\x00\xff\x7f\x80")]) * rng.randint(1, 8)
        else:
            data[i:i] = rng.choice(words)
    path = "/tmp/cand"
    open(path, "wb").write(bytes(data))
    try:
        r = subprocess.run([target, path], capture_output=True, timeout=8,
                           env={**os.environ})
    except subprocess.TimeoutExpired:
        continue
    execs += 1
    blob = (r.stdout or b"") + (r.stderr or b"")
    if FATAL.search(blob):
        h = hashlib.sha1(bytes(data)).hexdigest()[:12]
        if h not in seen:
            seen.add(h)
            open(os.path.join(arts, f"crash-{h}"), "wb").write(bytes(data))
            open(os.path.join(arts, f"crash-{h}.log"), "wb").write(blob[-4000:])
            crashes += 1
print(f"mini-mutator: execs={execs} crashes={crashes} budget={seconds}s")
