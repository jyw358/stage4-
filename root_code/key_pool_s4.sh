#!/bin/bash
# key_pool_s4.sh v2 -- GLM key pool selector for stage-4.
#
# Re-review fixes (PR#1 round 2):
#   P0-1 credential files are PARSED AS DATA, never sourced: whitelist
#       (GLM_API_KEY/GLM_BASE_URL/GLM_MODEL/AGCLI_CTX), optional "export "
#       prefix, optional paired quotes, value charset [A-Za-z0-9._:/+=-];
#       anything else refuses the file.
#   P0-2 every write step is checked; after switching, active.env is
#       re-verified against the chosen source file; failure is a loud rc=1.
#   P0-4 full trust checks BEFORE use: every pool file and active.env must be
#       a regular, non-symlink, root-owned file with NO group/other permission
#       bits, and every ancestor directory up to / must be root-owned and not
#       group/other-writable.
#   M5   probe results are cached in health.cache for S4_KEY_POOL_TTL seconds
#       (default 120) so per-task selection does not re-probe all keys each
#       launch; the flock still serializes selection itself.
#   M6   sticky only returns 0 after verifying active.env is byte-identical
#       to the marker's source file; otherwise it is atomically repaired
#       (and the repair is logged), or the selector fails.
#   M7   "keep last active when everything failed" is now authorized ONLY for
#       all-429 (rate-limited). Any non-429 failure (auth/transport/server) in
#       the pool with no healthy key => rc=1 => the wrapper REFUSEs the task.
#
# Rotation policy is stage-3's (deployed /root/key_pool.sh, sha256
# 309d1cfa41acdb4f4a07b3c42a0a1e0df5cf00db00deb9e9e14851f96c2954aa, vendored
# at docs/reference/stage3_key_pool.sh): priority
#   senary > quaternary > quinary > primary > secondary
# (tertiary owner-retired, excluded), probe-gated, sticky.
#
# Exit codes: 0 = active.env usable (selected / kept-and-verified / repaired)
#             1 = unusable (no healthy key and not all-429, or any trust,
#                 parse, or write failure)
set -u

POOL_DIR="${S4_KEY_POOL_DIR:-/etc/cybergym-agent/keys}"
ACTIVE_ENV="$POOL_DIR/active.env"
ACTIVE_MARK="$POOL_DIR/glm_active"
HEALTH_F="$POOL_DIR/health.cache"
LAST_SEL_F="$POOL_DIR/last_selection"
LOCK_F="/run/lock/s4-keypool.lock"
LOG_F="/var/log/s4-keypool.log"
PROBE_TIMEOUT="${S4_KEY_POOL_TIMEOUT:-20}"
HEALTH_TTL="${S4_KEY_POOL_TTL:-120}"

ORDER=(senary quaternary quinary primary secondary)
WHITELIST=" GLM_API_KEY GLM_BASE_URL GLM_MODEL AGCLI_CTX "

die() { echo "keypool: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root"

# r3-M5: operator-supplied knobs are validated BEFORE any probe -- an absurd
# TTL/timeout must refuse loudly, not stall task launches
case "$HEALTH_TTL" in ''|*[!0-9]*) die "S4_KEY_POOL_TTL must be a non-negative integer (got '$HEALTH_TTL')" ;; esac
[ "$HEALTH_TTL" -le 3600 ] || die "S4_KEY_POOL_TTL too large (max 3600, got $HEALTH_TTL)"
case "$PROBE_TIMEOUT" in ''|*[!0-9]*|0) die "S4_KEY_POOL_TIMEOUT must be a positive integer (got '$PROBE_TIMEOUT')" ;; esac
[ "$PROBE_TIMEOUT" -le 120 ] || die "S4_KEY_POOL_TIMEOUT too large (max 120, got $PROBE_TIMEOUT)"

# ---- trust checks (P0-4) -------------------------------------------------

mode_bits() { stat -c %a "$1" 2>/dev/null || echo "---"; }

trust_ancestors() {  # every existing ancestor up to /: root-owned, no go-write
    local p="$1" cur="" part
    local IFS='/'
    for part in $p; do
        cur="$cur/$part"
        [ -e "$cur" ] || continue
        [ "$(stat -c %u "$cur" 2>/dev/null)" = "0" ] || die "ancestor not root-owned: $cur"
        local m g o
        m="$(mode_bits "$cur")"; g=$(( ${m:?} / 10 % 10 )); o=$(( ${m:?} % 10 ))
        [ $(( g & 2 | o & 2 )) -eq 0 ] || die "ancestor group/other-writable: $cur ($m)"
    done
}

