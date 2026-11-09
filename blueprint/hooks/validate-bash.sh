#!/bin/bash
# Claude Code PreToolUse hook for Bash command validation
# Validates commands against patterns defined in bash-patterns.toml
# Handles command combinations (pipes, chains, subshells)
#
# Originally adapted from amulya-labs/ai-dev-foundry (MIT). Modifications © praxiom-systems.

# Record this tool invocation for the statusline stall-detector.
"$(dirname "$0")/touch-last-tool-call.sh" || true

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/bash-patterns.toml"

# Shared helpers (log dir init, cleanup, sanitize, structured logging).
# Respects MYORG_HOOK_LOG_DIR / MYORG_HOOK_LOG_RETENTION_DAYS env vars.
# shellcheck source=hook-lib.sh
. "$SCRIPT_DIR/hook-lib.sh"
hooklib_plugin_guard "$SCRIPT_DIR"

hooklib_init_log_dir
hooklib_cleanup_old_logs

# Check config file exists. If it is missing the gate cannot evaluate anything;
# fail OPEN (exit 0, abstain) rather than blocking every bash command on the
# hook's own misconfiguration. A deliberate deny still blocks via the JSON
# decision emitted on the exit-0 path below. (#958)
if [[ ! -f "$CONFIG_FILE" ]]; then
    if [[ -n "$HOOKLIB_LOG_DIR" ]]; then
        ERROR_LOG=$(hooklib_log_file_for "error")
        hooklib_log_event "$ERROR_LOG" "ConfigMissing" "validate-bash" "$CONFIG_FILE (failing open)"
    fi
    echo "validate-bash: config not found, failing open: $CONFIG_FILE" >&2
    exit 0
fi

# Capture stdin
INPUT=$(cat)

# Extract project name from cwd for log filename (worktree-aware — see hook-lib.sh)
CWD=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('cwd', ''))
except Exception:
    print('')
" 2>/dev/null)
PROJECT=$(hooklib_project_from_cwd "$CWD")
LOG_FILE=$(hooklib_log_file_for "$PROJECT")

# Use Python for TOML parsing and validation
# Capture stderr separately to avoid mixing with JSON output
STDERR_FILE=$(mktemp)
OUTPUT=$(echo "$INPUT" | python3 "$SCRIPT_DIR/validate-bash.py" "$CONFIG_FILE" 2>"$STDERR_FILE") || {
    COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command','<unknown>'))" 2>/dev/null || echo "<parse error>")
    STDERR_CONTENT=$(cat "$STDERR_FILE" 2>/dev/null || true)
    rm -f "$STDERR_FILE"
    if [[ -n "$LOG_FILE" ]]; then
        hooklib_log_event "$LOG_FILE" "PythonError" "$COMMAND" "failing open; stdout=$OUTPUT stderr=$STDERR_CONTENT"
    fi
    # The validator itself crashed (not a deliberate deny) — fail OPEN so a
    # broken Python env or transient parse error cannot brick every bash command
    # for an unattended agent. (#958)
    exit 0
}
rm -f "$STDERR_FILE"

# Only log ask/deny decisions (not allow) to reduce disk I/O
if [[ -n "$LOG_FILE" ]]; then
    if echo "$OUTPUT" | grep -q '"permissionDecision": *"deny"'; then
        COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null)
        REASON=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('hookSpecificOutput',{}).get('permissionDecisionReason',''))" 2>/dev/null)
        hooklib_log_event "$LOG_FILE" "BashDeny" "$COMMAND" "$REASON"
    elif echo "$OUTPUT" | grep -q '"permissionDecision": *"ask"'; then
        COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null)
        REASON=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('hookSpecificOutput',{}).get('permissionDecisionReason',''))" 2>/dev/null)
        hooklib_log_event "$LOG_FILE" "BashAsk" "$COMMAND" "$REASON"
    fi
fi

# Only output if non-empty (avoid spurious newlines)
if [[ -n "$OUTPUT" ]]; then
    printf '%s' "$OUTPUT"
fi
