#!/usr/bin/env python3
"""mutate_seed.py -- constraint-oriented mutation of a seed file.

Instead of blind fuzzing, you decide WHICH bytes matter (length fields,
offsets, magic bytes, indices -- derived from reading the parser), and this
tool generates targeted mutants around exactly those fields.

Usage:
  python3 mutate_seed.py <seed_file> <spec>... [-n N] [-o outdir]

Spec:  offset[:length]:mode
  offset    int; negative = counted from END of file
  length    bytes to touch (default 1)
  mode      flip | inc | dec | zero | ff | bitflip | rand | set:HEXBYTES
              inc/dec treat the field as a big-endian integer and +-1 it
              set: writes exact bytes (e.g. set:00ff20) -- precise construction
  -n N      number of mutants for stochastic modes (default 8)
  -o dir    output dir (default mutants/)

Output: writes mutants/m-NNN.bin (or your dir) and prints one line per
mutant: path + what changed (offset old->new hex). Deterministic.

Examples:
  python3 mutate_seed.py poc.bin 2:4:inc -n 6          # sweep a length field
  python3 mutate_seed.py poc.bin 0:4:set:deadbeef      # patch a magic header
  python3 mutate_seed.py poc.bin -3:1:bitflip -n 16    # wiggle the last byte
"""
import hashlib
import random
import sys
from pathlib import Path


def parse_int(s: str):
    return int(s, 0)  # accepts 0x.. / decimal / negative


def apply(data: bytearray, off: int, length: int, mode: str, rng) -> bytes:
    d = bytes(data)
    n = len(d)
    start = off if off >= 0 else n + off
    if start < 0 or start >= n:
        raise ValueError(f"offset {off} outside file (len={n})")
    end = min(start + max(length, 1), n)
    field = d[start:end]
    if mode == "flip":
        new = bytes(b ^ 0xFF for b in field)
    elif mode in ("inc", "dec"):
        delta = 1 if mode == "inc" else -1
        val = int.from_bytes(field, "big", signed=False)
        val = (val + delta) % (1 << (8 * len(field)))
        new = val.to_bytes(len(field), "big")
    elif mode == "zero":
        new = b"\x00" * len(field)
    elif mode == "ff":
        new = b"\xff" * len(field)
    elif mode == "bitflip":
        pos = rng.randrange(8 * len(field))
        new = bytearray(field)
        new[pos // 8] ^= 1 << (pos % 8)
        new = bytes(new)
    elif mode == "rand":
        new = bytes(rng.randrange(256) for _ in field)
    elif mode.startswith("set:"):
        hexs = mode[4:]
        raw = bytes.fromhex(hexs if len(hexs) % 2 == 0 else "0" + hexs)
        new = (raw * ((len(field) // len(raw)) + 1))[:len(field)]
    else:
        raise ValueError(f"unknown mode {mode!r}")
    return d[:start] + new + d[end:]


def main() -> int:
    # manual parse: argparse would swallow negative-offset specs ("-3:1:flip")
    argv = sys.argv[1:]
    seed_path, specs = None, []
    n_out, outdir = 8, "mutants"
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-n", "-o") and i + 1 < len(argv):
            if a == "-n":
                n_out = int(argv[i + 1], 0)
            else:
                outdir = argv[i + 1]
            i += 2
        elif seed_path is None:
            seed_path = a
            i += 1
        else:
            specs.append(a)
            i += 1
    if not seed_path or not specs:
        print(__doc__)
        return 2

    seed = Path(seed_path)
    if not seed.is_file():
        print(f"seed file not found: {seed}")
        return 2
    data = bytearray(seed.read_bytes())

    parsed = []
    for s in specs:
        parts = s.split(":")
        if len(parts) < 2:
            print(f"bad spec {s!r} (want offset[:len]:mode)")
            return 2
        off = parse_int(parts[0])
        length = parse_int(parts[1]) if len(parts) >= 3 and parts[1] else 1
        mode = ":".join(parts[2:])  # re-join: set:HEXBYTES contains a colon
        parsed.append((off, length, mode))
    specs = parsed

    stochastic = any(m in ("bitflip", "rand") for _, _, m in specs)
    count = n_out if stochastic else 1

    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(int(hashlib.sha1(
        f"{seed.name}:{argv}".encode()).hexdigest()[:8], 16))

    print(f"seed={seed} ({len(data)} bytes) mutants={count}")
    for i in range(count):
        cur = bytearray(data)
        changes = []
        for off, length, mode in specs:
            before = bytes(cur)
            after = apply(cur, off, length, mode, rng)
            cur = bytearray(after)
            b = bytes(before[off if off >= 0 else len(before) + off:
                             (off if off >= 0 else len(before) + off) + length])
            changes.append(f"@{off}:{b.hex()}->{cur[off if off >= 0 else len(cur) + off: (off if off >= 0 else len(cur) + off) + length].hex()}")
        p = out / f"m-{i:03d}.bin"
        p.write_bytes(bytes(cur))
        print(f"  {p}  " + " ".join(changes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
