"""Pattern-evaluation slice for the bash validator.

Extracted from ``validate-bash.py`` (issue #584) — owns the *evaluate*
slice: walking a command's segments and command-substitutions, matching
each against the compiled deny / ask / allow pattern lists, and producing
a raw verdict tuple. No I/O, no env vars, no host shaping — those live
in ``validate-bash.py`` and ``bash_policy_decide``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bash_command_parsing import (
    clean_segment,
    extract_assignments,
    extract_command_substitutions,
    is_safe_pipe_consumer,
    split_commands,
    strip_line_continuations,
    substitute_known_vars,
)
from bash_policy_patterns import CompiledPattern
from myorg_message import format_reason

_MAX_SUBSTITUTION_DEPTH = 8

__all__ = ["check_patterns", "validate_command"]


def check_patterns(segment: str, patterns: list[CompiledPattern]) -> tuple[bool, str]:
    """Check if segment matches any compiled pattern.

    Returns (matched, section_name).
    """
    for pattern in patterns:
        if pattern.regex.search(segment):
            return True, pattern.section
    return False, ""


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
    default_action: str = "allow",
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
                            "override: set `autonomy.hook_strictness: lax` in .myorg/config.yaml to silence this on no-match",
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
                        "override: set `autonomy.hook_strictness: lax` in .myorg/config.yaml to silence this on no-match",
                    ],
                )
            continue

        matched, section = check_patterns(cleaned, allow_patterns)
        if matched:
            if allow_section is None:
                allow_section = section
            continue

        # Pipe-stage noise mitigation: a read-only pipe consumer (`head -5`,
        # `grep foo`, `wc -l`) that survived deny+ask+allow is treated as
        # implicitly allowed so a benign `ls | head` doesn't trip default-ask.
        # Done HERE — never in clean_segment — so deny patterns always fire
        # first on `cat ~/.ssh/id_rsa`-style reads (PR #601 review).
        if is_safe_pipe_consumer(cleaned):
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
                    "override: set `autonomy.hook_strictness: lax` in .myorg/config.yaml to silence this on no-match",
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
