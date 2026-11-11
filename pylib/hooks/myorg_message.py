"""Shared structured-message formatter for getmyorg's hook validators.

All three guards (bash, read, edit) emit a reason in the form:

    [getmyorg] <KIND> <STATE>  (rule: <rule>)
      <body line 1>
      <body line 2>
      ...
      level:   /myorg:guards ($myorg:guards in Codex) shows or changes the guard level

Keeping this in one place means a user who recognises one message recognises
them all, and the footer points at the one place to see or change the guard
level.
"""
from __future__ import annotations


# Claude Code names plugin commands /myorg:<name> and Codex $myorg:<name>.
# A guard can't tell which host runs it, so the messages name both.
GUARDS_COMMAND = "/myorg:guards ($myorg:guards in Codex)"

_FOOTER = f"  level:   {GUARDS_COMMAND} shows or changes the guard level"


def format_reason(
    kind: str,
    state: str,
    rule: str | None,
    body_lines: list[str],
) -> str:
    """Format a hook decision reason as a multi-line structured string.

    Args:
        kind: Guard family — "BASH", "READ", "EDIT".
        state: Outcome — "DENIED"/"STOPPED", "ASK", "NOTE".
        rule: Pattern section name (e.g. "deny.privilege_escalation"), or
              None when no specific rule fired.
        body_lines: Free-form body lines (already-formatted "key: value" or
                    similar). Each is indented two spaces in the output.
    """
    if rule:
        header = f"[getmyorg] {kind} {state}  (rule: {rule})"
    else:
        header = f"[getmyorg] {kind} {state}"
    parts = [header]
    parts.extend(f"  {line}" for line in body_lines)
    parts.append(_FOOTER)
    return "\n".join(parts)
