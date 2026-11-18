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

from __future__ import annotations

import json
import os
import sys

from bash_policy_decide import (
    apply_policy_gate,
    compute_would_action,
    resolve_gate,
    resolve_hook_strictness,
    strictness_to_default_action,
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
from force_push import protected_force_push
from mark_registry import find_mark_for_command
from myorg_message import GATE_OFF_COMMAND, GUARDS_COMMAND, format_reason
from reflect_predicate import DEFAULT_PREDICATE, evaluate as evaluate_predicate


def _predicate_for_command(command: str, reflect_patterns, config: dict) -> str:
    """Return the self_approve_predicate for the [reflect.*] section
    whose pattern matches the command.

    Falls back to DEFAULT_PREDICATE when no [reflect.*] pattern matches
    (the command was unmatched at strict, but the agent
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
getmyorg's guard bounced this command so you can think it through first.

{reason}

Before retrying, rate it on three axes:

  reversibility: low | medium | high
  blast_radius:  local | repo | machine | external
  confidence:    0.0..1.0

If the rating clears the rule's bar (by default, confidence >= 0.7), record it
with the `mark_command_safe` tool and retry once; the guard honors the mark for
the rest of this session. Otherwise, ask the user before going further.

Don't change the guard level yourself. The user can type {guards}
to see or change it ({level} now), or run the command themselves with
`! <command>`.
"""

STOP_TEMPLATE = """\
getmyorg's guard stopped this command.

{reason}

You can't clear this stop yourself, and retrying won't help. If the command is
really needed, explain why to the user. They can run it themselves with
`! <command>`, or change the guard level with {guards}
({level} now). Don't change the guard level yourself.
"""

GATE_TEMPLATE = """\
getmyorg's gate: this command isn't on the routine list, so it needs your OK.

{reason}

Gate mode is on; {gate} turns it off.
"""

NOTE_TEMPLATE = """\
getmyorg guard note ({level} level): this command runs, but double-check it's
what you mean.

{reason}
"""


def soft_sections_for(config: dict, level: str) -> frozenset[str]:
    """Rules that earn only a note at ``level`` (#1089).

    relaxed: every [reflect.*] rule, and [deny.*] rules marked
    ``note_from = "relaxed"``. guarded: [reflect.*] rules marked
    ``note_from = "guarded"``. strict: none.
    """
    soft: set[str] = set()
    for name, entry in (config.get("reflect") or {}).items():
        if not isinstance(entry, dict):
            continue
        note_from = entry.get("note_from", "")
        if level == "relaxed" or (level == "guarded" and note_from == "guarded"):
            soft.add(f"reflect.{name}")
    if level == "relaxed":
        for name, entry in (config.get("deny") or {}).items():
            if isinstance(entry, dict) and entry.get("note_from") == "relaxed":
                soft.add(f"deny.{name}")
    return frozenset(soft)


def gate_applies(gate: str, fmt: str, input_data: dict) -> bool:
    """Whether gate mode can ask the user here (#1168).

    Only Claude Code shows a hook's ``ask`` as a permission prompt. Codex
    marks the hook run failed and runs the command, so gate mode stays out
    of Codex (it sets ``PLUGIN_ROOT`` and sends a ``turn_id``) and out of
    the raw format its adapters read.
    """
    if gate != "on" or fmt != "claude":
        return False
    return not ("turn_id" in input_data or os.environ.get("PLUGIN_ROOT"))


def output_decision(decision: str, reason: str, fmt: str = "claude", level: str = "guarded"):
    """Emit the validator decision in a host-specific shape.

    The guard never emits ``allow`` (#1089): a plugin's hooks run in every
    project, and an allow would skip the user's own permission prompts.

    - ``deny``: a stop the agent can't clear.
    - ``ask``: a bounce, sent as ``deny`` plus a prompt to think it through;
      ``mark_command_safe`` can clear it.
    - ``gate``: gate mode (#1168), sent as Claude Code's ``ask``, so the
      user answers a permission prompt. Nothing the agent does clears it.
    - ``note``: no decision, plus ``additionalContext`` for the agent.
    - ``allow``: no output; Claude Code's permission rules decide.

    ``raw`` emits ``{"decision": ..., "reason": ...}`` for per-host adapter
    scripts: ``deny`` (a stop or a bounce, with the full message), ``note``
    or ``allow``.
    """
    if fmt == "raw":
        if decision in ("ask", "deny"):
            template = REFLECT_PROMPT_TEMPLATE if decision == "ask" else STOP_TEMPLATE
            decision, reason = "deny", template.format(level=level, reason=reason, guards=GUARDS_COMMAND)
        print(json.dumps({"decision": decision, "reason": reason}))
        return
    if decision == "allow":
        return
    if decision == "gate":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": GATE_TEMPLATE.format(reason=reason, gate=GATE_OFF_COMMAND),
            }
        }))
        return
    if decision == "note":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": NOTE_TEMPLATE.format(level=level, reason=reason, guards=GUARDS_COMMAND),
            }
        }))
        return
    template = REFLECT_PROMPT_TEMPLATE if decision == "ask" else STOP_TEMPLATE
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": template.format(level=level, reason=reason, guards=GUARDS_COMMAND),
        }
    }))


