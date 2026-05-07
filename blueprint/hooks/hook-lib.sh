# hook-lib.sh — shared bash helpers for getmyorg hooks.
#
# Source, don't execute. Provides:
#   hooklib_init_log_dir           — create a safe log dir; sets HOOKLIB_LOG_DIR
#                                    to its path or "" if logging is disabled.
#   hooklib_cleanup_old_logs       — throttled retention cleanup (no-op if disabled).
#   hooklib_project_from_cwd <cwd> — stable project label, worktree-aware.
#   hooklib_log_file_for <project> — full path to today's log file.
#   hooklib_sanitize <string>      — strip control chars for single-line logging.
#   hooklib_log_event <file> <event> <subject> [detail]
#                                  — append a structured block matching the
#                                    format agent-teams-common.py writes.
#
# Environment:
#   MYORG_HOOK_LOG_DIR             — log directory; empty disables logging.
#                                    Default: /tmp/claude-hook-logs
#   MYORG_HOOK_LOG_RETENTION_DAYS  — how long to keep logs. Default: 15
#   MYORG_HOOK_LOG_CLEANUP_SECONDS — minimum seconds between cleanup runs.
#                                    Default: 86400 (one day)
#
# Originally adapted from amulya-labs/ai-dev-foundry (MIT). Modifications © praxiom-systems.

HOOKLIB_LOG_DIR_DEFAULT="/tmp/claude-hook-logs"
HOOKLIB_LOG_RETENTION_DAYS_DEFAULT=15
HOOKLIB_LOG_CLEANUP_SECONDS_DEFAULT=$((24 * 60 * 60))

hooklib_init_log_dir() {
    # Respect explicit disable: MYORG_HOOK_LOG_DIR set to empty string.
    if [[ -n "${MYORG_HOOK_LOG_DIR+x}" && -z "$MYORG_HOOK_LOG_DIR" ]]; then
        HOOKLIB_LOG_DIR=""
        return 0
    fi

    local dir="${MYORG_HOOK_LOG_DIR:-$HOOKLIB_LOG_DIR_DEFAULT}"

    local old_umask
    old_umask=$(umask)
    umask 077
    mkdir -p "$dir" 2>/dev/null
    umask "$old_umask"

    # Safety: not a symlink, is a directory we own. Otherwise disable logging.
    if [[ -L "$dir" ]] || [[ ! -d "$dir" ]] || [[ ! -O "$dir" ]]; then
        HOOKLIB_LOG_DIR=""
        return 0
    fi
    HOOKLIB_LOG_DIR="$dir"
}

hooklib_cleanup_old_logs() {
    [[ -z "${HOOKLIB_LOG_DIR:-}" ]] && return 0
    local retention="${MYORG_HOOK_LOG_RETENTION_DAYS:-$HOOKLIB_LOG_RETENTION_DAYS_DEFAULT}"
    local interval="${MYORG_HOOK_LOG_CLEANUP_SECONDS:-$HOOKLIB_LOG_CLEANUP_SECONDS_DEFAULT}"
    local state="$HOOKLIB_LOG_DIR/.last_cleanup"
    local now last
    now=$(date +%s)
    if [[ -f "$state" ]]; then
        last=$(cat "$state" 2>/dev/null); last=${last:-0}
    else
        last=0
    fi
    if (( now - last < interval )); then
        return 0
    fi
    find "$HOOKLIB_LOG_DIR" -name "*.log" -type f -mtime +"$retention" -delete 2>/dev/null || true
    echo "$now" > "$state" 2>/dev/null || true
}

hooklib_project_from_cwd() {
    local cwd="$1"
    if [[ -z "$cwd" ]]; then
        printf 'unknown'
        return 0
    fi
    # Worktree paths like /path/to/project/.claude/worktrees/agent-abc123
    # become "project-agent-abc123" instead of just "agent-abc123".
    if [[ "$cwd" == *"/.claude/worktrees/"* ]]; then
        local before="${cwd%%/.claude/worktrees/*}"
        local after="${cwd#*/.claude/worktrees/}"
        local agent="${after%%/*}"
        printf '%s-%s' "$(basename "$before")" "$agent"
    else
        printf '%s' "$(basename "$cwd")"
    fi
}

hooklib_log_file_for() {
    [[ -z "${HOOKLIB_LOG_DIR:-}" ]] && return 0
    local project="${1:-unknown}"
    printf '%s/%s-%s.log' "$HOOKLIB_LOG_DIR" "$(LC_ALL=C date '+%Y-%m-%d-%a')" "$project"
}

hooklib_sanitize() {
    printf '%s' "$1" | tr '\n\r\t' ' ' | tr -d '\000-\011\013-\037'
}

# hooklib_log_event <file> <event> <subject> [detail]
# Appends a structured block compatible with agent-teams-common.py's format.
hooklib_log_event() {
    local file="$1" event="$2" subject="$3" detail="${4:-}"
    [[ -z "$file" ]] && return 0
    {
        echo "========================================"
        echo "TIME:   $(LC_ALL=C date '+%Y-%m-%d %H:%M:%S')"
        echo "EVENT:  $event"
        [[ -n "$subject" ]] && echo "SUBJECT: $(hooklib_sanitize "$subject")"
        [[ -n "$detail"  ]] && echo "DETAIL:  $(hooklib_sanitize "$detail")"
        echo "========================================"
    } >> "$file" 2>/dev/null || true
}
