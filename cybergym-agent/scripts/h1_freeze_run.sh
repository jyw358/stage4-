#!/bin/bash
set -u
OUTBASE=/data2/arms/h1
echo "=== H1 freeze $(date -u +%FT%TZ) ==="
python3 /tmp/h1_freeze.py || exit 1
grep -q "sha256:" /dev/null 2>/dev/null || true
sha256sum "$OUTBASE/q16frozen.json" | awk '{print "sha256:", substr($1,1,16)}'
