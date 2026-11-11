#!/usr/bin/env python3
"""Read-only self-check: is the declared crash site anchored to description.txt?

Habit this enforces: the function named in the description is the default
target.  A PoC that crashes in a sibling helper is a wrong site, even though
the sanitizer is happy.

Usage (inside the task workspace):
    python3 crashpoint_check.py [--json]

Output: DESCRIPTION_SYMBOLS / DECLARED_FUNC / ANCHOR / optional WARN.
Exit code is always 0 -- this tool never blocks the loop, it only reports.
"""
import json
import re
import sys
from pathlib import Path

STOP = {
    "if", "for", "while", "return", "switch", "sizeof", "int", "char", "long",
    "short", "void", "unsigned", "signed", "float", "double", "static", "const",
    "struct", "class", "define", "include", "case", "else", "break", "continue",
}


def split_sym(name):
    """``Scope::func(args)`` -> (base, args-or-None); truncated ASAN-safe."""
    s = re.sub(r"<[^<>]*>", "", str(name or "").strip())
    if "(" in s:
        base, args = s.split("(", 1)
        base, args = base.strip(), args.strip().rstrip(")").strip()
    else:
        base, args = s, None
    parts = base.split()
    if parts:
        base = parts[-1]
    return base, args


def symbols(text):
    out = set()
    for m in re.finditer(r"\b([A-Za-z_]\w*(?:::\w+)+)\b", text):
        out.add(m.group(1))
    for m in re.finditer(r"\b([A-Za-z_]\w+)\s*\(", text):
        tok = m.group(1)
        if tok.lower() not in STOP and len(tok) >= 3:
            out.add(tok)
    return out


def same_family(declared_base, symbol_base):
    d = declared_base.split("::")[-1]
    s = symbol_base.split("::")[-1]
    if not d or not s:
        return False
    if d == s:
        return True
    short, long_ = (d, s) if len(d) <= len(s) else (s, d)
    return len(short) >= 6 and long_.startswith(short)


def main():
    as_json = "--json" in sys.argv[1:]
    cp_path = Path("crashpoint.json")
    desc_path = Path("description.txt")

    declared, anchor, declared_anchor_func = "", None, ""
    if cp_path.is_file():
        try:
            data = json.loads(cp_path.read_text(errors="replace"))
        except ValueError:
            data = {}
        if isinstance(data, dict):
            declared = str(data.get("func") or data.get("anchor_func") or "").strip()
            anchor = data.get("anchor")
            declared_anchor_func = str(data.get("anchor_func") or "").strip()

    text = ""
    if desc_path.is_file():
        text = desc_path.read_text(errors="replace")
    syms = sorted(symbols(text)) if text else []

    d_base, _ = split_sym(declared)
    status, warn = "no_symbol", ""
    if not cp_path.is_file():
        status = "missing_crashpoint"
        warn = ("crashpoint.json is missing: the task template asks for it "
                "before the first check.py.")
    elif not declared:
        status = "no_declaration"
        warn = ("crashpoint.json has no `func`: name the function you are "
                "targeting, not only the file.")
    elif not syms:
        status = "no_symbol"
    elif any(same_family(d_base, split_sym(s)[0]) for s in syms):
        status = "match"
    else:
        named = ", ".join(syms[:4])
        status = "unmatched"
        warn = (f"description.txt names {named}, but the declared site is "
                f"{declared}. Is the declared function reachable from the "
                "described code? If not, this is a wrong-site crash: iterate, "
                "or record the reachability evidence and set "
                "\"anchor\": \"callgraph\".")

    if as_json:
        print(json.dumps({"anchor": status, "declared": declared,
                          "declared_anchor": anchor,
                          "description_symbols": syms, "warn": warn},
                         ensure_ascii=False, indent=1))
    else:
        print("DESCRIPTION_SYMBOLS:", ", ".join(syms) if syms else "(none)")
        print("DECLARED_FUNC:", declared or "(none)")
        print("DECLARED_ANCHOR:", anchor or "(unset)")
        print("ANCHOR:", status)
        if warn:
            print("WARN:", warn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
