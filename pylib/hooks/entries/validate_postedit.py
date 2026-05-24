#!/usr/bin/env python3
"""
PostToolUse output-verification probe for Edit/Write/MultiEdit.

Runs two checks after an edit tool has applied a change:
  1. Band-aid re-scan — synchronous, advisory-first (same MYORG_BANDAID_MODE
     gating as the PreToolUse detector).  Uses the shared scan_diff() engine
     from validate_edit so pattern logic is never duplicated.
  2. Contract LLM judge — asynchronous fire-and-forget via
     a private contract judge (when an overlay module is available).
     Gated behind MYORG_VERIFY_LLM_JUDGE=1.
     Judge latency NEVER blocks the PostToolUse path.

PostToolUse hooks cannot block tool execution (the edit is already applied).
Output follows Claude Code's ``hookSpecificOutput`` JSON shape for PostToolUse:
  {"hookSpecificOutput": {"hookEventName": "PostToolUse", "suppressOutput": false}}

The hook always exits 0.  Advisory reasons are surfaced in the transcript via
the ``reason`` field without blocking the agent.

Environment variables:
  Strictness               — Read from .myorg/config.yaml autonomy.hook_strictness
                              (lax skips the band-aid re-scan; strict/balanced run it).
                              Legacy MYORG_BANDAID_MODE=off compat-honored for one cycle.
  MYORG_VERIFY_LLM_JUDGE   — Set to "1" to enable async contract judge invocation.
  MYORG_HOOK_LOG_DIR        — Override log directory (empty string disables logging).

Skip-list parity with validate_edit.py:
  - Edits to band-aid-patterns.toml, injection-patterns.toml, bash-patterns.toml
    are silently allowed (no scan, no judge).
  - Paths under tests/fixtures/ are silently allowed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


def _load_validate_edit_module():
    """Load validate_edit sibling by file path.

    Tries both ``validate_edit.py`` (underscore, used by direct-import and
    bundled layouts) and ``validate-edit.py`` (hyphen, used by flat
    projection into host hook dirs) so this module works identically across
    every projection mode.
    """
    here = Path(__file__).resolve().parent
    for name in ("validate_edit.py", "validate-edit.py"):
        candidate = here / name
        if candidate.is_file():
            mod_name = "validate_edit_sibling"
            spec = importlib.util.spec_from_file_location(mod_name, candidate)
            if spec is None or spec.loader is None:
                raise ImportError(f"could not load spec for {candidate}")
            module = importlib.util.module_from_spec(spec)
            # Register before exec so that dataclass cls.__module__ lookups work.
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            return module
    raise ImportError(f"validate_edit sibling not found near {here}")


_validate_edit = _load_validate_edit_module()
scan_diff = _validate_edit.scan_diff
_build_reason = _validate_edit._build_reason
_get_added_lines_for_input = _validate_edit._get_added_lines_for_input
_should_skip_file = _validate_edit._should_skip_file


# ---------------------------------------------------------------------------
# PostToolUse output helpers
# ---------------------------------------------------------------------------

def _output_post(reason: str = "", fmt: str = "claude") -> None:
    """Emit PostToolUse output in the requested format.

    ``claude`` (default) emits Claude Code's hookSpecificOutput shape.
    ``raw`` emits a host-neutral ``{"decision", "reason"}`` payload for
    adapter scripts (parity with validate-edit.py --format raw).
    """
    if fmt == "raw":
        payload: dict = {"decision": "allow"}
        if reason:
            payload["reason"] = reason
        print(json.dumps(payload))
        return
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "suppressOutput": False,
        }
    }
    if reason:
        payload["hookSpecificOutput"]["reason"] = reason
    print(json.dumps(payload))


try:
    from _verify_overlay import invoke_judge_async as _invoke_judge_async  # type: ignore[import-not-found]
except ImportError:
    def _invoke_judge_async(cwd: str) -> None:  # noqa: D401
        """Public starter: judge overlay not present; no-op."""
        return


def main() -> None:
    # Parse arguments: <config_path> [--format claude|raw]
    args = sys.argv[1:]
    fmt = "claude"
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--format":
            if i + 1 >= len(args):
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
        print(
            "Usage: validate-postedit.py <patterns.toml> [--format claude|raw]",
            file=sys.stderr,
        )
        sys.exit(1)

    config_path = positional[0]

    # #923: hook_strictness=lax → skip the band-aid re-scan entirely.
    # Legacy MYORG_BANDAID_MODE=off honored for one cycle for compat.
    try:
        from pylib.hooks.bash_policy_decide import resolve_hook_strictness
        strictness = resolve_hook_strictness(os.getcwd())
    except Exception:
        strictness = "strict"
    if strictness == "lax":
        _output_post(fmt=fmt)
        sys.exit(0)
    mode = os.environ.get("MYORG_BANDAID_MODE", "").strip().lower()
    llm_judge_enabled = os.environ.get("MYORG_VERIFY_LLM_JUDGE", "").strip() == "1"

    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        _output_post(fmt=fmt)
        sys.exit(0)

    tool_name = input_data.get("tool_name", "") or ""
    tool_input = input_data.get("tool_input", {}) or {}
    cwd = input_data.get("cwd", "") or ""

    # Only handle the target edit tools. Codex reports file edits through
    # apply_patch; Claude reports Edit/Write/MultiEdit.
    if tool_name.lower() not in ("edit", "write", "multiedit", "apply_patch"):
        _output_post(fmt=fmt)
        sys.exit(0)

    # Extract diff once (needed for both scan and skip-list check).
    added_lines: list[str] = []
    file_path = ""
    new_content_lines: list[str] = []

    if mode != "off":
        added_lines, file_path, new_content_lines = _get_added_lines_for_input(
            tool_name, tool_input
        )
        # Write tool in PostToolUse: the file on disk already matches the
        # tool input, so the line-by-line diff comes back empty. Fall back to
        # scanning the full new content so write-time band-aids still surface.
        if tool_name.lower() == "write" and not added_lines:
            added_lines = new_content_lines

    # For the judge skip-list check we still need file_path even in off mode.
    if not file_path:
        file_path = tool_input.get("file_path", "") or ""

    # Skip-list: parity with PreToolUse detector.
    if _should_skip_file(file_path):
        _output_post(fmt=fmt)
        sys.exit(0)

    # -------------------------------------------------------------------------
    # Step 1: synchronous band-aid re-scan (advisory only — edit already applied)
    # -------------------------------------------------------------------------
    reason = ""
    if mode != "off" and added_lines:
        result = scan_diff(
            added_lines,
            config_path,
            file_path=file_path,
            new_content_lines=new_content_lines or None,
        )
        if result.deny_hits or result.advise_hits:
            _decision, reason = _build_reason(result, mode)
            # PostToolUse: advisory only regardless of pattern tier —
            # the edit is already applied.  Prefix for transcript clarity.
            reason = "[PostEdit] " + reason

    # -------------------------------------------------------------------------
    # Step 2: async contract LLM judge (fire-and-forget)
    # -------------------------------------------------------------------------
    if llm_judge_enabled:
        _invoke_judge_async(cwd)

    _output_post(reason=reason, fmt=fmt)


if __name__ == "__main__":
    main()
