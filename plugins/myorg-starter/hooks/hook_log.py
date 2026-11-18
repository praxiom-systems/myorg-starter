"""Shared logging helpers for getmyorg Python hooks.

Mirrors the shell-side helpers in ``hook-lib.sh`` so that event-log files
written by Python hooks (validate-bash, agent-teams-common) are
byte-identical to those written by shell hooks. Single source of truth
for log directory resolution, worktree-aware project naming, daily file
naming, sanitization, and the structured event block format.

Environment:
  MYORG_HOOK_LOG_DIR — log directory; empty disables logging.
                       Default: /tmp/claude-hook-logs
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

DEFAULT_LOG_DIR = "/tmp/claude-hook-logs"
SHADOW_LOG_NAME = "shadow.jsonl"
_WORKTREE_MARKER = "/.claude/worktrees/"


def resolve_log_dir() -> Path | None:
    """Return a safe log directory or None if logging is disabled.

    Honors ``MYORG_HOOK_LOG_DIR``: empty string disables logging entirely;
    unset falls back to ``/tmp/claude-hook-logs``. The directory is created
    with mode 0o700 if missing. Returns None if the resolved path is a
    symlink, not a directory, or not owned by the current uid — matching
    the safety contract in ``hook-lib.sh``.

    Note: ``mkdir`` runs under ``umask 0o077`` so a *new* dir lands at
    0o700, but we do not chmod a pre-existing dir to tighten its mode.
    The uid-ownership check above is the primary defense against tampering;
    a permissive but uid-owned dir is treated as the operator's choice.
    """
    env_val = os.environ.get("MYORG_HOOK_LOG_DIR")
    if env_val is not None and env_val == "":
        return None
    log_dir = Path(env_val) if env_val else Path(DEFAULT_LOG_DIR)
    try:
        old_umask = os.umask(0o077)
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        finally:
            os.umask(old_umask)
    except OSError:
        return None
    if log_dir.is_symlink() or not log_dir.is_dir():
        return None
    try:
        if hasattr(os, "getuid") and log_dir.stat().st_uid != os.getuid():
            return None
    except OSError:
        return None
    return log_dir


def project_from_cwd(cwd: str) -> str:
    """Worktree-aware project label.

    Byte-parity with ``hooklib_project_from_cwd`` in hook-lib.sh:
    paths under ``/.claude/worktrees/<agent>/...`` produce
    ``<repo>-<agent>``; otherwise the basename of cwd. Empty cwd
    returns ``"unknown"``.
    """
    if not cwd:
        return "unknown"
    if _WORKTREE_MARKER in cwd:
        before, after = cwd.split(_WORKTREE_MARKER, 1)
        agent = after.split("/", 1)[0]
        return f"{os.path.basename(before)}-{agent}"
    return os.path.basename(cwd)


def daily_log_file(log_dir: Path, cwd: str, *, now: datetime | None = None) -> Path:
    """Return today's log file path: ``<log_dir>/<YYYY-MM-DD-Aaa>-<project>.log``.

    Date is rendered with C locale (matches shell-side ``LC_ALL=C date``).
    """
    when = now or datetime.now()
    project = project_from_cwd(cwd)
    return log_dir / f"{when.strftime('%Y-%m-%d-%a')}-{project}.log"


def shadow_log_path(log_dir: Path) -> Path:
    """Return ``<log_dir>/shadow.jsonl`` — the shared shadow-decision log."""
    return log_dir / SHADOW_LOG_NAME


def sanitize_for_log(value: str) -> str:
    """Strip control chars for single-line logging.

    Mirrors ``hooklib_sanitize``: collapses \\n \\r \\t to space, then drops
    other C0 control characters (0x00-0x1F except space).
    """
    if not value:
        return ""
    translated = value.translate({0x0A: 0x20, 0x0D: 0x20, 0x09: 0x20})
    return "".join(ch for ch in translated if ord(ch) >= 0x20)


def log_event(
    log_dir: Path | None,
    cwd: str,
    event: str,
    subject: str = "",
    detail: str = "",
    *,
    now: datetime | None = None,
) -> None:
    """Append a structured event block to today's log file.

    Byte-identical to ``hooklib_log_event``: same banner rule, TIME/EVENT
    fields always present, SUBJECT/DETAIL only when non-empty, sanitized.
    Silent on log_dir=None (logging disabled) or any I/O failure.
    """
    if log_dir is None:
        return
    when = now or datetime.now()
    path = daily_log_file(log_dir, cwd, now=when)
    lines = [
        "========================================",
        f"TIME:   {when.strftime('%Y-%m-%d %H:%M:%S')}",
        f"EVENT:  {event}",
    ]
    if subject:
        lines.append(f"SUBJECT: {sanitize_for_log(subject)}")
    if detail:
        lines.append(f"DETAIL:  {sanitize_for_log(detail)}")
    lines.append("========================================")
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        return


def append_jsonl_row(path: Path, row: dict) -> None:
    """Append one JSON object as a line to ``path``. Silent on failure."""
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        return
