#!/bin/bash
# Claude Code PostToolUse and PostToolUseFailure hook for Bash (#1195).
#
# When gate mode asked the user about a command, the guard left a pending
# marker named after the tool call (pylib/hooks/guard_events.py). The command
# ran, failed or not, so the user approved: record that next to the guard's
# row. Declines come from the guard's next call, which reads the transcript.
#
# Runs after every Bash call, so it stays in shell and leaves at once when no
# prompt is waiting. Never prints anything and always exits 0.

[[ "${MYORG_GUARD_EVENTS:-}" == "0" ]] && exit 0

# Same directory as guard_events.state_dir().
dir="${XDG_STATE_HOME:-$HOME/.local/state}/getmyorg"
pending="$dir/guard-pending"
[[ -d "$pending" ]] || exit 0
[[ -n "$(ls -A "$pending" 2>/dev/null)" ]] || exit 0

id=$(cat | grep -o '"tool_use_id" *: *"[A-Za-z0-9_-]*"' | head -n 1 | sed 's/.*"\([A-Za-z0-9_-]*\)"$/\1/')
[[ -n "$id" && -e "$pending/$id" ]] || exit 0
rm -f "$pending/$id" 2>/dev/null

# Seconds only (BSD date has no %N), rounded up: a row is never dated before
# it was written, so a send that already read the record can't skip it.
ts="$(( $(date +%s) + 1 ))000"
printf '{"v":1,"ts":%s,"id":"%s","outcome":"ran"}\n' "$ts" "$id" >>"$dir/guard-events.jsonl" 2>/dev/null
exit 0
