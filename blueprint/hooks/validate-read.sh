#!/bin/bash
# Claude Code PreToolUse hook for Read content validation.
# Scans file content for prompt-injection signatures against
# injection-patterns.toml. Advisory-first: only high-confidence signatures
# block; medium-confidence signatures are surfaced as non-blocking warnings.

# Record this tool invocation for the statusline stall-detector.
"$(dirname "$0")/touch-last-tool-call.sh" || true

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/injection-patterns.toml"

# shellcheck source=hook-lib.sh
. "$SCRIPT_DIR/hook-lib.sh"

hooklib_init_log_dir
hooklib_cleanup_old_logs

# Missing config = the gate's own misconfiguration; fail OPEN (exit 0, abstain)
# rather than blocking every read. A deliberate deny still blocks via the JSON
# decision emitted on the exit-0 path below. (#958)
if [[ ! -f "$CONFIG_FILE" ]]; then
    if [[ -n "$HOOKLIB_LOG_DIR" ]]; then
        ERROR_LOG=$(hooklib_log_file_for "error")
        hooklib_log_event "$ERROR_LOG" "ConfigMissing" "validate-read" "$CONFIG_FILE (failing open)"
    fi
    echo "validate-read: config not found, failing open: $CONFIG_FILE" >&2
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
OUTPUT=$(echo "$INPUT" | python3 "$SCRIPT_DIR/validate-read.py" "$CONFIG_FILE" 2>"$STDERR_FILE") || {
    FILE=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('file_path','<unknown>'))" 2>/dev/null || echo "<parse error>")
    STDERR_CONTENT=$(cat "$STDERR_FILE" 2>/dev/null || true)
    rm -f "$STDERR_FILE"
    if [[ -n "$LOG_FILE" ]]; then
        hooklib_log_event "$LOG_FILE" "PythonError" "$FILE" "failing open; stdout=$OUTPUT stderr=$STDERR_CONTENT"
    fi
    # Validator crashed (not a deliberate deny) — fail OPEN so a broken Python
    # env cannot brick every read for an unattended agent. (#958)
    exit 0
}
rm -f "$STDERR_FILE"

if [[ -n "$LOG_FILE" && -n "$OUTPUT" ]]; then
    if echo "$OUTPUT" | grep -q '"permissionDecision": *"deny"'; then
        FILE=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('file_path',''))" 2>/dev/null)
        REASON=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('hookSpecificOutput',{}).get('permissionDecisionReason',''))" 2>/dev/null)
        hooklib_log_event "$LOG_FILE" "ReadDeny" "$FILE" "$REASON"
    elif echo "$OUTPUT" | grep -q 'ADVISORY'; then
        FILE=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('file_path',''))" 2>/dev/null)
        REASON=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('hookSpecificOutput',{}).get('permissionDecisionReason',''))" 2>/dev/null)
        hooklib_log_event "$LOG_FILE" "ReadAdvise" "$FILE" "$REASON"
    fi
fi

if [[ -n "$OUTPUT" ]]; then
    printf '%s' "$OUTPUT"
fi