trust_dir() {  # root-owned dir, no group/other write, trusted ancestors
    local d="$1"
    [ -d "$d" ] || die "not a directory: $d"
    [ -L "$d" ] && die "symlink dir refused: $d"
    [ "$(stat -c %u "$d")" = "0" ] || die "dir not root-owned: $d"
    local m g o
    m="$(mode_bits "$d")"; g=$(( m / 10 % 10 )); o=$(( m % 10 ))
    [ $(( g & 2 | o & 2 )) -eq 0 ] || die "dir group/other-writable: $d ($m)"
    trust_ancestors "$d"
}

trust_file() {  # regular, non-symlink, root-owned, NO group/other bits at all
    local f="$1" what="${2:-file}"
    [ -f "$f" ] || die "$what missing: $f"
    [ -L "$f" ] && die "$what is a symlink, refused: $f"
    [ "$(stat -c %u "$f")" = "0" ] || die "$what not root-owned: $f"
    local m
    m="$(mode_bits "$f")"
    [ $(( (m / 10 % 10) | (m % 10) )) -eq 0 ] || die "$what has group/other permission bits: $f ($m)"
    trust_ancestors "$f"
}

# ---- strict data parser (P0-1) -------------------------------------------

# Prints "KEY<TAB>VALUE" lines for whitelisted keys; refuses anything else
# (extra keys, shell metacharacters, unparseable lines). NEVER evaluates.
parse_env_strict() {
    local f="$1" line key val first last
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        [ -z "$line" ] && continue
        case "$line" in '#'*) continue ;; esac
        case "$line" in 'export '*) line="${line#export }" ;; esac
        case "$line" in *=*) ;; *)
            echo "keypool: unparsable line in $f" >&2; return 1 ;;
        esac
        key="${line%%=*}"
        case "$WHITELIST" in *" $key "*) ;; *)
            echo "keypool: non-whitelisted key '$key' in $f" >&2; return 1 ;;
        esac
        val="${line#*=}"
        if [ "${#val}" -ge 2 ]; then
            first="${val:0:1}"; last="${val: -1}"
            if { [ "$first" = '"' ] && [ "$last" = '"' ]; } || \
               { [ "$first" = "'" ] && [ "$last" = "'" ]; }; then
                val="${val:1:${#val}-2}"
            fi
        fi
        # shellcheck disable=SC2076
        if ! [[ "$val" =~ ^[A-Za-z0-9._:/+=-]+$ ]]; then
            echo "keypool: unsafe value charset for $key in $f" >&2; return 1
        fi
        printf '%s\t%s\n' "$key" "$val"
    done < "$f"
}

# Loads a credential file into GLM_* globals (merge semantics: an empty value
# in the file never blanks an already-loaded value).
load_env_strict() {
    local f="$1" out k v
    out="$(parse_env_strict "$f")" || return 1
    while IFS=$'\t' read -r k v; do
        case "$k" in
            GLM_API_KEY)  GLM_API_KEY="$v" ;;
            GLM_BASE_URL) GLM_BASE_URL="$v" ;;
            GLM_MODEL)    GLM_MODEL="$v" ;;
            AGCLI_CTX)    AGCLI_CTX="$v" ;;
        esac
    done <<< "$out"
}

# ---- helpers -------------------------------------------------------------

log_line() {
    echo "$(date -u +%FT%TZ) $*" >> "$LOG_F" || die "cannot append keypool log $LOG_F"
}

atomic_write() {  # atomic_write <dest> <src-file>  (checked mktemp+chmod+mv, P0-2)
    local dest="$1" src="$2" tmp
    tmp="$(mktemp "$POOL_DIR/.tmp.XXXXXX")" || die "mktemp failed in $POOL_DIR"
    cat "$src" > "$tmp"              || { rm -f "$tmp"; die "copy to $tmp failed"; }
    chmod 600 "$tmp"                 || { rm -f "$tmp"; die "chmod $tmp failed"; }
    mv -f "$tmp" "$dest"             || { rm -f "$tmp"; die "mv $tmp -> $dest failed"; }
}

atomic_write_text() {  # atomic_write_text <dest> <text>  + read-back verify
    local dest="$1" text="$2" tmp
    tmp="$(mktemp "$POOL_DIR/.tmp.XXXXXX")" || die "mktemp failed in $POOL_DIR"
    printf '%s\n' "$text" > "$tmp"   || { rm -f "$tmp"; die "write $tmp failed"; }
    chmod 600 "$tmp"                 || { rm -f "$tmp"; die "chmod $tmp failed"; }
    mv -f "$tmp" "$dest"             || { rm -f "$tmp"; die "mv $tmp -> $dest failed"; }
    [ "$(cat "$dest" 2>/dev/null)" = "$text" ] || die "post-write verification failed for $dest"
}

file_fp() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }

