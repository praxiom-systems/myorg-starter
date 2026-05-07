#!/usr/bin/env python3
# Originally adapted from amulya-labs/ai-dev-foundry (MIT). Modifications © praxiom-systems.
"""
Bash command validator for Claude Code PreToolUse hook.
Reads patterns from TOML config and validates commands.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_log
from bash_command_parsing import (
    clean_segment,
    extract_assignments,
    extract_command_substitutions,
    split_commands,
    strip_line_continuations,
    substitute_known_vars,
)
from bash_policy_patterns import (
    CompiledPattern,
    compile_patterns,
    detect_os,
    inject_git_root_patterns,
    load_config,
    merge_os_config,
)
from myorg_message import format_reason


_MAX_SUBSTITUTION_DEPTH = 8


def check_patterns(segment: str, patterns: list[CompiledPattern]) -> tuple[bool, str]:
    """Check if segment matches any compiled pattern.

    Returns (matched, section_name).
    """
    for pattern in patterns:
        if pattern.regex.search(segment):
            return True, pattern.section
    return False, ""


try:
    from _policy_overlay import apply_policy_gate  # type: ignore[import-not-found]
except ImportError:
    def apply_policy_gate(  # noqa: D401
        decision: str,
        reason: str,
        matched_category: str,
        matched_rule: str | None,
        cwd: str,
    ) -> tuple[str, str, str, str | None]:
        """Public starter: policy overlay not present; passthrough."""
        return decision, reason, matched_category, matched_rule


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


def _validate_command_substitutions(
    segment: str,
    deny_patterns: list[CompiledPattern],
    ask_patterns: list[CompiledPattern],
    allow_patterns: list[CompiledPattern],
    default_action: str,
    substitution_depth: int,
) -> list[tuple[str, str, str, str, str | None]]:
    """Validate commands embedded in command/process substitutions.

    Returns tuples of (body, decision, reason, matched_category, matched_rule).
    """
    bodies = [body.strip() for body in extract_command_substitutions(segment)]
    bodies = [body for body in bodies if body]
    if not bodies:
        return []

    if substitution_depth >= _MAX_SUBSTITUTION_DEPTH:
        return [(
            segment[:100],
            "ask",
            "Command substitution nesting limit exceeded",
            "ask",
            None,
        )]

    results: list[tuple[str, str, str, str, str | None]] = []
    for body in bodies:
        decision, reason, matched_category, matched_rule = validate_command(
            body,
            deny_patterns,
            ask_patterns,
            allow_patterns,
            default_action,
            substitution_depth + 1,
        )
        results.append((body, decision, reason, matched_category, matched_rule))
        if decision == "deny":
            break
    return results


def validate_command(
    command: str,
    deny_patterns: list[CompiledPattern],
    ask_patterns: list[CompiledPattern],
    allow_patterns: list[CompiledPattern],
    default_action: str = "ask",
    substitution_depth: int = 0,
) -> tuple[str, str, str, str | None]:
    """Validate a command against patterns.

    Returns (decision, reason, matched_category, matched_rule).
    decision is one of: "deny", "ask", "allow"
    matched_category is one of: "deny", "ask", "allow", "default"
        — "default" means at least one segment matched no rule and the
        decision was driven by ``default_action``.
    matched_rule is the section name of the load-bearing match
        (e.g. "deny.privilege_escalation") or None if no rule matched.

    ``default_action`` controls the fallback when a segment matches no
    rule. "ask" preserves the historical behavior; "allow" makes the
    deny+ask lists the fence and silently approves anything else.
    """
    command = strip_line_continuations(command)

    matched, section = check_patterns(command, deny_patterns)
    if matched:
        reason = format_reason(
            kind="BASH",
            state="DENIED",
            rule=section,
            body_lines=[
                f"command: {command[:100]}",
                f"why:     matches {section}",
                "override: this rule cannot be overridden via env; "
                "edit blueprint/hooks/bash-patterns.toml under your user override",
            ],
        )
        return "deny", reason, "deny", section

    segments = split_commands(command)

    has_real_ask = False
    has_default_fallback = False
    ask_section: str | None = None
    ask_segment: str = ""
    ask_reason: str | None = None
    fallback_segment: str = ""
    fallback_reason: str | None = None
    allow_section: str | None = None

    env_context: dict[str, str] = {}

    for segment in segments:
        env_context.update(extract_assignments(segment))

        for (
            substitution_body,
            substitution_decision,
            substitution_reason,
            substitution_category,
            substitution_rule,
        ) in _validate_command_substitutions(
            segment,
            deny_patterns,
            ask_patterns,
            allow_patterns,
            default_action,
            substitution_depth,
        ):
            if substitution_decision == "deny":
                reason = format_reason(
                    kind="BASH",
                    state="DENIED",
                    rule=substitution_rule,
                    body_lines=[
                        f"command substitution: {substitution_body[:100]}",
                        f"why:     {substitution_reason}",
                        "override: edit blueprint/hooks/bash-patterns.toml",
                    ],
                )
                return (
                    "deny",
                    reason,
                    substitution_category,
                    substitution_rule,
                )
            if substitution_decision == "ask":
                if substitution_category == "default":
                    if not has_default_fallback:
                        has_default_fallback = True
                        fallback_segment = substitution_body
                        fallback_reason = substitution_reason
                elif not has_real_ask:
                    has_real_ask = True
                    ask_section = substitution_rule
                    ask_segment = substitution_body
                    ask_reason = format_reason(
                        kind="BASH",
                        state="ASK",
                        rule=substitution_rule,
                        body_lines=[
                            f"command: {substitution_body[:100]}",
                            f"why:     {substitution_reason}",
                            "override: MYORG_BASH_DEFAULT=allow to bypass interactive ask",
                        ],
                    )
            elif substitution_category == "default" and not has_default_fallback:
                has_default_fallback = True
                fallback_segment = substitution_body
                fallback_reason = substitution_reason

        cleaned = clean_segment(segment)
        if not cleaned:
            continue

        cleaned = substitute_known_vars(cleaned, env_context)

        matched, section = check_patterns(cleaned, deny_patterns)
        if matched:
            reason = format_reason(
                kind="BASH",
                state="DENIED",
                rule=section,
                body_lines=[
                    f"command: {cleaned[:100]}",
                    f"why:     matches {section}",
                    "override: this rule cannot be overridden via env; "
                    "edit blueprint/hooks/bash-patterns.toml under your user override",
                ],
            )
            return "deny", reason, "deny", section

        matched, section = check_patterns(cleaned, ask_patterns)
        if matched:
            if not has_real_ask:
                has_real_ask = True
                ask_section = section
                ask_segment = cleaned
                ask_reason = format_reason(
                    kind="BASH",
                    state="ASK",
                    rule=section,
                    body_lines=[
                        f"command: {cleaned[:100]}",
                        f"why:     matches {section}",
                        "override: MYORG_BASH_DEFAULT=allow to bypass interactive ask",
                    ],
                )
            continue

        matched, section = check_patterns(cleaned, allow_patterns)
        if matched:
            if allow_section is None:
                allow_section = section
            continue

        if not has_default_fallback:
            has_default_fallback = True
            fallback_segment = cleaned
            fallback_reason = None

    if has_real_ask:
        return (
            "ask",
            ask_reason or format_reason(
                kind="BASH",
                state="ASK",
                rule=ask_section,
                body_lines=[
                    f"command: {ask_segment[:100]}",
                    f"why:     matches {ask_section}",
                    "override: MYORG_BASH_DEFAULT=allow to bypass interactive ask",
                ],
            ),
            "ask",
            ask_section,
        )

    if has_default_fallback:
        if default_action == "allow":
            return (
                "allow",
                fallback_reason or f"'{fallback_segment}' not in any list (default-allow)",
                "default",
                None,
            )
        return (
            "ask",
            fallback_reason or f"'{fallback_segment}' not in auto-approve list",
            "default",
            None,
        )

    return "allow", "Command matches allow patterns", "allow", allow_section


def _resolve_default_action() -> str:
    """Resolve the default-action env var.

    Default is now ``allow`` — deny+ask lists are the fence and anything
    that doesn't match either is silently approved. Set ``MYORG_BASH_DEFAULT=ask``
    to restore strict behavior (unknown commands require confirmation). Anything
    other than ``ask`` resolves to ``allow`` (defensive: typos don't silently
    tighten the fence in the wrong direction).
    """
    val = os.environ.get("MYORG_BASH_DEFAULT", "allow").strip().lower()
    return "ask" if val == "ask" else "allow"


def _compute_would_action(
    actual_decision: str, matched_category: str, default_action: str
) -> str:
    """Compute what the decision would have been under the *other* default.

    The flip only matters when the load-bearing path was the default
    fallback — deny/ask/allow matches are stable across the flip.
    """
    if matched_category != "default":
        return actual_decision
    flipped = "ask" if default_action == "allow" else "allow"
    return flipped


_TOKEN_HASH_THRESHOLD = 32


def _sanitize_command(cmd: str) -> str:
    """Sanitize a command for the shadow log.

    Preserves overall command shape (so reviewers can recognize it)
    while hashing any whitespace-separated token longer than
    ``_TOKEN_HASH_THRESHOLD`` chars. Catches long bearer tokens, base64
    blobs, large path bodies, etc., without dropping the command verb.
    """
    out_tokens: list[str] = []
    for tok in cmd.split():
        if len(tok) >= _TOKEN_HASH_THRESHOLD:
            digest = hashlib.sha256(tok.encode("utf-8", errors="replace")).hexdigest()[:8]
            out_tokens.append(f"<sha8:{digest}>")
        else:
            out_tokens.append(tok)
    return " ".join(out_tokens)


def _hash_repo(cwd: str) -> str | None:
    """Return a stable opaque hash of the repo path, or None if no cwd."""
    if not cwd:
        return None
    return hashlib.sha256(cwd.encode("utf-8", errors="replace")).hexdigest()[:12]


def _write_shadow_log(row: dict) -> None:
    """Append one JSONL row to the shadow log. Failures are silent."""
    log_dir = hook_log.resolve_log_dir()
    if log_dir is None:
        return
    hook_log.append_jsonl_row(hook_log.shadow_log_path(log_dir), row)


def _build_shadow_row(
    *,
    command: str,
    cwd: str,
    input_data: dict,
    actual_decision: str,
    matched_category: str,
    matched_rule: str | None,
    would_action: str,
    default_mode: str,
) -> dict:
    return {
        "ts": int(time.time() * 1000),
        "session_id": input_data.get("session_id"),
        "repo_hash": _hash_repo(cwd),
        "agent_slug": os.environ.get("MYORG_ACTIVE_AGENT"),
        "profile": os.environ.get("MYORG_ACTIVE_PROFILE"),
        "cmd_sanitized": _sanitize_command(command),
        "cmd_sha256": hashlib.sha256(
            command.encode("utf-8", errors="replace")
        ).hexdigest(),
        "matched_rule": matched_rule,
        "matched_category": matched_category,
        "would_action": would_action,
        "actual_action": actual_decision,
        "default_mode": default_mode,
    }


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

    default_action = _resolve_default_action()
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

    would_action = _compute_would_action(decision, matched_category, default_action)
    _write_shadow_log(
        _build_shadow_row(
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
