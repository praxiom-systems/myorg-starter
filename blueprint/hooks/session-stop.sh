#!/bin/bash
# Claude Code Stop hook — canonical `session.stop` trigger.
#
# Fires when the assistant finishes responding to the user. Logs the event and
# runs advisory-only completion guards. Do NOT add automatic test runs,
# retrospective enforcement, response mutation, or any side effect that blocks
# the host turn.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=hook-lib.sh
. "$SCRIPT_DIR/hook-lib.sh"

hooklib_init_log_dir

INPUT=$(cat)

# NOTE: ``python3 <<'EOF'`` (here-doc as stdin) was rejected — it replaces
# the pipe from the host with the script body, so json.load(sys.stdin) sees
# the script source instead of the host payload. Use the substituted-script
# form so Python's stdin remains the host's JSON payload.
CWD=$(printf '%s' "$INPUT" | python3 -c "$(cat <<'PYEOF'
import sys, json
try:
    print(json.load(sys.stdin).get('cwd') or '')
except Exception:
    print('')
PYEOF
)" 2>/dev/null)
[[ -z "$CWD" ]] && CWD="$PWD"

PROJECT=$(hooklib_project_from_cwd "$CWD")
LOG_FILE=$(hooklib_log_file_for "$PROJECT")

if [[ -n "$LOG_FILE" ]]; then
    hooklib_log_event "$LOG_FILE" "SessionStop" "$PROJECT"
fi

# Completion guards still run when hook logging is unavailable; the guard is
# advisory-only and uses repo-local dispatch state rather than the hook log.
GETMYORG=$(hooklib_find_getmyorg)
if [[ -n "$GETMYORG" && -n "$CWD" ]]; then
    REPO_ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || true)
    if [[ -n "$REPO_ROOT" && -d "$REPO_ROOT/.myorg" ]]; then
        WINDOW="${MYORG_COMPLETION_GUARD_WINDOW_SECONDS:-300}"
        printf '%s' "$INPUT" | (
            cd "$REPO_ROOT" &&
                "$GETMYORG" dispatch completion-guard \
                    --repo "$REPO_ROOT" \
                    --window-seconds "$WINDOW" 2>/dev/null
        ) || true
    fi
fi

exit 0
