#!/bin/bash
# Claude Code PostToolUse hook for Bash commands
# Records that a command ran (for the status bar's guard-stop alert) and logs
# ASK command outcomes (approved)
#
# Originally adapted from amulya-labs/ai-dev-foundry (MIT). Modifications © praxiom-systems.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/bash-patterns.toml"

# Shared helpers (log dir init, sanitize, structured logging).
# Respects MYORG_HOOK_LOG_DIR / MYORG_HOOK_LOG_RETENTION_DAYS env vars.
# shellcheck source=hook-lib.sh
. "$SCRIPT_DIR/hook-lib.sh"

# Capture stdin
INPUT=$(cat)

# Parse input with Python
PARSED=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('cwd', ''))
    print(data.get('tool_input', {}).get('command', '').replace('\n', '\\\\n'))
except Exception:
    print('')
    print('')
" 2>/dev/null)

CWD=$(echo "$PARSED" | sed -n '1p')
COMMAND=$(echo "$PARSED" | sed -n '2p')

[[ -z "$COMMAND" ]] && exit 0

# The command ran, so the agent got past any guard stop: the status bar
# clears its "guard stopped the agent" alert once this marker is newer than
# the stop. Only repos set up with /myorg:init have a status bar to feed.
REPO_ROOT=$(git -C "${CWD:-.}" rev-parse --show-toplevel 2>/dev/null)
if [[ -n "$REPO_ROOT" && -d "$REPO_ROOT/.myorg" ]]; then
    mkdir -p "$REPO_ROOT/.myorg/statusline" 2>/dev/null && touch "$REPO_ROOT/.myorg/statusline/last-through" 2>/dev/null
fi

hooklib_init_log_dir
[[ -z "$HOOKLIB_LOG_DIR" ]] && exit 0

PROJECT=$(hooklib_project_from_cwd "$CWD")
LOG_FILE=$(hooklib_log_file_for "$PROJECT")

# Check if this was an ASK pattern using the raw (host-neutral) validator output.
DECISION=$(echo "$INPUT" | python3 "$SCRIPT_DIR/validate-bash.py" "$CONFIG_FILE" --format raw 2>/dev/null | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('decision', ''))
except Exception:
    print('')
" 2>/dev/null)

# Log ASK commands that were approved
if [[ "$DECISION" == "ask" && -n "$LOG_FILE" ]]; then
    hooklib_log_event "$LOG_FILE" "BashAskApproved" "$COMMAND"
fi

exit 0
