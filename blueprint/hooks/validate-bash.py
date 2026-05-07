#!/usr/bin/env python3
# Originally adapted from amulya-labs/ai-dev-foundry (MIT). Modifications © praxiom-systems.
"""Bash command validator for Claude Code PreToolUse hook.

Thin entrypoint: parses CLI args, loads the TOML config (with OS overlay
and git-root injection), dispatches to ``bash_policy_evaluate`` for the
raw verdict, applies the optional policy overlay via
``bash_policy_decide``, writes the shadow-log row, and emits the
host-shaped JSON decision. The pattern matching, env-var resolution, and
shadow-log construction live in their respective ``bash_policy_*``
modules (issue #584 decomposition).
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bash_policy_decide import (
    apply_policy_gate,
    compute_would_action,
    resolve_default_action,
)
from bash_policy_evaluate import validate_command
from bash_policy_patterns import (
    compile_patterns,
    detect_os,
    inject_git_root_patterns,
    load_config,
    merge_os_config,
)
from bash_policy_shadow import build_shadow_row, write_shadow_row


def output_decision(decision: str, reason: str, fmt: str = "claude"):
    """Emit the validator decision in a host-specific shape.

    ``claude`` (default) emits Claude Code's hookSpecificOutput JSON.
    ``raw`` emits a host-neutral ``{"decision": ..., "reason": ...}`` payload
    that per-host adapter scripts can wrap into whatever JSON their runtime
    expects (Codex, Gemini CLI, OpenCode, …) without re-implementing the
    validator itself.
    """
    if fmt == "raw":
        print(json.dumps({"decision": decision, "reason": reason}))
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason
        }
    }))


_HELP_TEXT = """\
Usage: validate-bash.py <config.toml> [--format claude|raw] [--help]

Validates a bash command (read as JSON on stdin) against the deny / ask /
allow patterns in <config.toml> and emits a permission decision.

Environment variables:
  MYORG_BASH_DEFAULT=ask|allow
        Controls the fallback decision when a command segment matches no
        rule. Default is "allow" — deny+ask lists are the fence and
        anything that doesn't match either is silently approved. Set to
        "ask" to restore strict behavior (unknown commands require
        confirmation). Per-session kill switch — `export
        MYORG_BASH_DEFAULT=ask` restores strict behavior immediately.

  MYORG_HOOK_LOG_DIR=<path>
        Directory for hook logs, including the shadow log
        (<dir>/shadow.jsonl) which records what the validator *would*
        have decided under the opposite default for every invocation.
        Default: /tmp/claude-hook-logs. Empty string disables logging.

  MYORG_ACTIVE_AGENT, MYORG_ACTIVE_PROFILE
        Optional context labels included in shadow-log rows.
"""


def main():
    args = sys.argv[1:]
    fmt = "claude"
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(_HELP_TEXT)
            sys.exit(0)
        if a == "--format":
            if i + 1 >= len(args):
                print(_HELP_TEXT, file=sys.stderr)
                sys.exit(1)
            fmt = args[i + 1]
            i += 2
            continue
        if a.startswith("--format="):
            fmt = a.split("=", 1)[1]
            i += 1
            continue
        positional.append(a)
        i += 1

    if len(positional) != 1 or fmt not in ("claude", "raw"):
        print(_HELP_TEXT, file=sys.stderr)
        sys.exit(1)

    config_path = positional[0]
    config = load_config(config_path)

    # Load and merge OS-specific patterns (e.g. bash-patterns.linux.toml)
    os_suffix = detect_os()
    config_dir = os.path.dirname(config_path)
    config_base = os.path.splitext(os.path.basename(config_path))[0]
    os_config_path = os.path.join(config_dir, f"{config_base}.{os_suffix}.toml")
    if os.path.isfile(os_config_path):
        os_config = load_config(os_config_path)
        config = merge_os_config(config, os_config)

    # Read JSON input first — cwd is needed for dynamic pattern injection
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Invalid input, let it pass
        sys.exit(0)

    command = input_data.get("tool_input", {}).get("command", "")
    if not command:
        sys.exit(0)

    # If running inside a git repository, inject git-root-aware patterns before
    # compiling so that rm within the repo is auto-approved and .git is protected.
    cwd = input_data.get("cwd", "")
    if cwd:
        cwd = os.path.normpath(cwd)
        if os.path.exists(os.path.join(cwd, ".git")):
            inject_git_root_patterns(config, cwd)

    # Compile patterns (includes any git-root injections from above)
    deny_patterns = compile_patterns(config, "deny")
    ask_patterns = compile_patterns(config, "ask")
    allow_patterns = compile_patterns(config, "allow")

    default_action = resolve_default_action()
    decision, reason, matched_category, matched_rule = validate_command(
        command, deny_patterns, ask_patterns, allow_patterns, default_action
    )

    # For force-push patterns, apply the graduated-autonomy policy gate.
    # This may upgrade the decision from ask → deny (L3, no fresh evidence)
    # or downgrade to allow (L0/L1, auto-approved). Degrades gracefully
    # when the policy overlay is absent (OSS baseline: passthrough).
    decision, reason, matched_category, matched_rule = apply_policy_gate(
        decision, reason, matched_category, matched_rule, cwd
    )

    would_action = compute_would_action(decision, matched_category, default_action)
    write_shadow_row(
        build_shadow_row(
            command=command,
            cwd=cwd,
            input_data=input_data,
            actual_decision=decision,
            matched_category=matched_category,
            matched_rule=matched_rule,
            would_action=would_action,
            default_mode=default_action,
        )
    )

    output_decision(decision, reason, fmt=fmt)


if __name__ == "__main__":
    main()
