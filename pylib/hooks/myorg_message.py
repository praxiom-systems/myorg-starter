"""Shared structured-message formatter for myorg-starter hook validators.

All three guards (bash, read, edit) emit a reason in the form:

    [myorg-starter] <KIND> <STATE>  (rule: <rule>)
      <body line 1>
      <body line 2>
      ...
      docs:    bash bin/doctor.sh

Keeping this in one place means a user who recognises one message recognises
them all, and the docs footer is the single discoverability surface for the
doctor command.
"""
from __future__ import annotations


_FOOTER = "  docs:    bash bin/doctor.sh"


def format_reason(
    kind: str,
    state: str,
    rule: str | None,
    body_lines: list[str],
) -> str:
    """Format a hook decision reason as a multi-line structured string.

    Args:
        kind: Guard family — "BASH", "READ", "EDIT".
        state: Outcome — "DENIED", "ASK", "ADVISORY".
        rule: Pattern section name (e.g. "deny.privilege_escalation"), or
              None when no specific rule fired.
        body_lines: Free-form body lines (already-formatted "key: value" or
                    similar). Each is indented two spaces in the output.
    """
    if rule:
        header = f"[myorg-starter] {kind} {state}  (rule: {rule})"
    else:
        header = f"[myorg-starter] {kind} {state}"
    parts = [header]
    parts.extend(f"  {line}" for line in body_lines)
    parts.append(_FOOTER)
    return "\n".join(parts)
