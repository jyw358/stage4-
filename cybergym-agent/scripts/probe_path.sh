#!/bin/bash
# probe_path.sh <poc_file> <location>... -- gdb reachability probe.
#   location: function name | file:line | *address
# Runs your poc ONCE under gdb inside the scoring image against the LOCAL
# vulnerable binary. For each location prints REACHED (8-frame backtrace)
# or NOT-REACHED. Free, no submission, does not touch the check budget.
# Purpose: distinguish "input never reaches the vulnerable path" (fix the
# input structure / take another branch) from "reaches but doesn't crash"
# (the values are wrong -- mutate the fields).
set -u
[ $# -ge 2 ] || { echo "usage: probe_path.sh <poc_file> <location>..."; exit 2; }
TID="${CG_TASK_ID:?CG_TASK_ID not set}"
# PR-A (r9 P0-2): resolve local_oracle from THIS script's directory (the
# workspace) and drop the deployment-tree plugins.fuzz_local import --
# side_info() implements the identical target/ASAN parsing for BOTH arvo
# and oss-fuzz task kinds, keeping the workspace self-contained.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 - "$SCRIPT_DIR" "$TID" "$@" <<'PYEOF'
import pathlib, shutil, subprocess, sys, tempfile
sys.path.insert(0, sys.argv[1])
from local_oracle import side_info

GDB_RUNNER = "cybergym/gdb-runner:latest"  # stock runner lacks gdb

tid_full = sys.argv[2]
poc, locs = sys.argv[3], sys.argv[4:]
vi = side_info(tid_full, "vul")
if not vi:
    print("NO_LOCAL_BUILD: no prebuilt vul binaries for this task")
    sys.exit(4)
bin_dir, target, asan, _kind = vi
p = pathlib.Path(poc)
if not p.is_file():
    print(f"poc not found: {poc}"); sys.exit(2)
tmp = pathlib.Path(tempfile.mkdtemp(prefix="probe-"))
shutil.copyfile(p, tmp / "in0")

ex = ["set confirm off", "set pagination off"]
for loc in locs[:6]:
    ex += [f"break {loc}"]
ex += ["run /poc/in0"]
for i, loc in enumerate(locs[:6]):
    ex += [f"printf \"=== BP{i} {loc}: REACHED\\n\"", "bt 8", "continue"]

inner = (f"env LD_LIBRARY_PATH=/out-libs ASAN_OPTIONS={asan} "
         f"timeout -s SIGKILL 60 gdb -q -batch "
         + " ".join(f"-ex '{e}'" for e in ex)
         + f" /out/{target}")
cmd = ["docker", "run", "--rm",
       "-v", f"{bin_dir / 'out'}:/out:ro",
       "-v", f"{bin_dir / 'libs'}:/out-libs:ro",
       "-v", f"{tmp}:/poc:ro", GDB_RUNNER, "bash", "-c", inner]
try:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
except subprocess.TimeoutExpired:
    print("RESULT: TIMEOUT (gdb session hung)"); sys.exit(5)
out = (r.stdout or "") + (r.stderr or "")
if "gdb: command not found" in out or "gdb: not found" in out:
    print("NO_GDB: runner image lacks gdb -- fall back to try_local.sh")
    sys.exit(4)
print(out[-4000:])
reached = [loc for i, loc in enumerate(locs[:6]) if f"BP{i} {loc}: REACHED" in out]
missed = [loc for loc in locs[:6] if loc not in reached]
print("--- SUMMARY ---")
for loc in reached:
    print(f"REACHED    {loc}")
for loc in missed:
    print(f"NOT-REACHED {loc}  (breakpoint never hit: input does not take this path)")
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if reached else 1)
PYEOF
