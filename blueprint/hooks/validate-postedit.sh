#!/bin/bash
# Claude Code PostToolUse hook for Edit/Write/MultiEdit output-verification probe.
#
# Runs two checks after an edit is applied:
#   1. Band-aid re-scan (synchronous, advisory) — shares the engine from
#      validate-edit.py via the scan_diff() public API.
#   2. Contract LLM judge (async fire-and-forget via getmyorg verify --probe contract)
#      — gated behind MYORG_VERIFY_LLM_JUDGE=1.
#
# PostToolUse hooks cannot block execution.  The hook always exits 0 and
# emits a PostToolUse hookSpecificOutput JSON payload.  Advisory reasons
# are surfaced in the transcript but do not stop the agent.

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
        hooklib_log_event "$ERROR_LOG" "ConfigMissing" "validate-postedit" "$CONFIG_FILE"
    fi
    # Non-fatal for PostToolUse — emit silent allow.
    echo '{"hookSpecificOutput":{"hookEventName":"PostToolUse","suppressOutput":false}}'
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
OUTPUT=$(echo "$INPUT" | python3 "$SCRIPT_DIR/validate-postedit.py" "$CONFIG_FILE" 2>"$STDERR_FILE") || {
    TOOL=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_name','<unknown>'))" 2>/dev/null || echo "<parse error>")
    STDERR_CONTENT=$(cat "$STDERR_FILE" 2>/dev/null || true)
    rm -f "$STDERR_FILE"
    if [[ -n "$LOG_FILE" ]]; then
        hooklib_log_event "$LOG_FILE" "PythonError" "$TOOL" "stdout=$OUTPUT stderr=$STDERR_CONTENT"
    fi
    # Non-fatal for PostToolUse.
    echo '{"hookSpecificOutput":{"hookEventName":"PostToolUse","suppressOutput":false}}'
    exit 0
}
rm -f "$STDERR_FILE"

if [[ -n "$LOG_FILE" && -n "$OUTPUT" ]]; then
    if echo "$OUTPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
r = d.get('hookSpecificOutput', {}).get('reason', '')
if r:
    print(r)
" 2>/dev/null | grep -q .; then
        TOOL=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_name',''))" 2>/dev/null)
        REASON=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('hookSpecificOutput',{}).get('reason',''))" 2>/dev/null)
        if echo "$REASON" | grep -q "Blocked\|band-aid"; then
            hooklib_log_event "$LOG_FILE" "PostEditAdvise" "$TOOL" "$REASON"
        fi
    fi
fi

if [[ -n "$OUTPUT" ]]; then
    printf '%s' "$OUTPUT"
else
    echo '{"hookSpecificOutput":{"hookEventName":"PostToolUse","suppressOutput":false}}'
fi
