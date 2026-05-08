#!/bin/bash
# Codex Stop adapter. Runs the shared observational session-stop hook, then
# emits valid empty JSON for Codex's Stop event.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$SCRIPT_DIR/session-stop.sh" >/dev/null 2>/dev/null || true
printf '{}'
