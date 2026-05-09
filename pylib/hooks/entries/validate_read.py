#!/usr/bin/env python3
"""
Read-tool content guard for Claude Code PreToolUse hook.

Scans the target file for prompt-injection signatures before the Read tool
surfaces its contents to the model. Advisory-first: most signatures emit
``allow`` with a reason (visible in logs/transcript, non-blocking); only
high-confidence instruction-override signatures emit ``deny``.

Pattern catalog: blueprint/hooks/injection-patterns.toml.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass

from pylib.hooks.myorg_message import format_reason

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


# Only scan files whose extension suggests prose/config. Source files in
# common programming languages are skipped — high volume, low prompt-injection
# risk, and prone to false positives on docstrings / string literals.
SCAN_EXTENSIONS = {
    ".md",
    ".mdx",
    ".markdown",
    ".txt",
    ".rst",
    ".yaml",
    ".yml",
    ".json",
    ".jsonl",
    ".toml",
    ".ini",
    ".cfg",
    ".env",
}

# Files larger than this are skipped — the hook fires on every Read and must
# stay cheap. Injection payloads are typically short; 256KB covers realistic
# prose docs without paying the cost of scanning large data files.
MAX_SCAN_BYTES = 256 * 1024


@dataclass
class CompiledPattern:
    regex: re.Pattern
    section: str
    original: str


def load_patterns(config_path: str) -> tuple[list[CompiledPattern], list[CompiledPattern]]:
    """Load [deny.*] and [advise.*] sections, returning (deny, advise)."""
    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
    except FileNotFoundError:
        print(f"Warning: Config file not found: {config_path} — skipping scan", file=sys.stderr)
        sys.exit(0)
    except tomllib.TOMLDecodeError as e:
        print(f"Warning: Invalid TOML in {config_path}: {e} — skipping scan", file=sys.stderr)
        sys.exit(0)
    return _compile(config.get("deny", {}), "deny"), _compile(config.get("advise", {}), "advise")


def _compile(sections: dict, tier: str) -> list[CompiledPattern]:
    out: list[CompiledPattern] = []
    for name, spec in sections.items():
        if not isinstance(spec, dict) or "patterns" not in spec:
            continue
        for pattern in spec["patterns"]:
            try:
                out.append(
                    CompiledPattern(
                        regex=re.compile(pattern, re.IGNORECASE),
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


def should_scan(file_path: str) -> bool:
    if not file_path:
        return False
    ext = os.path.splitext(file_path)[1].lower()
    return ext in SCAN_EXTENSIONS


def read_content(file_path: str) -> str | None:
    """Read up to MAX_SCAN_BYTES of file_path as UTF-8. None on any failure."""
    try:
        if os.path.getsize(file_path) > MAX_SCAN_BYTES:
            return None
        with open(file_path, "rb") as f:
            raw = f.read(MAX_SCAN_BYTES)
    except OSError:
        return None
    return raw.decode("utf-8", errors="replace")


def scan(content: str, patterns: list[CompiledPattern]) -> CompiledPattern | None:
    for p in patterns:
        if p.regex.search(content):
            return p
    return None


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


def main() -> None:
    args = sys.argv[1:]
    fmt = "claude"
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--format":
            if i + 1 >= len(args):
                print(
                    "Usage: validate-read.py <patterns.toml> [--format claude|raw]",
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
            "Usage: validate-read.py <patterns.toml> [--format claude|raw]",
            file=sys.stderr,
        )
        sys.exit(1)

    config_path = positional[0]

    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Malformed input — let the Read proceed rather than blocking
        sys.exit(0)

    file_path = input_data.get("tool_input", {}).get("file_path", "")
    cwd = input_data.get("cwd", "")
    if file_path and not os.path.isabs(file_path) and cwd:
        file_path = os.path.join(cwd, file_path)
    if not should_scan(file_path):
        sys.exit(0)

    content = read_content(file_path)
    if content is None:
        sys.exit(0)

    deny, advise = load_patterns(config_path)

    hit = scan(content, deny)
    if hit is not None:
        output_decision(
            "deny",
            format_reason(
                kind="READ",
                state="DENIED",
                rule=hit.section,
                body_lines=[
                    f"file:    {file_path}",
                    f"why:     content matches {hit.section}",
                    "override: this rule cannot be overridden — review the file before re-running",
                ],
            ),
            fmt=fmt,
        )
        return

    hit = scan(content, advise)
    if hit is not None:
        output_decision(
            "allow",
            format_reason(
                kind="READ",
                state="ADVISORY",
                rule=hit.section,
                body_lines=[
                    f"file:    {file_path}",
                    f"why:     content matches {hit.section}",
                ],
            ),
            fmt=fmt,
        )
        return

    # Clean — silent allow (empty stdout so the tool proceeds without note).
    sys.exit(0)


if __name__ == "__main__":
    main()
