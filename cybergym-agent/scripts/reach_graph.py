#!/usr/bin/env python3
"""reach_graph.py -- static reachability ranking from the harness entry (S2.2).

Feeds D1 (hypothesis generation): BFS the repo_map call graph from the fuzz
harness entry, then rank reachable functions by
    distance-from-entry (closer = more likely reachable)
  + in-degree (hub-ness)
  + description-token overlap (functions whose names share identifiers with
    description.txt jump to the top)

  python3 reach_graph.py [--repo DIR] [--top N] [--desc description.txt]

Prints ranked candidates with file:line and the entry->candidate call chain
(truncated). Deterministic; pair with repo_map.py build first.
"""
import argparse
import json
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_map import load, reverse_edges, ENTRY_RE, FUNC_RE


def find_entry_symbol(d, repo):
    """Best harness entry: fuzz-named + LLVMFuzzerTestOneInput, shallow paths,
    and files referenced by build.sh win."""
    build_txt = ""
    bp = Path(repo) / "build.sh"
    if bp.is_file():
        try:
            build_txt = bp.read_text(errors="replace")
        except Exception:
            pass
    best = None
    for f in d["entries"]:
        low = f.lower()
        base = Path(f).name
        p = Path(repo) / f
        try:
            text = p.read_text(errors="replace")[:400000].replace('extern "C"', "")
        except Exception:
            continue
        for m in re.finditer(r"\b(LLVMFuzzerTestOneInput|FuzzerTestOneInput|main)\s*\(", text):
            sym = m.group(1)
            score = 0.0
            if "fuzz" in low:
                score += 3
            if sym == "LLVMFuzzerTestOneInput":
                score += 2
            if base in build_txt:
                score += 4          # the build script compiles THIS harness
            score -= min(f.count("/"), 8) * 0.5   # deep contrib paths lose
            if best is None or score > best[0]:
                best = (score, sym, f)
    return (best[1], best[2]) if best else (None, None)


def bfs(d, entry_sym, max_depth=14):
    """Returns {sym: (depth, parent)} via caller->callee edges."""
    seen = {entry_sym: (0, None)}
    frontier = [entry_sym]
    for depth in range(1, max_depth + 1):
        nxt = []
        for s in frontier:
            for c in d["edges"].get(s, []):
                if c not in seen:
                    seen[c] = (depth, s)
                    nxt.append(c)
        frontier = nxt
        if not frontier:
            break
    return seen


def chain(seen, sym):
    out = []
    while sym is not None and sym in seen:
        out.append(sym)
        sym = seen[sym][1]
    return out[::-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="repo")
    ap.add_argument("--desc", default="description.txt")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--max-depth", type=int, default=14)
    a = ap.parse_args()

    d = load()
    rev = reverse_edges(d)
    entry_sym, entry_file = find_entry_symbol(d, a.repo)
    if not entry_sym:
        print("NO-ENTRY: no fuzz/main entry indexed; run repo_map.py build first "
              "or inspect manually")
        return 3
    print(f"entry: {entry_sym}  ({entry_file})")

    seen = bfs(d, entry_sym, a.max_depth)
    print(f"reachable functions within {a.max_depth} hops: {len(seen)}")

    toks = set()
    dp = Path(a.desc)
    if dp.is_file():
        for t in re.findall(r"[A-Za-z_]\w{3,}", dp.read_text(errors="replace")):
            if t.lower() not in {"the", "and", "for", "with", "when", "this",
                                 "that", "from", "into", "should", "value"}:
                toks.add(t)
    print(f"description tokens: {len(toks)}")

    scored = []
    for sym, (depth, _) in seen.items():
        if sym == entry_sym:
            continue
        indeg = len(rev.get(sym, []))
        lo = sym.lower()
        overlap = sum(1 for t in toks if t.lower() in lo)
        # also match tokens that appear as parts (snake_case splits)
        parts = set(re.split(r"[_]", lo))
        overlap += sum(1 for t in toks if t.lower() in parts)
        score = overlap * 10 - depth + min(indeg, 20) * 0.2
        scored.append((score, sym, depth, indeg, overlap))
    scored.sort(key=lambda x: (-x[0], x[1]))

    print(f"\ntop {a.top} candidate vulnerable functions "
          f"(score, depth, in-deg, desc-overlap):")
    for score, sym, depth, indeg, ov in scored[:a.top]:
        hit = d["defs"].get(sym, [{}])[0]
        print(f"  {score:7.1f}  d{depth:<2} in{indeg:<3} ov{ov}  {sym}"
              f"  {hit.get('file', '?')}:{hit.get('line', '?')}")
        if ov:
            print(f"          chain: {' -> '.join(chain(seen, sym)[:8])}")
    if toks:
        matched = {s for *_, s, _, _, ov in scored if ov}
        print(f"\ndescription-named functions among reachable: "
              f"{len(matched)} {sorted(matched)[:12]}")


if __name__ == "__main__":
    sys.exit(main())