probe_one() {  # probe_one <env-file> -> prints HTTP code
    local envf="$1" code
    GLM_API_KEY=""; GLM_BASE_URL=""; GLM_MODEL=""
    load_env_strict "$envf" || die "strict parse failed for $envf"
    [ -n "$GLM_API_KEY" ] && [ -n "$GLM_BASE_URL" ] || die "$envf lacks GLM_API_KEY/GLM_BASE_URL"
    code="$(curl -s -m "$PROBE_TIMEOUT" -o /dev/null -w "%{http_code}" \
        "$GLM_BASE_URL/v1/messages" \
        -H "x-api-key: $GLM_API_KEY" -H "anthropic-version: 2023-06-01" \
        -H "content-type: application/json" \
        -d '{"model":"'"$GLM_MODEL"'","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}' \
        2>/dev/null)" || code="000"
    echo "${code:-000}"
}

# ---- main ----------------------------------------------------------------

trust_dir "$POOL_DIR"
for k in "${ORDER[@]}"; do
    [ -f "$POOL_DIR/glm-$k.env" ] || continue
    trust_file "$POOL_DIR/glm-$k.env" "pool key file"
done

exec 9>"$LOCK_F" || die "cannot open $LOCK_F"
flock 9 || die "cannot flock $LOCK_F"

# stale mktemp leftovers from crashed runs (M6/P0-2 hygiene)
n_left="$(find "$POOL_DIR" -maxdepth 1 -name '.tmp.*' | wc -l)"
if [ "$n_left" -gt 0 ]; then
    find "$POOL_DIR" -maxdepth 1 -name '.tmp.*' -delete || die "cannot clean stale .tmp.* in $POOL_DIR"
    log_line "cleaned $n_left stale .tmp.* files"
fi

declare -A CODES=()
now="$(date +%s)"

# M5: reuse fresh cached probe results instead of re-probing every launch
# (S4_KEY_POOL_TTL=0 disables caching entirely -- tests and ops can force a
# probe with it).  The cache is READ through the same trust checks as every
# other pool file, AND (r3-P0) every cached code is bound to the sha256 of
# its key file at probe time -- rotating/replacing any glm-*.env within the
# TTL invalidates that entry and forces a fresh probe, so a stale "200" can
# never outlive the configuration it described.
declare -A FPS=()
cache_usable=0
# NB: a PRESENT cache (regular, empty, or symlink -- incl. dangling) must
# pass trust_file -- an untrusted cache REFUSES the run; only a genuinely
# absent cache counts as "no cache" (r3-P0/N1/N4; r4-P1a: -e unifies the
# "present means trusted" semantics with the marker check)
if [ "$HEALTH_TTL" -gt 0 ] && { [ -e "$HEALTH_F" ] || [ -L "$HEALTH_F" ]; }; then
    trust_file "$HEALTH_F" "health cache"
    c_ts="$(head -1 "$HEALTH_F" 2>/dev/null)"
    case "$c_ts" in ''|*[!0-9]*) c_ts=0 ;; esac
    if [ $(( now - c_ts )) -ge 0 ] && [ $(( now - c_ts )) -le "$HEALTH_TTL" ]; then
        # r4-P1b: strict record-shape validation -- exactly the five known
        # keys, no duplicates, code in {3-digit HTTP | no-file}, fingerprint
        # 64-hex (empty only for no-file); any anomaly invalidates the cache
        ok=1
        declare -A SEEN=()
        while read -r ck cc cf; do
            [ -n "${ck:-}" ] || { ok=0; break; }
            case " senary quaternary quinary primary secondary " in *" $ck "*) ;;
                *) ok=0; break ;; esac
            [ -z "${SEEN[$ck]:-}" ] || { ok=0; break; }
            SEEN[$ck]=1
            case "$cc" in
                no-file) [ -z "$cf" ] || { ok=0; break; } ;;
                [0-9][0-9][0-9])
                    if [ "${#cf}" != 64 ] || ! [[ "$cf" =~ ^[0-9a-f]+$ ]]; then
                        ok=0; break
                    fi ;;
                *) ok=0; break ;;
            esac
            CODES[$ck]="$cc"; FPS[$ck]="$cf"
        done < <(tail -n +2 "$HEALTH_F")
        if [ "$ok" = 1 ]; then
            for k in "${ORDER[@]}"; do
                [ -n "${CODES[$k]:-}" ] || { ok=0; break; }
                f="$POOL_DIR/glm-$k.env"
                if [ -f "$f" ]; then
                    [ "${FPS[$k]:-}" = "$(file_fp "$f")" ] || { ok=0; break; }
                else
                    [ "${CODES[$k]}" = "no-file" ] || { ok=0; break; }
                fi
            done
        fi
        [ "$ok" = 1 ] && cache_usable=1
    fi
fi

