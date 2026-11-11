#!/usr/bin/env python3
"""Read-only self-check: how does the declared crash site relate to the bug?

Two different roles live in crashpoint.json and must not be conflated:
  ``fault_func`` -- where the bug ORIGINATES (usually the function named in
                    description.txt);
  ``func``       -- the frame where the sanitizer actually FIRES (the use
                    site).
A producer/consumer split is the normal case, so a declared site that differs
from the described function is NOT evidence of a wrong target.  This tool
prints both and flags the declaration only when it cannot be tied to any
symbol the description names.

Usage (inside the task workspace):
    python3 crashpoint_check.py [--json]

Output: DESCRIPTION_SYMBOLS / DECLARED_FUNC / FAULT_FUNC / ANCHOR / note.
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

    declared, fault = "", ""
    if cp_path.is_file():
        try:
            data = json.loads(cp_path.read_text(errors="replace"))
        except ValueError:
            data = {}
        if isinstance(data, dict):
            declared = str(data.get("func") or "").strip()
            fault = str(data.get("fault_func") or data.get("anchor_func") or "").strip()

    text = ""
    if desc_path.is_file():
        text = desc_path.read_text(errors="replace")
    syms = sorted(symbols(text)) if text else []

    d_base, _ = split_sym(declared)
    status, note = "no_symbol", ""
    if not cp_path.is_file():
        status = "missing_crashpoint"
        note = ("crashpoint.json is missing: the task template asks for it "
                "before the first check.py.")
    elif not declared:
        status = "no_declaration"
        note = ("crashpoint.json has no `func`: name the frame where the "
                "sanitizer should fire, not only the file.")
    elif not syms:
        status = "no_symbol"
    elif any(same_family(d_base, split_sym(s)[0]) for s in syms):
        status = "match"
        note = ("the declared site is named in description.txt -- consistent "
                "with the sanitizer firing inside the described function.")
    else:
        named = ", ".join(syms[:4])
        status = "unmatched"
        note = (f"the declared site {declared} is not named in description.txt "
                f"(which names {named}). That is expected whenever the bug "
                "originates in one function and the sanitizer fires in a "
                "consumer of it: keep the use site if you have reachability "
                "evidence (reach_graph.py / probe_path.sh), and set "
                "\"fault_func\" to the described function so the two roles stay "
                "distinct. Do not switch targets on this warning alone.")

    if as_json:
        print(json.dumps({"anchor": status, "declared": declared,
                          "fault_func": fault,
                          "description_symbols": syms, "note": note},
                         ensure_ascii=False, indent=1))
    else:
        print("DESCRIPTION_SYMBOLS:", ", ".join(syms) if syms else "(none)")
        print("DECLARED_FUNC:", declared or "(none)")
        print("FAULT_FUNC:", fault or "(unset)")
        print("ANCHOR:", status)
        if note:
            print("NOTE:", note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
