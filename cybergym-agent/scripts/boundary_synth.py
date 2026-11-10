#!/usr/bin/env python3
"""boundary_synth.py -- boundary-value PoC synthesiser (Stage-1 S1.2).

Parses an ASan/MSan/UBSan report for allocation sizes / access offsets /
access sizes, locates where those numbers are encoded in the crashing input,
and emits N-1 / N / N+1 boundary variants automatically.

  python3 boundary_synth.py poc.bin -r report.txt [-o outdir] [--max 48]

Also accepts the report on stdin:  ./try_local.sh poc.bin 2>&1 | \\
    python3 boundary_synth.py poc.bin -r -

Deterministic, offline (no docker, no network). Output mutants go to
boundary/ (or -o dir) with a manifest of what was inferred and patched.
"""
import argparse
import re
import sys
from pathlib import Path

# ---- report parsing --------------------------------------------------------
PATTERNS = [
    # ASan: "0x... is located 12 bytes to the right of 100-byte region [...)"
    (re.compile(r"is located\s+(\d+)\s+bytes?\s+(?:to the right|to the left|after|before)\s+of\s+(\d+)-byte"), ("offset", "region")),
    # ASan: "0x... is located 0 bytes inside of 100-byte region"
    (re.compile(r"is located\s+(\d+)\s+bytes?\s+inside\s+of\s+(\d+)-byte"), ("offset", "region")),
    # ASan: "located 4 bytes to the right of 40-byte region" / "of size 40"
    (re.compile(r"(\d+)-byte region"), ("region",)),
    (re.compile(r"allocated\s+(\d+)\s+bytes"), ("alloc",)),
    (re.compile(r"[Rr]ead of size\s+(\d+)"), ("readsz",)),
    (re.compile(r"[Ww]rite of size\s+(\d+)"), ("writesz",)),
    # UBSan: "index 42 out of bounds for type 'int [40]'"
    (re.compile(r"index\s+(\d+)\s+out of bounds for type\s+'[^']*?\[(\d+)\]'"), ("offset", "region")),
    # MSan: "...was created by a heap allocation of size 64"
    (re.compile(r"allocation of size\s+(\d+)"), ("alloc",)),
]


def parse_report(text):
    got = {}
    for rx, keys in PATTERNS:
        for m in rx.finditer(text):
            for k, v in zip(keys, m.groups()):
                v = int(v)
                if v > 0xFFFFFFFF or (k not in got):
                    got.setdefault(k, v)
    return got


# ---- input patching --------------------------------------------------------
def encodings(v):
    """All widths in which v *could* be stored (big- and little-endian)."""
    out = []
    for w in (2, 3, 4, 8):
        if v < (1 << (8 * w)):
            be = v.to_bytes(w, "big")
            le = v.to_bytes(w, "little")
            out.append((w, "big", be))
            if le != be:
                out.append((w, "little", le))
    return out


def find_fields(data: bytes, values):
    """Locate (start, width, endian, value) for every encoded occurrence."""
    hits = []
    for label, v in values:
        for w, endian, pat in encodings(v):
            start = 0
            while True:
                i = data.find(pat, start)
                if i < 0:
                    break
                hits.append({"at": i, "width": w, "endian": endian,
                             "value": v, "label": label})
                start = i + 1
    # de-overlap: keep earliest, prefer wider fields on ties
    hits.sort(key=lambda h: (h["at"], -h["width"]))
    taken, clean = [], []
    for h in hits:
        if any(t <= h["at"] < t + w for t, w in taken):
            continue
        clean.append(h)
        taken.append((h["at"], h["width"]))
    return clean


def patch(data: bytes, at, width, endian, newval):
    d = bytearray(data)
    d[at:at + width] = int(newval).to_bytes(width, endian)
    return bytes(d)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("poc", help="crashing input file")
    ap.add_argument("-r", "--report", required=True,
                    help="sanitizer report file, or '-' for stdin")
    ap.add_argument("-o", "--outdir", default="boundary")
    ap.add_argument("--max", type=int, default=48, help="max variants")
    a = ap.parse_args()

    data = Path(a.poc).read_bytes()
    text = sys.stdin.read() if a.report == "-" else Path(a.report).read_text(errors="replace")
    vals = parse_report(text)
    if not vals:
        print("NO_VALUES: no sizes/offsets parsed from the report; "
              "feed the full try_local.sh / sanitizer output.")
        return 3

    boundaries = {}
    for k, v in vals.items():
        boundaries[k] = sorted({x for x in (v - 1, v, v + 1) if x >= 0})

    fields = find_fields(data, [(k, v) for k, v in vals.items()])
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    seen = set()
    for f in fields[:16]:
        for nv in boundaries[f["label"]]:
            if nv == f["value"]:
                continue
            key = (f["at"], f["width"], f["endian"], nv)
            if key in seen:
                continue
            seen.add(key)
            nb = patch(data, f["at"], f["width"], f["endian"], nv)
            name = out / f"b-{n:03d}-at{f['at']}-{f['label']}{f['value']}to{nv}.bin"
            name.write_bytes(nb)
            print(f"{name}  patch @{f['at']} w{f['width']} {f['endian']} "
                  f"{f['label']}={f['value']} -> {nv}")
            n += 1
            if n >= a.max:
                break
        if n >= a.max:
            break

    # file-length boundary: if any inferred value is close to len(data), the
    # size relationship itself may be the trigger -> emit truncation variants
    L = len(data)
    for k, v in vals.items():
        if abs(L - v) <= 4:
            for nv in (v - 1, v, v + 1):
                if 0 <= nv < L and nv != L:
                    nb = data[:nv]
                    name = out / f"b-{n:03d}-trunc-to-{nv}.bin"
                    name.write_bytes(nb)
                    print(f"{name}  truncate {L} -> {nv} ({k}={v})")
                    n += 1
            break

    print(f"SUMMARY: parsed={vals} fields={len(fields)} variants={n} -> {out}/")
    return 0 if n else 4


if __name__ == "__main__":
    sys.exit(main())