if [ "$cache_usable" != 1 ]; then
    for k in "${ORDER[@]}"; do
        [ -f "$POOL_DIR/glm-$k.env" ] || { CODES[$k]="no-file"; FPS[$k]=""; continue; }
        # NB: probe_one runs in a command substitution (subshell); its die()
        # only exits the subshell, so a parse failure MUST be caught here or
        # it would silently degrade to "unhealthy key" and keep rotating
        CODES[$k]="$(probe_one "$POOL_DIR/glm-$k.env")" \
            || die "strict parse failed for glm-$k.env"
        FPS[$k]="$(file_fp "$POOL_DIR/glm-$k.env")"
    done
    atomic_write "$HEALTH_F" <(printf '%s\n' "$now"; for k in "${ORDER[@]}"; do printf '%s %s %s\n' "$k" "${CODES[$k]}" "${FPS[$k]}"; done)
    chmod 600 "$HEALTH_F" || die "chmod health.cache failed"
fi

SUMMARY="$(for k in "${ORDER[@]}"; do printf '%s=%s ' "$k" "${CODES[$k]:-none}"; done)"

NEW=""
for k in "${ORDER[@]}"; do
    if [ -z "$NEW" ] && [ "${CODES[$k]:-}" = "200" ]; then NEW="$k"; fi
done

CUR="unknown"
# r3-M2/N4: a PRESENT marker (incl. symlink, dangling or not) must pass
# trust_file; an anomalous marker REFUSES instead of being silently ignored
# (and later overwritten) as if it did not exist
if [ -e "$ACTIVE_MARK" ] || [ -L "$ACTIVE_MARK" ]; then
    trust_file "$ACTIVE_MARK" "active marker"
    CUR="$(cat "$ACTIVE_MARK" 2>/dev/null || echo unknown)"
fi
case "$CUR" in senary|quaternary|quinary|primary|secondary) ;; *) CUR="unknown" ;; esac

if [ -n "$NEW" ]; then
    [ -f "$POOL_DIR/glm-$NEW.env" ] || die "chosen key $NEW has no source file"
    if [ "$NEW" = "$CUR" ] && [ -s "$ACTIVE_ENV" ] && \
       cmp -s "$ACTIVE_ENV" "$POOL_DIR/glm-$CUR.env" 2>/dev/null; then
        atomic_write_text "$LAST_SEL_F" "healthy:$NEW"   # r3-M4: never counted as healthy silently
        exit 0                       # sticky AND verified consistent (M6)
    fi
    # switch, marker/active repair, or missing active -- all converge on a
    # checked atomic write followed by verification (P0-2)
    why="switch $CUR -> $NEW"
    [ "$NEW" = "$CUR" ] && why="repair inconsistent active.env for $CUR"
    [ -s "$ACTIVE_ENV" ] || why="rebuild missing active.env for $NEW"
    atomic_write "$ACTIVE_ENV" "$POOL_DIR/glm-$NEW.env"
    atomic_write_text "$ACTIVE_MARK" "$NEW"     # r3-M2: atomic + read-back verified
    trust_file "$ACTIVE_ENV" "active key"
    cmp -s "$ACTIVE_ENV" "$POOL_DIR/glm-$NEW.env" || die "post-write verification failed for $ACTIVE_ENV"
    atomic_write_text "$LAST_SEL_F" "healthy:$NEW"
    log_line "$why ($SUMMARY)"
    exit 0
fi

# no healthy key (M7): keep-active is authorized ONLY for all-429
all_429=1
for k in "${ORDER[@]}"; do
    [ "${CODES[$k]:-}" = "429" ] || all_429=0
done
if [ "$all_429" = 1 ] && [ -s "$ACTIVE_ENV" ]; then
    trust_file "$ACTIVE_ENV" "active key"
    # r3-M1: with NO healthy key we may not repair anything -- the kept state
    # must already be internally consistent (marker -> source -> content) or
    # the run refuses rather than launch on an unprovable exception key
    case "$CUR" in
        senary|quaternary|quinary|primary|secondary) ;;
        *) die "all-429 keep: marker unreadable/invalid (CUR='$CUR'), refusing" ;;
    esac
    [ -f "$POOL_DIR/glm-$CUR.env" ] || die "all-429 keep: marker $CUR has no source file"
    cmp -s "$ACTIVE_ENV" "$POOL_DIR/glm-$CUR.env" || \
        die "all-429 keep: active.env does not match marker $CUR, refusing"
    atomic_write_text "$LAST_SEL_F" "throttled_keep:$CUR"
    log_line "all keys rate-limited (429), authorized keep of $CUR -- NOT probe-verified ($SUMMARY)"
    exit 0
fi

echo "keypool: no healthy key and not all-429 ($SUMMARY)" >&2
exit 1
