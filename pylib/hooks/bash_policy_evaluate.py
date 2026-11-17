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
    mask_literal_pipes,
    runs_shell_code,
    split_commands,
    strip_line_continuations,
    substitute_known_vars,
    unwrap_command,
)
from bash_policy_patterns import CompiledPattern
from myorg_message import format_reason

_MAX_SUBSTITUTION_DEPTH = 8

# The one rule about what a pipe feeds into (`curl ... | sh`). It sees the
# command with data pipes (quoted, escaped, heredoc bodies) blanked out.
_PIPE_RULE_SECTION = "reflect.code_execution"

__all__ = ["check_patterns", "validate_command"]


def _ask_reason(rule: str, command: str, why: str) -> str:
    """The reason text for an ask: the rule, the command and why it matched."""
    return format_reason(
        kind="BASH",
        state="ASK",
        rule=rule,
        body_lines=[f"command: {command[:100]}", f"why:     {why}"],
    )


def check_patterns(
    segment: str, patterns: list[CompiledPattern], skip: frozenset[str] = frozenset()
) -> tuple[bool, str]:
    """Check if segment matches any compiled pattern outside ``skip``.

    Returns (matched, section_name).
    """
    pipe_view: str | None = None
    for pattern in patterns:
        if pattern.section in skip:
            continue
        subject = segment
        if pattern.section == _PIPE_RULE_SECTION:
            # A shell's command string and heredoc are code: never mask them.
            if pipe_view is None:
                pipe_view = segment if runs_shell_code(segment) else mask_literal_pipes(segment)
            subject = pipe_view
        if pattern.regex.search(subject):
            return True, pattern.section
    return False, ""


def _match(
    subjects: list[str], patterns: list[CompiledPattern], soft: frozenset[str]
) -> tuple[str | None, str | None]:
    """Return (hard_section, soft_section) across all subjects.

    A hard (non-soft) match anywhere wins: a soft rule that happens to come
    first in the catalog must not hide it (#1089 review). The soft section
    is the first soft rule that matched, or None.
    """
    hard = soft_hit = None
    for subject in subjects:
        matched, section = check_patterns(subject, patterns, skip=soft)
        if matched:
            hard = hard or section
        if soft_hit is None and soft:
            only_soft = [pat for pat in patterns if pat.section in soft]
            matched, section = check_patterns(subject, only_soft)
            if matched:
                soft_hit = section
    return hard, soft_hit


def _validate_command_substitutions(
    segment: str,
    deny_patterns: list[CompiledPattern],
    ask_patterns: list[CompiledPattern],
    allow_patterns: list[CompiledPattern],
    default_action: str,
    substitution_depth: int,
    soft_sections: frozenset[str] = frozenset(),
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
            soft_sections,
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
    soft_sections: frozenset[str] = frozenset(),
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

    ``soft_sections`` names deny/reflect sections that only earn a note at
    the caller's guard level (#1089). A match there doesn't stop the scan:
    a harder match later in the command still wins. When the only matches
    are soft, decision is "note" and matched_category is the soft rule's
    own category.
    """
    command = strip_line_continuations(command)

    note: tuple[str, str, str] | None = None  # (reason, category, rule)

    def soft_note(category: str, rule: str, subject: str) -> None:
        nonlocal note
        if note is None:
            note = (
                format_reason(
                    kind="BASH",
                    state="NOTE",
                    rule=rule,
                    body_lines=[f"command: {subject[:100]}", f"why:     matches {rule}"],
                ),
                category,
                rule,
            )

    hard, soft = _match([command], deny_patterns, soft_sections)
    if soft:
        soft_note("deny", soft, command)
    matched, section = hard is not None, hard
    if matched:
        reason = format_reason(
            kind="BASH",
            state="DENIED",
            rule=section,
            body_lines=[
                f"command: {command[:100]}",
                f"why:     matches {section}",
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

    # Segments are single pipeline stages, so `curl ... | sh` never reaches the
    # pipe rule as one string. Check each whole pipeline against it too.
    pipe_patterns = [pat for pat in ask_patterns if pat.section == _PIPE_RULE_SECTION]
    for pipeline in split_commands(command, split_pipes=False) if pipe_patterns else []:
        cleaned_pipeline = clean_segment(pipeline)
        hard, soft = _match([cleaned_pipeline], pipe_patterns, soft_sections)
        if hard is None and soft:
            soft_note("ask", soft, cleaned_pipeline)
        elif hard is not None and not has_real_ask:
            has_real_ask = True
            ask_section = hard
            ask_segment = cleaned_pipeline
            ask_reason = _ask_reason(hard, cleaned_pipeline, f"matches {hard}")

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
            soft_sections,
        ):
            if substitution_decision == "deny":
                reason = format_reason(
                    kind="BASH",
                    state="DENIED",
                    rule=substitution_rule,
                    body_lines=[
                        f"command substitution: {substitution_body[:100]}",
                        f"why:     {substitution_reason}",
                    ],
                )
                return (
                    "deny",
                    reason,
                    substitution_category,
                    substitution_rule,
                )
            if substitution_decision == "note":
                if note is None:
                    note = (substitution_reason, substitution_category, substitution_rule)
                continue
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
                    ask_reason = _ask_reason(substitution_rule, substitution_body, substitution_reason)
            elif substitution_category == "default" and not has_default_fallback:
                has_default_fallback = True
                fallback_segment = substitution_body
                fallback_reason = substitution_reason

        cleaned = clean_segment(segment)
        if not cleaned:
            continue

        cleaned = substitute_known_vars(cleaned, env_context)
        # Check what a wrapper runs too: `sudo dd of=/dev/sda` must hit the
        # disk rule even where sudo alone is only a note.
        unwrapped = unwrap_command(cleaned)
        subjects = [cleaned] if unwrapped == cleaned else [cleaned, unwrapped]

        hard, soft = _match(subjects, deny_patterns, soft_sections)
        if soft:
            soft_note("deny", soft, cleaned)
        matched, section = hard is not None, hard
        if matched:
            reason = format_reason(
                kind="BASH",
                state="DENIED",
                rule=section,
                body_lines=[
                    f"command: {cleaned[:100]}",
                    f"why:     matches {section}",
                ],
            )
            return "deny", reason, "deny", section

        hard, soft = _match(subjects, ask_patterns, soft_sections)
        if hard is None and soft:
            soft_note("ask", soft, cleaned)
            continue
        matched, section = hard is not None, hard
        if matched:
            if not has_real_ask:
                has_real_ask = True
                ask_section = section
                ask_segment = cleaned
                ask_reason = _ask_reason(section, cleaned, f"matches {section}")
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
                ],
            ),
            "ask",
            ask_section,
        )

    if note is not None:
        return "note", note[0], note[1], note[2]

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
