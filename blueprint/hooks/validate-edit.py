#!/usr/bin/env python3
"""
Edit/Write/MultiEdit band-aid pattern detector for Claude Code PreToolUse hook.

Scans added lines for band-aid patterns before the edit tool applies a change.
Advisory-first: most patterns emit ``allow`` with a reason (visible in
logs/transcript, non-blocking); only high-confidence broad-suppression patterns
emit ``deny``.

Pattern catalog: blueprint/hooks/band-aid-patterns.toml.

Environment variables:
  MYORG_BANDAID_MODE  — Controls hook behaviour.
                        (unset or empty) normal mode: deny fires on [deny.*] hits.
                        all-advise: downgrades deny hits to advisory.
                        off: disables scanning entirely (exit 0 silent).

Public API (importable by sibling hooks):
  scan_diff(added_lines, config_path, file_path="", new_content_lines=None)
      → ScanResult  — run the band-aid engine on an already-extracted diff.
  load_patterns(config_path) → (deny, advise)
  _should_skip_file(file_path) → bool
  _is_test_file(file_path) → bool
  _extract_added_lines_diff(old, new) → list[str]
  _get_added_lines_for_input(tool_name, tool_input) → (added_lines, file_path, new_content_lines)
  _build_reason(result, mode) → (decision, reason)
  output_decision(decision, reason, fmt) → None
  ScanResult, SectionHit, CompiledPattern  — dataclasses
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from myorg_message import format_reason

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        print(
            "Error: Python 3.11+ required, or install 'tomli' for older versions",
            file=sys.stderr,
        )
        sys.exit(1)


# Cap combined added content to avoid spending time on huge generated files.
MAX_SCAN_BYTES = 256 * 1024

# Exact basenames that are always silently skipped — the hook-owned pattern
# files themselves.  This is an exact-match list; do NOT use substring/glob
# matching here — a file like "auth-band-aid.py" must NOT be silently allowed
# (it could smuggle in the very suppressions this hook is meant to catch).
_SKIP_EXACT_BASENAMES: frozenset[str] = frozenset([
    "band-aid-patterns.toml",
    "injection-patterns.toml",
    "bash-patterns.toml",
])

# TODO suppression: if a TODO/FIXME/HACK/XXX line also contains (#NNN),
# suppress the advisory for that line.
_ISSUE_LINK_RE = re.compile(r'\(#\d+\)')

# Test file extensions that trigger the test_skips section skip.
_TEST_FILENAME_RE = re.compile(r'\.(test|spec)\.')


@dataclass
class CompiledPattern:
    regex: re.Pattern
    section: str
    original: str


@dataclass
class SectionHit:
    section: str
    snippet: str  # first matched line, truncated


@dataclass
class ScanResult:
    deny_hits: list[SectionHit] = field(default_factory=list)
    advise_hits: list[SectionHit] = field(default_factory=list)


def load_patterns(
    config_path: str,
) -> tuple[list[CompiledPattern], list[CompiledPattern]]:
    """Load [deny.*] and [advise.*] sections, returning (deny, advise)."""
    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
    except FileNotFoundError:
        print(
            f"Warning: Config file not found: {config_path} — skipping scan",
            file=sys.stderr,
        )
        sys.exit(0)
    except tomllib.TOMLDecodeError as e:
        print(
            f"Warning: Invalid TOML in {config_path}: {e} — skipping scan",
            file=sys.stderr,
        )
        sys.exit(0)
    return (
        _compile(config.get("deny", {}), "deny"),
        _compile(config.get("advise", {}), "advise"),
    )


def _compile(sections: dict, tier: str) -> list[CompiledPattern]:
    out: list[CompiledPattern] = []
    for name, spec in sections.items():
        if not isinstance(spec, dict) or "patterns" not in spec:
            continue
        for pattern in spec["patterns"]:
            try:
                out.append(
                    CompiledPattern(
                        regex=re.compile(pattern),
                        section=f"{tier}.{name}",
                        original=pattern,
                    )
                )
            except re.error as e:
                print(
                    f"Warning: invalid regex '{pattern}' in {tier}.{name}: {e}",
                    file=sys.stderr,
                )
    return out


def _should_skip_file(file_path: str) -> bool:
    """Return True if this file should be silently skipped."""
    if not file_path:
        return False
    basename = os.path.basename(file_path)
    # Exact-match only — no substring/glob on basename.  A file named
    # "auth-band-aid.py" is NOT on this list and must be scanned normally.
    if basename in _SKIP_EXACT_BASENAMES:
        return True
    # Skip test fixtures (directory-scope exemption, not basename trick)
    norm = file_path.replace("\\", "/")
    if "tests/fixtures/" in norm:
        return True
    return False


def _is_test_file(file_path: str) -> bool:
    """Return True if the file is a test/spec file (*.test.* or *.spec.*)."""
    basename = os.path.basename(file_path or "")
    return bool(_TEST_FILENAME_RE.search(basename))


def _extract_added_lines_diff(old: str, new: str) -> list[str]:
    """Return lines that were added (present in new but not old) via unified_diff.

    unified_diff is dramatically faster than ndiff for the typical case
    (large files, small changes). Callers must guard against oversized inputs
    before calling this function.
    """
    added = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    return added


# ---------------------------------------------------------------------------
# Structural rule: two-line swallowed exception
#
# The single-line form  `except Exception: pass`  is caught by the regex
# catalog.  The two-line form:
#
#     except Exception:
#         pass
#
# and the incremental-edit case where only `pass` is added after a
# pre-existing `except ...:`  both evade the regex.  This structural check
# fills the gap by examining *new-content context* — for each added `pass`
# line, we look at the preceding line (in the post-edit text) to see if it
# is an `except` clause.
# ---------------------------------------------------------------------------

_EXCEPT_CLAUSE_RE = re.compile(r'^\s*except(\s+\w+(\s+as\s+\w+)?)?\s*:\s*$')
_PASS_ONLY_RE = re.compile(r'^\s+pass\s*$')
_SWALLOWED_EXCEPT_SECTION = "deny.python_swallowed_errors"


def _check_structural_swallowed_except(
    added_lines: list[str],
    new_content_lines: list[str],
) -> SectionHit | None:
    """
    Look for added `pass` lines that immediately follow an `except ...:` line
    in the post-edit new content.  Returns the first SectionHit if found.

    added_lines     — the set of genuinely-added lines (used as a lookup set)
    new_content_lines — lines of the *new* post-edit text (for context lookup)
    """
    if not new_content_lines:
        return None

    # Build a fast lookup set of added lines (stripped of trailing newline).
    added_set: set[str] = {l.rstrip("\n\r") for l in added_lines}

    for idx, line in enumerate(new_content_lines):
        stripped = line.rstrip("\n\r")
        # Only care about added `pass` lines
        if stripped not in added_set:
            continue
        if not _PASS_ONLY_RE.match(stripped):
            continue
        # Look at the preceding line (if any) in the new content
        if idx == 0:
            continue
        prev = new_content_lines[idx - 1].rstrip("\n\r")
        if _EXCEPT_CLAUSE_RE.match(prev):
            snippet = f"{prev} / {stripped}"
            return SectionHit(
                section=_SWALLOWED_EXCEPT_SECTION,
                snippet=snippet[:120],
            )
    return None


def _scan_lines(
    lines: list[str],
    deny: list[CompiledPattern],
    advise: list[CompiledPattern],
    is_test_file: bool,
    new_content_lines: list[str] | None = None,
) -> ScanResult:
    """Scan lines against compiled patterns, returning all hits.

    new_content_lines, if provided, is used for structural multi-line checks
    (e.g. two-line swallowed exception: `except ...:` followed by `pass`).
    It represents the full post-edit content of the modified text block.
    """
    result = ScanResult()

    # Build sets of sections already recorded (deduplicate per-section, one hit per section)
    seen_deny: set[str] = set()
    seen_advise: set[str] = set()

    for line in lines:
        stripped = line.rstrip("\n\r")

        # Check deny patterns
        for p in deny:
            if p.section not in seen_deny and p.regex.search(stripped):
                seen_deny.add(p.section)
                result.deny_hits.append(SectionHit(
                    section=p.section,
                    snippet=stripped[:120],
                ))

        # Check advise patterns
        for p in advise:
            if p.section not in seen_advise:
                # Skip test_skips section for test files
                if is_test_file and p.section == "advise.test_skips":
                    continue
                if p.regex.search(stripped):
                    # TODO suppression: todo_markers with issue link are suppressed
                    if p.section == "advise.todo_markers" and _ISSUE_LINK_RE.search(stripped):
                        continue
                    seen_advise.add(p.section)
                    result.advise_hits.append(SectionHit(
                        section=p.section,
                        snippet=stripped[:120],
                    ))

    # Structural check: two-line swallowed exception (except ...: / pass).
    # This catches the multi-line form that the single-line regex cannot reach.
    # Only run when we have new content context and the section hasn't already
    # been triggered by the regex scan above.
    if new_content_lines and _SWALLOWED_EXCEPT_SECTION not in seen_deny:
        hit = _check_structural_swallowed_except(lines, new_content_lines)
        if hit is not None:
            seen_deny.add(_SWALLOWED_EXCEPT_SECTION)
            result.deny_hits.append(hit)

    return result


def scan_diff(
    added_lines: list[str],
    config_path: str,
    file_path: str = "",
    new_content_lines: list[str] | None = None,
) -> ScanResult:
    """
    Public entry-point for the band-aid scan engine.

    Run the pattern scan on an already-extracted list of added lines and return
    a ScanResult.  Callers (e.g. validate-postedit.py) that already have the
    added-lines diff computed should call this directly instead of going through
    ``main()``.

    Parameters
    ----------
    added_lines       — Lines that were added (output of _extract_added_lines_diff or
                        equivalent).
    config_path       — Path to band-aid-patterns.toml.
    file_path         — Original file path; used for skip-list and test-file checks.
    new_content_lines — Post-edit full content lines; used for structural multi-line
                        checks (e.g. two-line swallowed exception).  Pass None to
                        skip structural checks.

    Returns
    -------
    ScanResult with deny_hits and advise_hits populated.  Both lists are empty
    when no patterns fire or when the file is on the skip-list.
    """
    if _should_skip_file(file_path):
        return ScanResult()
    if not added_lines:
        return ScanResult()
    total_chars = sum(len(line) for line in added_lines)
    if total_chars > MAX_SCAN_BYTES:
        return ScanResult()
    deny_patterns, advise_patterns = load_patterns(config_path)
    is_test = _is_test_file(file_path)
    return _scan_lines(
        added_lines,
        deny_patterns,
        advise_patterns,
        is_test,
        new_content_lines=new_content_lines,
    )


def _build_reason(result: ScanResult, mode: str) -> tuple[str, str]:
    """
    Return (decision, reason) from a ScanResult.
    decision is 'deny' or 'allow'.
    reason is a structured string produced by format_reason.
    """
    all_hits = result.deny_hits + result.advise_hits
    if not all_hits:
        return "allow", ""

    # In all-advise mode, deny hits are treated as advisories.
    is_deny = bool(result.deny_hits) and mode not in ("all-advise",)

    first_hit = all_hits[0]
    section = first_hit.section
    snippet = first_hit.snippet[:120]
    # De-duplicate while preserving insertion order — when multiple hits
    # land in one validator pass, callers (and tests) expect every distinct
    # section name to appear in the reason, not just the first.
    sections: list[str] = []
    for hit in all_hits:
        if hit.section not in sections:
            sections.append(hit.section)
    sections_line = f"sections: {', '.join(sections)}"

    if is_deny:
        reason = format_reason(
            kind="EDIT",
            state="DENIED",
            rule=section,
            body_lines=[
                f"matched: {snippet}",
                sections_line,
                "override: MYORG_BANDAID_MODE=all-advise (downgrade denies to advisories) or =off (disable)",
            ],
        )
        decision = "deny"
    else:
        reason = format_reason(
            kind="EDIT",
            state="ADVISORY",
            rule=section,
            body_lines=[
                f"matched: {snippet}",
                sections_line,
                "override: MYORG_BANDAID_MODE=off (session) or =all-advise (downgrade denies)",
            ],
        )
        decision = "allow"

    return decision, reason


def output_decision(decision: str, reason: str, fmt: str = "claude") -> None:
    if fmt == "raw":
        print(json.dumps({"decision": decision, "reason": reason}))
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def _get_added_lines_for_input(
    tool_name: str, tool_input: dict
) -> tuple[list[str], str, list[str]]:
    """
    Extract added lines, the target file path, and new-content context lines
    from a tool_input dict.

    Returns (added_lines, file_path, new_content_lines).

    new_content_lines is the post-edit line list used for structural multi-line
    checks (e.g. two-line swallowed exception).  For Write it is the full new
    content.  For Edit/MultiEdit it is the union of all new_string blocks.
    """
    tool_name_lower = tool_name.lower()

    if tool_name_lower == "write":
        content = tool_input.get("content", "") or ""
        file_path = tool_input.get("file_path", "") or ""
        # For Write, diff against the prior file content when the file already
        # exists.  This avoids false positives when overwriting a file that
        # already contains band-aid patterns — only genuinely *added* lines
        # are flagged.  If the file doesn't exist, all content is "added".
        # Read errors (permission, race) are treated as "file doesn't exist"
        # with a logged warning (fail-closed on enforcement: we scan everything,
        # erring on the side of catching real violations over silently passing).
        new_content_lines = content.splitlines()
        if file_path and os.path.isfile(file_path):
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                    prior_content = fh.read()
                # Pre-diff size guard: if combined size exceeds cap, fall back to
                # "all added" (fail-closed) without running the diff.
                if len(prior_content) + len(content) > MAX_SCAN_BYTES * 2:
                    return new_content_lines, file_path, new_content_lines
                return (
                    _extract_added_lines_diff(prior_content, content),
                    file_path,
                    new_content_lines,
                )
            except OSError as exc:
                logging.warning(
                    "validate-edit: could not read prior content of %r (%s) — "
                    "treating full Write content as added",
                    file_path,
                    exc,
                )
        return new_content_lines, file_path, new_content_lines

    if tool_name_lower == "edit":
        old = tool_input.get("old_string", "") or ""
        new = tool_input.get("new_string", "") or ""
        file_path = tool_input.get("file_path", "") or ""
        # Pre-diff size guard: skip diffing oversized Edit pairs — return no
        # added lines (the per-added-bytes cap in main() will short-circuit anyway).
        if len(old) + len(new) > MAX_SCAN_BYTES * 2:
            return [], file_path, new.splitlines()
        return _extract_added_lines_diff(old, new), file_path, new.splitlines()

    if tool_name_lower == "multiedit":
        edits = tool_input.get("edits", []) or []
        file_path = tool_input.get("file_path", "") or ""
        all_added: list[str] = []
        all_new_content: list[str] = []
        for edit in edits:
            old = edit.get("old_string", "") or ""
            new = edit.get("new_string", "") or ""
            # Per-edit size guard: skip oversized edits individually, don't bail
            # the whole MultiEdit batch on one large edit.
            if len(old) + len(new) > MAX_SCAN_BYTES * 2:
                all_new_content.extend(new.splitlines())
                continue
            all_added.extend(_extract_added_lines_diff(old, new))
            all_new_content.extend(new.splitlines())
        return all_added, file_path, all_new_content

    return [], "", []


def main() -> None:
    # Parse arguments
    args = sys.argv[1:]
    fmt = "claude"
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--format":
            if i + 1 >= len(args):
                print(
                    "Usage: validate-edit.py <patterns.toml> [--format claude|raw]",
                    file=sys.stderr,
                )
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
            "Usage: validate-edit.py <patterns.toml> [--format claude|raw]",
            file=sys.stderr,
        )
        sys.exit(1)

    config_path = positional[0]

    # Check kill switch first — before doing any work
    mode = os.environ.get("MYORG_BANDAID_MODE", "").strip().lower()
    if mode == "off":
        sys.exit(0)

    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Malformed input — let the tool proceed rather than blocking
        sys.exit(0)

    tool_name = input_data.get("tool_name", "") or ""
    tool_input = input_data.get("tool_input", {}) or {}

    # Only handle the three target tools
    if tool_name.lower() not in ("edit", "write", "multiedit"):
        sys.exit(0)

    added_lines, file_path, new_content_lines = _get_added_lines_for_input(
        tool_name, tool_input
    )

    # Skip-list: certain file paths are always silently allowed
    if _should_skip_file(file_path):
        sys.exit(0)

    # Short-circuit if nothing was added
    if not added_lines:
        sys.exit(0)

    # Cap total scan size
    total_chars = sum(len(line) for line in added_lines)
    if total_chars > MAX_SCAN_BYTES:
        sys.exit(0)

    deny_patterns, advise_patterns = load_patterns(config_path)
    is_test = _is_test_file(file_path)

    result = _scan_lines(
        added_lines, deny_patterns, advise_patterns, is_test,
        new_content_lines=new_content_lines or None,
    )

    if not result.deny_hits and not result.advise_hits:
        sys.exit(0)

    decision, reason = _build_reason(result, mode)

    if not reason:
        sys.exit(0)

    output_decision(decision, reason, fmt=fmt)


if __name__ == "__main__":
    main()
