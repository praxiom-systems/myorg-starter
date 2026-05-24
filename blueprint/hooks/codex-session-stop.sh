#!/bin/bash
# Codex Stop adapter. Runs the shared session-stop hook, then emits either the
# advisory JSON it returned or valid empty JSON for Codex's Stop event.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INPUT=$(cat)
OUTPUT=$(printf '%s' "$INPUT" | "$SCRIPT_DIR/session-stop.sh" 2>/dev/null || true)
if [[ -n "$OUTPUT" ]]; then
    printf '%s' "$OUTPUT"
else
    printf '{}'
fi
