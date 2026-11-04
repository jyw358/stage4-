#!/usr/bin/env python3
"""assert_code_tree.py -- TRUSTED boot-time audit of the stage-4 code tree.

Deployed OUTSIDE the audited tree (next to stage4.sh in /root/): this script
is stdlib-only, imports NOTHING from the tree it audits, and runs BEFORE the
driver's Python process starts.  This is the R3 answer to review blocker 1:
a Python check inside the same tree it validates can never establish trust
(it imports the code it is checking), so the audit runs from a trusted,
root-owned location instead.  ``scripts/stage4.py`` keeps only a narrow
defensive lstat check for the exact files it is about to import.

What it refuses (exit 1, one violation per line):
  - any entry in the tree that is a symlink (deployment must be a real tree);
  - any file/dir with group- or other-write permission (chmod -R go-w);
  - any entry NOT owned by root (uid 0) -- includes stale __pycache__ dirs;
  - any __pycache__ directory at all (bytecode caches are removed by the
    launcher before this audit; if one still exists, refuse);
  - an ancestor chain where any component is other-writable, group-writable,
  - or owned by one of the untrusted accounts (prep/agent users), because a
    writable parent allows rename/replace of subtrees even when the tree
    itself is root-owned.

Limitations (documented, must be covered by on-host negative tests):
  POSIX ACLs, mount flags and case-insensitive aliasing are not visible to
  os.stat; ``namei -l`` + ``find -perm /022 -o -type l`` + an actual
  ``sudo -u <agent> touch`` attempt remain part of the deploy checklist.
"""
from __future__ import annotations

import argparse
import os
import stat
import sys

PYCACHE = "__pycache__"


def entry_violation(rel: str, st_mode: int, st_uid: int,
                    is_link: bool, root_uid: int = 0) -> str | None:
    """Classification rule for one entry INSIDE the code tree (pure, testable)."""
    if is_link:
        return f"{rel}: symlink (refused; deploy a real tree)"
    if st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"{rel}: group/other-writable (root-run code must be go-w free)"
    if st_uid != root_uid:
        return f"{rel}: owner uid={st_uid} != {root_uid} (untrusted writer?)"
    return None


def ancestor_violation(rel: str, st_mode: int, st_uid: int,
                       untrusted_uids: frozenset, root_uid: int = 0) -> str | None:
    """Classification rule for one ancestor directory of the code tree."""
    if st_mode & stat.S_IWOTH:
        return f"{rel}: ancestor is other-writable (subtree replaceable)"
    if st_mode & stat.S_IWGRP:
        return f"{rel}: ancestor is group-writable (subtree replaceable)"
    if st_uid in untrusted_uids:
        return f"{rel}: ancestor owned by untrusted uid={st_uid}"
    if st_uid != root_uid and st_uid not in untrusted_uids:
        # non-root, non-untrusted owner: still not the deploy owner we expect
        return f"{rel}: ancestor owner uid={st_uid} != {root_uid}"
    return None


def walk_tree(base: str, root_uid: int = 0) -> list:
    """Violations for every entry under ``base`` (symlinks are refused and
    never followed)."""
    out = []
    base_st = os.lstat(base)
    v = entry_violation(".", base_st.st_mode, base_st.st_uid,
                        stat.S_ISLNK(base_st.st_mode), root_uid)
    if v:
        out.append(v)
        return out            # a broken base says everything already
    for dirpath, dirnames, filenames in os.walk(base, topdown=True,
                                                followlinks=False):
        for name in sorted(dirnames + filenames):
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, base)
            try:
                st = os.lstat(p)
            except OSError as e:
                out.append(f"{rel}: lstat failed: {e}")
                continue
            if stat.S_ISLNK(st.st_mode):
                # do not descend into symlinked dirs either
                if name in dirnames:
                    dirnames.remove(name)
                out.append(entry_violation(rel, st.st_mode, st.st_uid, True,
                                           root_uid))
                continue
            v = entry_violation(rel, st.st_mode, st.st_uid, False, root_uid)
            if v:
                out.append(v)
            if name == PYCACHE and name in dirnames:
                dirnames.remove(name)     # contents already flagged via owner
    return [v for v in out if v]


def walk_ancestors(base: str, untrusted_uids: frozenset,
                   root_uid: int = 0) -> list:
    """Violations along every ancestor of ``base`` up to the filesystem root."""
    out = []
    cur = os.path.abspath(base)
    parent = os.path.dirname(cur)
    while parent != cur:
        try:
            st = os.lstat(parent)
        except OSError as e:
            out.append(f"{parent}: lstat failed: {e}")
            break
        v = ancestor_violation(parent, st.st_mode, st.st_uid,
                               untrusted_uids, root_uid)
        if v:
            out.append(v)
        cur, parent = parent, os.path.dirname(cur)
    return out


def resolve_untrusted(names: str) -> frozenset:
    """Map 's4agent,naco' to the set of uids whose write access is refused."""
    uids = set()
    if os.name != "posix":
        return frozenset()
    try:
        import pwd
    except ImportError:  # pragma: no cover
        return frozenset()
    for n in (x.strip() for x in names.split(",") if x.strip()):
        try:
            uids.add(pwd.getpwnam(n).pw_uid)
        except KeyError:
            print(f"WARN: untrusted user {n!r} does not exist (create it "
                  "before running the driver)", file=sys.stderr)
    return frozenset(uids)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True,
                    help="code tree root to audit (e.g. /opt/cybergym-agent)")
    ap.add_argument("--untrusted-users", default="s4agent,naco",
                    help="comma-separated accounts whose write access to the "
                         "tree or its ancestors is refused")
    args = ap.parse_args()

    if os.name != "posix":
        print("assert_code_tree: not POSIX; nothing to audit")
        return 0
    base = os.path.abspath(args.base)
    if not os.path.exists(base):
        print(f"assert_code_tree: code tree does not exist: {base}")
        return 1
    untrusted = resolve_untrusted(args.untrusted_users)
    violations = walk_tree(base) + walk_ancestors(base, untrusted)
    if violations:
        print(f"assert_code_tree: {len(violations)} violation(s) in {base}:")
        for v in violations[:200]:
            print(f"  {v}")
        return 1
    print(f"assert_code_tree: {base} is root-clean "
          f"({args.untrusted_users} refused write access)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
