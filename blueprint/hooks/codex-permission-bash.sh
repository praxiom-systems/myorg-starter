#!/bin/bash
# Codex PermissionRequest adapter for Bash command validation.
#
# This participates in Codex's native approval prompt only when Codex was
# already going to ask. Deny-patterns still deny; allow-patterns can approve;
# ask-patterns decline to decide so the normal Codex prompt remains visible.

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
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "deny", "message": reason or "Blocked by myorg policy."},
        }
    }
    print(json.dumps(out))
elif decision == "allow":
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    print(json.dumps(out))
PY