_HELP_TEXT = """\
Usage: validate-bash.py <config.toml> [--format claude|raw] [--help]

Validates a bash command (read as JSON on stdin) against the deny / reflect /
allow patterns in <config.toml>. It stops the command, bounces it back to the
agent, adds a note, asks the user (gate mode), or says nothing. It never
emits "allow".

The guard level is the user's autonomy.hook_strictness: the repo's
gitignored .myorg/config.local.yaml, then the user's getmyorg config.yaml,
then relaxed.

  strict   → commands that match no allow rule bounce
  guarded  → risky [reflect.*] commands bounce; routine ones get a note
  relaxed  → only never-OK [deny.*] rules stop; the rest get a note
  off      → the guard does nothing

Gate mode (autonomy.gate: on, default off) asks the user, through Claude
Code's permission prompt, about every command that isn't routine, at every
level but off. Codex can't ask, so it doesn't apply there.

Force-pushing or deleting main, master or the remote's default branch is
stopped at every level except off.

Environment variables:
  MYORG_HOOK_STRICTNESS=strict|guarded|relaxed|off
  MYORG_HOOK_GATE=on|off
        Override the settings files. Useful for
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

  MYORG_SHADOW_LOG=0
        Don't write the shadow log (the plugin's hooks set this).
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

    # Read JSON input first — cwd decides the level and the git-root patterns.
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Invalid input, let it pass
        sys.exit(0)

    command = input_data.get("tool_input", {}).get("command", "")
    if not command:
        sys.exit(0)

    cwd = input_data.get("cwd", "")
    if cwd:
        cwd = os.path.normpath(cwd)
    strictness = resolve_hook_strictness(cwd)
    if strictness == "off":
        sys.exit(0)

    config = load_config(config_path)

    # Load and merge OS-specific patterns (e.g. bash-patterns.linux.toml)
    os_suffix = detect_os()
    config_dir = os.path.dirname(config_path)
    config_base = os.path.splitext(os.path.basename(config_path))[0]
    os_config_path = os.path.join(config_dir, f"{config_base}.{os_suffix}.toml")
    if os.path.isfile(os_config_path):
        os_config = load_config(os_config_path)
        config = merge_os_config(config, os_config)

    # If running inside a git repository, inject git-root-aware patterns before
    # compiling so that rm within the repo is auto-approved and .git is protected.
    if cwd and os.path.exists(os.path.join(cwd, ".git")):
        inject_git_root_patterns(config, cwd)

    protected = protected_force_push(command, cwd)
    if protected:
        output_decision(
            "deny",
            format_reason(
                kind="BASH",
                state="STOPPED",
                rule="force_push.protected_branch",
                body_lines=[
                    f"command: {command[:100]}",
                    f"why:     force-pushes or deletes {protected}, which other people's work builds on",
                ],
            ),
            fmt=fmt,
            level=strictness,
        )
        return

    # Compile patterns (includes any git-root injections from above).
    # The legacy `[ask.*]` category is renamed to `[reflect.*]` per #923:
    # hooks no longer emit Claude Code's mechanical `ask` — they emit `deny`
    # with a structured reflection prompt. The internal evaluator still
    # uses the variable name `ask_patterns` for now; rename is mechanical
    # and tracked separately to keep this change reviewable.
    deny_patterns = compile_patterns(config, "deny")
    ask_patterns = compile_patterns(config, "reflect")
    allow_patterns = compile_patterns(config, "allow")

    default_action = strictness_to_default_action(strictness)
    soft_sections = soft_sections_for(config, strictness)
    # Gate mode (#1168): everything but the routine list asks the user, so
    # unmatched commands and [reflect.*] rules count, whatever the level.
    # [deny.*] rules the level softens to a note ask the user too.
    gate = gate_applies(resolve_gate(cwd), fmt, input_data)
    if gate:
        default_action = "ask"
        soft_sections = frozenset(s for s in soft_sections if s.startswith("deny."))

    decision, reason, matched_category, matched_rule = validate_command(
        command, deny_patterns, ask_patterns, allow_patterns, default_action,
        soft_sections=soft_sections,
    )

    # For force-push patterns, apply the graduated-autonomy policy gate.
    # This may upgrade the decision from ask → deny (L3, no fresh evidence)
    # or downgrade to allow (L0/L1, auto-approved). Degrades gracefully
    # when the policy overlay is absent (OSS baseline: passthrough).
    decision, reason, matched_category, matched_rule = apply_policy_gate(
        decision, reason, matched_category, matched_rule, cwd
    )

    # The user answers a gate prompt, so mark_command_safe never sees it.
    if gate and decision in ("ask", "note"):
        decision = "gate"

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

    output_decision(decision, reason, fmt=fmt, level=strictness)


if __name__ == "__main__":
    main()
