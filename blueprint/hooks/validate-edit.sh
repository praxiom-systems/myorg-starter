#!/bin/bash
# Claude Code PreToolUse hook for Edit/Write/MultiEdit band-aid detection.
# Scans added lines for band-aid patterns (bare excepts, lint/type suppressions,
# skipped tests, unlinked TODOs) against band-aid-patterns.toml.
# Advisory-first: only high-confidence patterns block; most emit a non-blocking
# advisory surfaced in logs and the transcript.

# Record this tool invocation for the statusline stall-detector.
"$(dirname "$0")/touch-last-tool-call.sh" || true

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/band-aid-patterns.toml"

# shellcheck source=hook-lib.sh
. "$SCRIPT_DIR/hook-lib.sh"

hooklib_init_log_dir
hooklib_cleanup_old_logs

if [[ ! -f "$CONFIG_FILE" ]]; then
    if [[ -n "$HOOKLIB_LOG_DIR" ]]; then
        ERROR_LOG=$(hooklib_log_file_for "error")
        hooklib_log_event "$ERROR_LOG" "ConfigMissing" "validate-edit" "$CONFIG_FILE"
    fi
    echo "Error: Configuration file not found: $CONFIG_FILE" >&2
    exit 1
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
        hooklib_log_event "$LOG_FILE" "PythonError" "$TOOL" "stdout=$OUTPUT stderr=$STDERR_CONTENT"
    fi
    exit 1
}
rm -f "$STDERR_FILE"

if [[ -n "$LOG_FILE" && -n "$OUTPUT" ]]; then
    if echo "$OUTPUT" | grep -q '"permissionDecision": *"deny"'; then
        TOOL=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_name',''))" 2>/dev/null)
        REASON=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('hookSpecificOutput',{}).get('permissionDecisionReason',''))" 2>/dev/null)
        hooklib_log_event "$LOG_FILE" "EditDeny" "$TOOL" "$REASON"
    elif echo "$OUTPUT" | grep -q 'ADVISORY'; then
        TOOL=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_name',''))" 2>/dev/null)
        REASON=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('hookSpecificOutput',{}).get('permissionDecisionReason',''))" 2>/dev/null)
        hooklib_log_event "$LOG_FILE" "EditAdvise" "$TOOL" "$REASON"
    fi
fi

if [[ -n "$OUTPUT" ]]; then
    printf '%s' "$OUTPUT"
fi
