# shellcheck shell=bash
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
#   hooklib_find_getmyorg          — echo the getmyorg binary path or "".
#   hooklib_plugin_guard <dir>     — for the copy shipped in the myorg plugin:
#                                    turn off the shadow log, and exit 0 when
#                                    the repo runs its own copies
#                                    (MYORG_HOOKS_SOURCE=project).
#   hooklib_orchestrate_semantic_refresh <repo_root>
#                                  — detached, debounced semantic-refresh
#                                    queue; never commits/writes/models.
#   hooklib_orchestrate_docs_reconcile <repo_root>
#                                  — detached, debounced authored-doc
#                                    freshness-sidecar refresh; never
#                                    commits/writes-worktree/writes-db.
#   hooklib_emit_semantic_refresh_nudge <repo_root>
#                                  — foreground, DB-read-only nudge to
#                                    stdout when the semantic map is stale
#                                    relative to HEAD; silent otherwise.
#                                    Never auto-runs a refresh.
#
# Environment:
#   MYORG_HOOK_LOG_DIR             — log directory; empty disables logging.
#                                    Default: /tmp/claude-hook-logs
#   MYORG_HOOK_LOG_RETENTION_DAYS  — how long to keep logs. Default: 15
#   MYORG_HOOK_LOG_CLEANUP_SECONDS — minimum seconds between cleanup runs.
#                                    Default: 86400 (one day)
#   MYORG_HOOKS_SOURCE=project     — this repo registers its own hook copies
#                                    (getmyorg's dev checkout, via .envrc), so
#                                    the plugin's copies step aside.
#
# Originally adapted from amulya-labs/ai-dev-foundry (MIT). Modifications © praxiom-systems.

HOOKLIB_LOG_DIR_DEFAULT="/tmp/claude-hook-logs"
HOOKLIB_LOG_RETENTION_DAYS_DEFAULT=15
HOOKLIB_LOG_CLEANUP_SECONDS_DEFAULT=$((24 * 60 * 60))

hooklib_plugin_guard() {
    # The plugin's copies live in <plugin>/hooks/, next to .claude-plugin/.
    local dir="$1"
    [[ -f "$dir/../.claude-plugin/plugin.json" ]] || return 0
    export MYORG_SHADOW_LOG=0
    if [[ "${MYORG_HOOKS_SOURCE:-}" == "project" ]]; then
        exit 0
    fi
}

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

# hooklib_find_getmyorg — echo the path to the getmyorg binary, or "".
#
# Deliberately a near-duplicate of the git-hook-side getmyorg resolver.
# Trigger hooks (.claude/hooks/, a flat allowlisted tree) and git hooks
# (.git/hooks/myorg/) are distinct delivery channels with no shared source
# path, so they each carry their own copy rather than reaching across the
# tree. Keep the resolution ORDER identical between the two; the deliberate
# omission of an implicit in-repo build-output fallback (a trust-boundary
# expansion) is shared.
hooklib_find_getmyorg() {
    if [ -n "${GETMYORG_DEV_BINARY:-}" ] && [ -x "${GETMYORG_DEV_BINARY:-}" ]; then
        echo "$GETMYORG_DEV_BINARY"
        return 0
    fi
    local bin
    bin="$(command -v getmyorg 2>/dev/null || true)"
    if [ -n "$bin" ]; then
        echo "$bin"
        return 0
    fi
    local candidate
    for candidate in "$HOME/.local/bin/getmyorg" "$HOME/bin/getmyorg" "/usr/local/bin/getmyorg"; do
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    echo ""
}

