#!/bin/bash
# UserPromptSubmit hook: when the user types `/myorg:config guards <level>`,
# `... gate on|off` or `... profile <profile>` (Codex: `$myorg:config ...`;
# the starter: `/myorg-starter:config ...`), set it before the agent sees the
# prompt. Only the user changes these; the other guards stop the agent from
# doing it (#1089, #1168, #1178). Every other prompt passes through untouched.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hook-lib.sh
. "$SCRIPT_DIR/hook-lib.sh"
hooklib_plugin_guard "$SCRIPT_DIR"

INPUT=$(cat)

# Cheap pre-filter: most prompts aren't this command, so skip Python for them.
case "$INPUT" in
    *myorg:config*|*myorg-starter:config*) ;;
    *) exit 0 ;;
esac

# Never block a prompt: a crash here just means the setting didn't change, so
# say that instead of letting the user think it did.
if ! OUTPUT=$(printf '%s' "$INPUT" | python3 "$SCRIPT_DIR/config-prompt.py" 2>/dev/null); then
    printf '%s\n' '{"systemMessage": "getmyorg: the config command couldn'"'"'t run, so the setting didn'"'"'t change. Check that python3 runs."}'
    exit 0
fi
if [[ -n "$OUTPUT" ]]; then
    printf '%s' "$OUTPUT"
fi
