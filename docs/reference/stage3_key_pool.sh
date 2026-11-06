#!/bin/bash
# key_pool.sh: pool affinity over primary/secondary/quaternary(key4).
# Tertiary excluded (owner-retired). Prefer the key with freshest week budget;
# probe-gated (429 never chosen); resets stale win anchors when probe=200.
ACTIVE_F=/data/cybergym/glm_active
probe() {
    local envf=$1
    set -a; . "$envf"; set +a
    curl -s -m 20 -o /dev/null -w "%{http_code}" "$GLM_BASE_URL/v1/messages" \
        -H "x-api-key: $GLM_API_KEY" -H "anthropic-version: 2023-06-01" \
        -H "content-type: application/json" \
        -d "{\"model\":\"glm-5.3\",\"max_tokens\":8,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"
}
Q=$(probe /home/naco/work/cybergym-agent/.glm4.env)
P=$(probe /home/naco/work/cybergym-agent/.glm2.env)
S=$(probe /home/naco/work/cybergym-agent/.glm.env)
Y=$(probe /home/naco/work/cybergym-agent/.glm5.env)
Z=$(probe /home/naco/work/cybergym-agent/.glm6.env)
CUR=$(cat "$ACTIVE_F" 2>/dev/null || echo unknown)
NEW=""
[ "$Z" = "200" ] && NEW=senary
[ -z "$NEW" ] && [ "$Q" = "200" ] && NEW=quaternary
[ -z "$NEW" ] && [ "$Y" = "200" ] && NEW=quinary
[ -z "$NEW" ] && [ "$P" = "200" ] && NEW=primary
[ -z "$NEW" ] && [ "$S" = "200" ] && NEW=secondary
if [ "$NEW" = "$CUR" ]; then exit 0; fi
if [ -n "$NEW" ] && [ "$NEW" != "$CUR" ]; then
    echo "$NEW" > "$ACTIVE_F"
    echo "$(date -u +%FT%TZ) pool switch $CUR -> $NEW (q=$Q y=$Y p=$P s=$S z=$Z)"
    python3 - << "PYEOF"
import json, time
a = json.load(open("/data/cybergym/quota_anchor.json"))
now = int(time.time())
k = open("/data/cybergym/glm_active").read().strip()
if k in a and a[k].get("win", 0) > 20000:
    a[k]["win"] = 0; a[k]["win_ts"] = now
    json.dump(a, open("/data/cybergym/quota_anchor.json", "w"))
PYEOF
fi
