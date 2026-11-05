#!/bin/bash
# key_pool_s4.sh: stage-4 port of stage-3's GLM key pool (key_pool.sh).
#
# Same rotation policy as stage 3:
#   - 5 usable keys with a FIXED priority order
#       senary > quaternary > quinary > primary > secondary
#     (tertiary is owner-retired in stage 3 and stays excluded)
#   - probe-gated: a key that does not answer 200 is never chosen
#   - sticky: when the best key equals the current active, nothing is written
#   - when every key is throttled, the last active is KEPT (stage-3 behavior)
#
# stage-4 differences (trust model, not policy):
#   - pool files live root:root 0600 under /etc/cybergym-agent/keys/
#     (stage-3's /home/naco/work/cybergym-agent/.glm*.env are naco-writable
#     and therefore unacceptable as a formal stage-4 credential source)
#   - selection runs per task launch from run_agent_task.sh under flock,
#     instead of a cron loop
#   - active.env is written atomically (mktemp+mv in the same dir) and every
#     decision is appended to /var/log/s4-keypool.log for audit
#
# Exit codes: 0 = active.env usable (selected, kept, or already current)
#             1 = no key probed 200 AND no previous active.env (caller must
#                 refuse to spend model budget)
set -u
POOL_DIR="${S4_KEY_POOL_DIR:-/etc/cybergym-agent/keys}"
ACTIVE_ENV="$POOL_DIR/active.env"
ACTIVE_MARK="$POOL_DIR/glm_active"
LOCK_F="/run/lock/s4-keypool.lock"
LOG_F="/var/log/s4-keypool.log"
PROBE_TIMEOUT="${S4_KEY_POOL_TIMEOUT:-20}"

# stage-3 priority order (first wins among keys that probe 200)
ORDER=(senary quaternary quinary primary secondary)

[ -d "$POOL_DIR" ] || { echo "keypool: $POOL_DIR missing" >&2; exit 1; }

exec 9>"$LOCK_F" || exit 1
flock 9

probe() {
    local envf=$1 code
    set -a; . "$envf"; set +a
    code=$(curl -s -m "$PROBE_TIMEOUT" -o /dev/null -w "%{http_code}" \
        "$GLM_BASE_URL/v1/messages" \
        -H "x-api-key: $GLM_API_KEY" -H "anthropic-version: 2023-06-01" \
        -H "content-type: application/json" \
        -d '{"model":"glm-5.3","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}' \
        2>/dev/null)
    echo "${code:-000}"
}

declare -A CODES
NEW=""
for k in "${ORDER[@]}"; do
    f="$POOL_DIR/glm-$k.env"
    [ -f "$f" ] || { CODES[$k]="no-file"; continue; }
    CODES[$k]=$(probe "$f")
    if [ -z "$NEW" ] && [ "${CODES[$k]}" = "200" ]; then
        NEW="$k"
    fi
done
SUMMARY="$(for k in "${ORDER[@]}"; do printf '%s=%s ' "$k" "${CODES[$k]}"; done)"
CUR="$(cat "$ACTIVE_MARK" 2>/dev/null || echo unknown)"

if [ -n "$NEW" ] && [ "$NEW" != "$CUR" ]; then
    tmp="$(mktemp "$POOL_DIR/.active.XXXXXX")"
    cat "$POOL_DIR/glm-$NEW.env" > "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$ACTIVE_ENV"
    echo "$NEW" > "$ACTIVE_MARK"
    chmod 600 "$ACTIVE_MARK"
    echo "$(date -u +%FT%TZ) pool switch $CUR -> $NEW ($SUMMARY)" >> "$LOG_F"
    exit 0
fi

if [ -n "$NEW" ] && [ "$NEW" = "$CUR" ]; then
    exit 0                       # already current, sticky
fi

# every key throttled/failed: stage-3 behavior -- keep the last active
if [ -s "$ACTIVE_ENV" ]; then
    echo "$(date -u +%FT%TZ) all keys throttled ($SUMMARY), keeping $CUR" >> "$LOG_F"
    exit 0
fi

echo "keypool: no key probed 200 and no previous active.env ($SUMMARY)" >&2
exit 1
