#!/bin/bash
# Claude Code SessionEnd hook — canonical `session.end` trigger.
#
# Fires when the host exits a session (SIGINT, /exit, window close, etc.).
# Substrate for future end-of-session recap/checkpoint consumers (e.g. brief
# checkpoint for next-session resume). This first version logs the event and
# exits 0; do NOT add any blocking behavior or side effects that depend on
# session state — SessionEnd runs during teardown and a flaky hook degrades
# the host's exit experience.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=hook-lib.sh
. "$SCRIPT_DIR/hook-lib.sh"

hooklib_init_log_dir
[[ -z "$HOOKLIB_LOG_DIR" ]] && exit 0

# NOTE: ``python3 <<'EOF'`` (here-doc as stdin) was rejected — it replaces
# the pipe from the host with the script body, so json.load(sys.stdin) sees
# the script source instead of the host payload. Use the substituted-script
# form so Python's stdin remains the host's JSON payload.
PARSED=$(python3 -c "$(cat <<'PYEOF'
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('cwd') or '')
    print(data.get('reason') or '')
except Exception:
    print('')
    print('')
PYEOF
)" 2>/dev/null)

CWD=$(printf '%s\n' "$PARSED" | sed -n '1p')
REASON=$(printf '%s\n' "$PARSED" | sed -n '2p')

PROJECT=$(hooklib_project_from_cwd "$CWD")
LOG_FILE=$(hooklib_log_file_for "$PROJECT")

if [[ -n "$LOG_FILE" ]]; then
    SUBJECT="$PROJECT"
    [[ -n "$REASON" ]] && SUBJECT="$PROJECT (reason: $(hooklib_sanitize "$REASON"))"
    hooklib_log_event "$LOG_FILE" "SessionEnd" "$SUBJECT"
fi

exit 0