# hooklib_orchestrate_semantic_refresh <repo_root> — opportunistically queue
# the cumulative, debounced semantic-refresh scope for <repo_root> (issue
# #874, follow-up to #868).
#
# This adds NO orchestration/lineage logic (a #874 non-goal): it only
# re-invokes the existing `semantic-refresh orchestrate` CLI from the
# trigger layer. orchestrate is internally debounced — it short-circuits
# when the semantic DB is already at HEAD — so repeated calls at the same
# HEAD do not re-queue or thrash the pending marker. The call is detached
# and timeout-capped; it never runs a model, creates a commit, or writes
# the worktree. Non-blocking by contract: returns 0 on every path.
hooklib_orchestrate_semantic_refresh() {
    local target="${1:-}"
    [ -n "$target" ] || return 0
    [ -d "$target/.myorg" ] || return 0

    local bin
    bin="$(hooklib_find_getmyorg)"
    [ -n "$bin" ] || return 0

    local secs="${MYORG_MAP_REFRESH_TIMEOUT:-60}"
    [ "$secs" -gt 0 ] 2>/dev/null || secs=60

    (
        if command -v timeout >/dev/null 2>&1; then
            timeout "$secs" "$bin" semantic-refresh orchestrate --repo "$target" >/dev/null 2>&1
        elif command -v gtimeout >/dev/null 2>&1; then
            gtimeout "$secs" "$bin" semantic-refresh orchestrate --repo "$target" >/dev/null 2>&1
        else
            "$bin" semantic-refresh orchestrate --repo "$target" >/dev/null 2>&1
        fi
    ) </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
    return 0
}

# hooklib_emit_semantic_refresh_nudge <repo_root> — single-line nudge to
# stdout when the semantic map is stale relative to HEAD and the diff
# carries semantic impact. SessionStart hook calls this in the foreground
# so Claude Code captures the line as session context; pure read of the
# project-map DB, no model, no commit, no worktree write. Silent in three
# cases the operator does not need to act on (and so the line never adds
# noise to a healthy session):
#
#   - getmyorg is not on PATH (cannot evaluate)
#   - the nudge subcommand evaluates the map as not stale OR no semantic impact
#   - a transient failure (git, db) — short-circuit silent rather than wedge
#
# Non-blocking: returns 0 on every path. Timeout-capped so a slow git op
# does not stall session bring-up.
hooklib_emit_semantic_refresh_nudge() {
    local target="${1:-}"
    [ -n "$target" ] || return 0
    [ -d "$target/.myorg" ] || return 0

    local bin
    bin="$(hooklib_find_getmyorg)"
    [ -n "$bin" ] || return 0

    local secs="${MYORG_NUDGE_TIMEOUT:-10}"
    [ "$secs" -gt 0 ] 2>/dev/null || secs=10

    if command -v timeout >/dev/null 2>&1; then
        timeout "$secs" "$bin" semantic-refresh nudge --repo "$target" 2>/dev/null || true
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$secs" "$bin" semantic-refresh nudge --repo "$target" 2>/dev/null || true
    else
        "$bin" semantic-refresh nudge --repo "$target" 2>/dev/null || true
    fi
    return 0
}

# hooklib_orchestrate_docs_reconcile <repo_root> — opportunistically refresh
# the authored-doc freshness sidecar for <repo_root>, debounced.
#
# The docs-reconcile-substrate mirror of hooklib_orchestrate_semantic_refresh.
# `docs-reconcile orchestrate` is internally debounced: it reuses the same
# mapfreshness verdict the status line reads and short-circuits when
# authored_docs is already fresh against the current map version, so
# repeated calls at the same map state never re-scan. The call is detached
# and timeout-capped; it never runs a model, creates a commit, writes the
# worktree, or writes project-map.db — only the gitignored
# .docs-reconcile-state.json sidecar. Non-blocking: returns 0 on every path.
hooklib_orchestrate_docs_reconcile() {
    local target="${1:-}"
    [ -n "$target" ] || return 0
    [ -d "$target/.myorg" ] || return 0

    local bin
    bin="$(hooklib_find_getmyorg)"
    [ -n "$bin" ] || return 0

    local secs="${MYORG_MAP_REFRESH_TIMEOUT:-60}"
    [ "$secs" -gt 0 ] 2>/dev/null || secs=60

    (
        if command -v timeout >/dev/null 2>&1; then
            timeout "$secs" "$bin" docs-reconcile orchestrate --repo "$target" >/dev/null 2>&1
        elif command -v gtimeout >/dev/null 2>&1; then
            gtimeout "$secs" "$bin" docs-reconcile orchestrate --repo "$target" >/dev/null 2>&1
        else
            "$bin" docs-reconcile orchestrate --repo "$target" >/dev/null 2>&1
        fi
    ) </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
    return 0
}
