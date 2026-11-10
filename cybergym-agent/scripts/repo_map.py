#!/usr/bin/env python3
"""repo_map.py -- symbol & call-graph index for the task repo (Stage-2 S2.1).

Replaces linear grep with ONE build pass + fast structured queries.

  python3 repo_map.py build [--repo DIR]      # -> .repo-map.json (~seconds)
  python3 repo_map.py defs SYM [SYM...]       # where is it defined
  python3 repo_map.py callers SYM             # who calls it
  python3 repo_map.py callees SYM             # what it calls
  python3 repo_map.py entry                   # fuzz-harness entries detected
  python3 repo_map.py files [SUBSTR]          # indexed files

Pure stdlib, deterministic. C/C++-focused regex heuristics: function
definitions (brace-matched bodies), call sites inside bodies, typedefs,
structs, enums. Good enough to jump-start LOCATE; the agent still verifies
by reading the code.
"""
import json
import os
import re
import sys
from pathlib import Path

MAP = ".repo-map.json"
EXTS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
SKIP_DIRS = {".git", ".repo", "out", "build", "node_modules"}
MAX_FILES = 4000
MAX_BYTES = 3_000_000

FUNC_RE = re.compile(
    r"^[ \t]*(?:[A-Za-z_][\w:<>]*[&*\s]+)*([A-Za-z_]\w*)\s*\("
    r"[^;{}]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{", re.M)
BAD_PREFIX_RE = re.compile(r"^[ \t]*(?:return\b|else\b|delete\b|case\b|\}|//|/\*)")
CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
TYPEDEF_RE = re.compile(r"\b(?:typedef)\s+[^;]*?\b([A-Za-z_]\w*)\s*;", re.S)
STRUCT_RE = re.compile(r"\b(?:struct|enum|union)\s+([A-Za-z_]\w*)")
ENTRY_RE = re.compile(r"\b(LLVMFuzzerTestOneInput|FuzzerTestOneInput|fuzz_target|main)\b")
KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "case", "else",
            "do", "catch", "throw", "assert"}


def brace_body(text, start):
    """Return (body, end) of the brace block starting at index `start` ('{')."""
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
        i += 1
    return text[start:], n


def index_file(path, rel, defs, edges, entries):
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return
    text = text.replace('extern "C"', "")
    if len(text) > MAX_BYTES:
        text = text[:MAX_BYTES]
    for m in FUNC_RE.finditer(text):
        name = m.group(1)
        if name in KEYWORDS or BAD_PREFIX_RE.match(text[m.start():m.end()]):
            continue
        bstart = text.find("{", m.end() - 1)
        if bstart < 0:
            continue
        body, _ = brace_body(text, bstart)
        line = text.count("\n", 0, m.start()) + 1
        defs.setdefault(name, []).append({"file": rel, "line": line, "kind": "func",
                                          "bytes": len(body)})
        calls = sorted({c.group(1) for c in CALL_RE.finditer(body)
                        if c.group(1) not in KEYWORDS and c.group(1) != name})
        if calls:
            edges.setdefault(name, []).extend(calls)
    for m in TYPEDEF_RE.finditer(text):
        defs.setdefault(m.group(1), []).append(
            {"file": rel, "line": text.count("\n", 0, m.start()) + 1, "kind": "typedef"})
    for m in STRUCT_RE.finditer(text):
        defs.setdefault(m.group(1), []).append(
            {"file": rel, "line": text.count("\n", 0, m.start()) + 1, "kind": "type"})
    if ENTRY_RE.search(text):
        entries.append(rel)


def build(repo):
    defs, edges, entries, files = {}, {}, [], {}
    n = 0
    for root, dirs, fs in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in fs:
            if Path(f).suffix.lower() not in EXTS:
                continue
            p = Path(root) / f
            rel = str(p.relative_to(repo))
            n += 1
            if n > MAX_FILES:
                continue
            files[rel] = p.stat().st_size
            index_file(p, rel, defs, edges, entries)
    for k in edges:
        edges[k] = sorted(set(edges[k]))
    data = {"repo": str(repo), "files": files, "defs": defs, "edges": edges,
            "entries": sorted(set(entries))}
    Path(MAP).write_text(json.dumps(data))
    print(f"indexed {n} files ({len(files)} kept): {len(defs)} symbols, "
          f"{len(edges)} caller nodes, entries: {data['entries'][:6]}")
    print(f"wrote {MAP}")


def load():
    return json.loads(Path(MAP).read_text())


def reverse_edges(d):
    r = {}
    for caller, cs in d["edges"].items():
        for c in cs:
            r.setdefault(c, []).append(caller)
    return r


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 2
    if a[0] == "build":
        repo = a[a.index("--repo") + 1] if "--repo" in a else "repo"
        build(Path(repo))
        return 0
    d = load()
    rev = reverse_edges(d)
    cmd = a[0]
    if cmd == "defs":
        for s in a[1:]:
            for hit in d["defs"].get(s, [])[:6]:
                print(f"{s}  {hit['kind']}  {hit['file']}:{hit['line']}")
            if s not in d["defs"]:
                print(f"{s}  NOT-FOUND")
    elif cmd == "callers":
        for s in a[1:]:
            cs = sorted(set(rev.get(s, [])))[:20]
            print(f"{s} <- {len(cs)} callers: {' '.join(cs)}")
    elif cmd == "callees":
        for s in a[1:]:
            cs = d["edges"].get(s, [])[:30]
            print(f"{s} -> {' '.join(cs) if cs else '(none indexed)'}")
    elif cmd == "entry":
        for e in d["entries"]:
            print("entry-file:", e)
    elif cmd == "files":
        sub = a[1] if len(a) > 1 else ""
        for f in list(d["files"])[:4000]:
            if sub in f:
                print(f)
    else:
        print(__doc__)
        return 2


if __name__ == "__main__":
    sys.exit(main())
