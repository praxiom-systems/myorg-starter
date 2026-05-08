#!/bin/bash
# Codex PostToolUse adapter for apply_patch band-aid rescans.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/band-aid-patterns.toml"

INPUT=$(cat)
RAW=$(echo "$INPUT" | python3 "$SCRIPT_DIR/validate-postedit.py" "$CONFIG_FILE" --format raw 2>/dev/null || true)

python3 - "$RAW" <<'PY'
import json
import sys

try:
    payload = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    sys.exit(0)

reason = payload.get("reason", "")
if reason:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": reason,
        }
    }))
PY
