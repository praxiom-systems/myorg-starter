#!/bin/bash
# Codex PreToolUse adapter for Bash command validation.
#
# Codex currently supports blocking via permissionDecision=deny, but
# permissionDecision=ask fails open. Ask-pattern handling is split:
#   - PreToolUse emits an advisory systemMessage for ask decisions.
#   - PermissionRequest lets Codex's native approval prompt continue when
#     the host was already going to ask.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/bash-patterns.toml"

INPUT=$(cat)
RAW=$(echo "$INPUT" | python3 "$SCRIPT_DIR/validate-bash.py" "$CONFIG_FILE" --format raw 2>/dev/null || true)

python3 - "$RAW" <<'PY'
import json
import sys

try:
    payload = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    sys.exit(0)

decision = payload.get("decision")
reason = payload.get("reason", "")
if decision == "deny":
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
elif decision == "ask" and reason:
    print(json.dumps({"systemMessage": reason}))
PY
