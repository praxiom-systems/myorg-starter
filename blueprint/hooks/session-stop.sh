#!/bin/bash
# Claude Code Stop hook — canonical `session.stop` trigger.
#
# Fires when the assistant finishes responding to the user. Substrate for
# future advisory checklists (e.g. #379 final-answer/working-tree warning).
# This first version is intentionally observational: it logs the event and
# exits 0 so the session is never blocked. Do NOT add automatic test runs,
# retrospective enforcement, or any side effect that mutates user state.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=hook-lib.sh
. "$SCRIPT_DIR/hook-lib.sh"

hooklib_init_log_dir
[[ -z "$HOOKLIB_LOG_DIR" ]] && exit 0

# NOTE: ``python3 <<'EOF'`` (here-doc as stdin) was rejected — it replaces
# the pipe from the host with the script body, so json.load(sys.stdin) sees
# the script source instead of the host payload. Use the substituted-script
# form so Python's stdin remains the host's JSON payload.
CWD=$(python3 -c "$(cat <<'PYEOF'
import sys, json
try:
    print(json.load(sys.stdin).get('cwd') or '')
except Exception:
    print('')
PYEOF
)" 2>/dev/null)

PROJECT=$(hooklib_project_from_cwd "$CWD")
LOG_FILE=$(hooklib_log_file_for "$PROJECT")

if [[ -n "$LOG_FILE" ]]; then
    hooklib_log_event "$LOG_FILE" "SessionStop" "$PROJECT"
fi

exit 0
