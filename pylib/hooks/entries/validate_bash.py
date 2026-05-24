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

from pylib.hooks.bash_policy_decide import (
    apply_policy_gate,
    compute_would_action,
    resolve_hook_strictness,
    strictness_to_default_action,
)
from pylib.hooks.bash_policy_evaluate import validate_command
from pylib.hooks.bash_policy_patterns import (
    compile_patterns,
    detect_os,
    inject_git_root_patterns,
    load_config,
    merge_os_config,
)
from pylib.hooks.bash_policy_shadow import build_shadow_row, write_shadow_row
from pylib.hooks.mark_registry import find_mark_for_command
from pylib.hooks.reflect_predicate import DEFAULT_PREDICATE, evaluate as evaluate_predicate


def _predicate_for_command(command: str, reflect_patterns, config: dict) -> str:
    """Return the self_approve_predicate for the [reflect.*] section
    whose pattern matches the command.

    Falls back to DEFAULT_PREDICATE when no [reflect.*] pattern matches
    (the command was unmatched at strict/balanced, but the agent
    deliberated and marked it safe anyway — apply the conservative
    default).
    """
    for pattern in reflect_patterns:
        try:
            if not pattern.regex.search(command):
                continue
        except Exception:
            continue
        # pattern.section looks like "reflect.<rule>".
        section = pattern.section.split(".", 1)[1] if "." in pattern.section else pattern.section
        entry = (config.get("reflect") or {}).get(section, {})
        predicate = entry.get("self_approve_predicate")
        if isinstance(predicate, str) and predicate.strip():
            return predicate
        return DEFAULT_PREDICATE
    return DEFAULT_PREDICATE


REFLECT_PROMPT_TEMPLATE = """\
This command did not match a curated allow/deny rule (or matched a [reflect.*]
pattern). Before re-attempting, deliberate on three axes and decide:

  reversibility: low | medium | high
  blast_radius:  local | repo | machine | external
  confidence:    0.0..1.0

If the classification satisfies the matched [reflect.*] pattern's
self_approve_predicate (default: confidence >= 0.7), record the deliberation
with the `mark_command_safe` MCP tool and retry — the hook will honor the
mark on retry within this session.

If you are uncertain, or the predicate is not satisfied, use AskUserQuestion
to escalate. The org-answering layer routes to a human via the configured
notification channels (interactive CLI, GitHub issue, or Slack).

Do not retry the same command without either recording a mark or receiving
explicit go-ahead via AskUserQuestion.

----- original validator reason -----
{reason}
"""


def output_decision(decision: str, reason: str, fmt: str = "claude"):
    """Emit the validator decision in a host-specific shape.

    Per #923: hooks emit only ``allow`` or ``deny``. The validator's
    internal "ask" decision (matched [reflect.*] pattern or no-match
    fallback at strict/balanced strictness) is translated here into a
    ``deny`` with a structured reflection prompt that keeps the LLM in
    control of the session in every mode, including autonomous.

    ``claude`` (default) emits Claude Code's hookSpecificOutput JSON.
    ``raw`` emits a host-neutral ``{"decision": ..., "reason": ...}`` payload
    that per-host adapter scripts can wrap into whatever JSON their runtime
    expects (Codex, Gemini CLI, OpenCode, …) without re-implementing the
    validator itself.
    """
    if decision == "ask":
        decision = "deny"
        reason = REFLECT_PROMPT_TEMPLATE.format(reason=reason)

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

Validates a bash command (read as JSON on stdin) against the deny / reflect /
allow patterns in <config.toml> and emits a Claude Code permission decision
(allow or deny — never the mechanical "ask", which would block the LLM).

Strictness (the no-match fallback) is read from .myorg/config.yaml under
``autonomy.hook_strictness``. When a command matches no rule, strictness
controls the outcome:

  strict   → deny + heavy reflection prompt (default failure-closed)
  balanced → deny + light reflection prompt
  lax      → allow

Environment variables:
  MYORG_HOOK_STRICTNESS=strict|balanced|lax
        Overrides the value loaded from .myorg/config.yaml. Useful for
        tests and short-lived overrides. An invalid value falls through
        to the config-driven resolution rather than tightening or
        loosening silently.

  MYORG_HOOK_LOG_DIR=<path>
        Directory for hook logs, including the shadow log
        (<dir>/shadow.jsonl) which records what the validator *would*
        have decided under the opposite default for every invocation.
        Default: /tmp/claude-hook-logs. Empty string disables logging.

  MYORG_ACTIVE_AGENT, MYORG_ACTIVE_PROFILE
        Optional context labels included in shadow-log rows.

Removed in #923: MYORG_BASH_DEFAULT (use hook_strictness instead).
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

    # Compile patterns (includes any git-root injections from above).
    # The legacy `[ask.*]` category is renamed to `[reflect.*]` per #923:
    # hooks no longer emit Claude Code's mechanical `ask` — they emit `deny`
    # with a structured reflection prompt. The internal evaluator still
    # uses the variable name `ask_patterns` for now; rename is mechanical
    # and tracked separately to keep this change reviewable.
    deny_patterns = compile_patterns(config, "deny")
    ask_patterns = compile_patterns(config, "reflect")
    allow_patterns = compile_patterns(config, "allow")

    strictness = resolve_hook_strictness(cwd)
    default_action = strictness_to_default_action(strictness)

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

    # #923 (Codex review #2): mark_command_safe runs AFTER pattern eval +
    # policy overlay. A mark can only shortcut an "ask" verdict to
    # "allow" — it cannot launder a deny (whether from a [deny.*]
    # pattern OR from the policy overlay upgrading a [reflect.*] match
    # to deny because evidence is stale or authority is L3). The order
    # matters: previously the mark was checked first, so a single
    # successful force-push mark could persistently bypass the
    # graduated-autonomy gate even when the journey-health evidence had
    # gone stale. Now the gate dominates.
    if decision == "ask":
        mark = find_mark_for_command(command, cwd)
        if mark:
            predicate = _predicate_for_command(command, ask_patterns, config)
            classification = mark.get("classification") or {}
            if evaluate_predicate(predicate, classification):
                decision = "allow"
                reason = (
                    f"[mark_command_safe] honored prior deliberation "
                    f"(predicate={predicate!r}, "
                    f"classification={json.dumps(classification, sort_keys=True)})"
                )
                matched_category = "mark"
                matched_rule = mark.get("generalization") or matched_rule

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
