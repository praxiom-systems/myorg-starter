#!/bin/bash
# Claude Code PreToolUse hook for Edit/Write/MultiEdit band-aid detection.
# Scans added lines for band-aid patterns (bare excepts, lint/type suppressions,
# skipped tests, unlinked TODOs) against band-aid-patterns.toml.
# A match is a note to the agent; only at the strict guard level do the
# high-confidence patterns stop the edit. Also stops edits that change the guard
# level itself (only the user's /myorg:guards does that).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/band-aid-patterns.toml"

# shellcheck source=hook-lib.sh
. "$SCRIPT_DIR/hook-lib.sh"
hooklib_plugin_guard "$SCRIPT_DIR"

hooklib_init_log_dir
hooklib_cleanup_old_logs

# Missing config = the gate's own misconfiguration; fail OPEN (exit 0, abstain)
# rather than blocking every edit. A deliberate deny still blocks via the JSON
# decision emitted on the exit-0 path below. (#958)
if [[ ! -f "$CONFIG_FILE" ]]; then
    if [[ -n "$HOOKLIB_LOG_DIR" ]]; then
        ERROR_LOG=$(hooklib_log_file_for "error")
        hooklib_log_event "$ERROR_LOG" "ConfigMissing" "validate-edit" "$CONFIG_FILE (failing open)"
    fi
    echo "validate-edit: config not found, failing open: $CONFIG_FILE" >&2
    exit 0
fi

INPUT=$(cat)

CWD=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('cwd', ''))
except Exception:
    print('')
" 2>/dev/null)
PROJECT=$(hooklib_project_from_cwd "$CWD")
LOG_FILE=$(hooklib_log_file_for "$PROJECT")

STDERR_FILE=$(mktemp)
OUTPUT=$(echo "$INPUT" | python3 "$SCRIPT_DIR/validate-edit.py" "$CONFIG_FILE" 2>"$STDERR_FILE") || {
    TOOL=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_name','<unknown>'))" 2>/dev/null || echo "<parse error>")
    STDERR_CONTENT=$(cat "$STDERR_FILE" 2>/dev/null || true)
    rm -f "$STDERR_FILE"
    if [[ -n "$LOG_FILE" ]]; then
        hooklib_log_event "$LOG_FILE" "PythonError" "$TOOL" "failing open; stdout=$OUTPUT stderr=$STDERR_CONTENT"
    fi
    # Validator crashed (not a deliberate deny) — fail OPEN so a broken Python
    # env cannot brick every edit for an unattended agent. (#958)
    exit 0
}
rm -f "$STDERR_FILE"

if [[ -n "$LOG_FILE" && -n "$OUTPUT" ]]; then
    if echo "$OUTPUT" | grep -q '"permissionDecision": *"deny"'; then
        TOOL=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_name',''))" 2>/dev/null)
        REASON=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('hookSpecificOutput',{}).get('permissionDecisionReason',''))" 2>/dev/null)
        hooklib_log_event "$LOG_FILE" "EditDeny" "$TOOL" "$REASON"
    elif echo "$OUTPUT" | grep -q '"additionalContext"'; then
        TOOL=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_name',''))" 2>/dev/null)
        REASON=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('hookSpecificOutput',{}).get('additionalContext',''))" 2>/dev/null)
        hooklib_log_event "$LOG_FILE" "EditAdvise" "$TOOL" "$REASON"
    fi
fi

if [[ -n "$OUTPUT" ]]; then
    printf '%s' "$OUTPUT"
fi
