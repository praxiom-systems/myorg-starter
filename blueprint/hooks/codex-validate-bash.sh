#!/bin/bash
# Codex PreToolUse adapter for Bash command validation.
#
# Codex supports blocking via permissionDecision=deny. Per #923, the
# upstream validator never emits the old "ask" verdict — it translates
# matched [reflect.*] patterns and no-match-at-strict/balanced into
# `deny` + reflection prompt itself. So this adapter only has to handle
# `allow` and `deny` raw decisions; no host-specific fallback is needed.

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
PY
