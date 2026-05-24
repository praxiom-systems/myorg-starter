"""Per-session `mark_command_safe` registry (#923).

When the agent deliberates after a `[reflect.*]`-driven `deny`, it
records the outcome via the `mark_command_safe` MCP tool. The mark
captures both the exact command and a regex *generalization* so that
later commands of the same shape (different branch name, different
file, etc.) skip the reflection turn.

Marks live in ``<repo>/.myorg/marks.current.json`` — one file per repo,
refreshed at session boundaries. The Go MCP tool writes through the
same path so the hook and the tool agree on shape without an RPC.

All IO is best-effort: missing file, malformed file, or filesystem
errors degrade silently to "no marks" so the hook never fails open
because of an IO hiccup. The agent can always retry via reflection +
AskUserQuestion.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

MARKS_FILENAME = "marks.current.json"
_MAX_MARKS = 2048


def _find_repo_marks_path(cwd: str | None) -> Path | None:
    if not cwd:
        cwd = os.getcwd()
    cur = Path(cwd).resolve()
    for parent in [cur, *cur.parents]:
        myorg_dir = parent / ".myorg"
        if myorg_dir.is_dir():
            return myorg_dir / MARKS_FILENAME
    return None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_marks(cwd: str | None = None) -> list[dict[str, Any]]:
    """Return the list of active marks for the repo at cwd.

    Returns [] when no .myorg/ exists, the file is absent, or the file
    is malformed. Never raises.
    """
    path = _find_repo_marks_path(cwd)
    if path is None or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception:
        # Malformed file is operationally equivalent to "no marks" for
        # gating decisions — the agent will fall back to reflection.
        # Surfacing this only matters at debug time; the file is
        # rewritten on next mark_command_safe call.
        return []
    marks = data.get("marks") if isinstance(data, dict) else None
    if not isinstance(marks, list):
        return []
    return [m for m in marks if isinstance(m, dict)]


def find_mark_for_command(command: str, cwd: str | None = None) -> dict[str, Any] | None:
    """Return the first mark whose generalization matches the command.

    Falls back to exact-command match when generalization is missing or
    invalid. Returns None when no mark matches.
    """
    marks = load_marks(cwd)
    if not marks:
        return None
    for mark in marks:
        gen = mark.get("generalization")
        if isinstance(gen, str) and gen:
            try:
                if re.search(gen, command):
                    return mark
            except re.error:
                continue
        exact = mark.get("command")
        if isinstance(exact, str) and exact == command:
            return mark
    return None


def write_mark(
    command: str,
    generalization: str,
    classification: dict[str, Any],
    rationale: str,
    cwd: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    """Append a mark to the per-repo registry.

    Returns the persisted mark dict on success, or None when no .myorg/
    ancestor was found (no place to write).
    """
    path = _find_repo_marks_path(cwd)
    if path is None:
        return None
    mark = {
        "command": command,
        "generalization": generalization,
        "classification": classification,
        "rationale": rationale,
        "recorded_at": _now_iso(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict) and isinstance(data.get("marks"), list):
                existing = [m for m in data["marks"] if isinstance(m, dict)]
        except Exception:
            # Corrupt existing file: prefer rewriting over refusing.
            existing = []
    existing.append(mark)
    if len(existing) > _MAX_MARKS:
        existing = existing[-_MAX_MARKS:]
    payload = {
        "session_id": session_id,
        "updated_at": _now_iso(),
        "marks": existing,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)
    return mark


def reset_marks(cwd: str | None = None) -> bool:
    """Drop the marks file for this repo. Called at session boundaries.

    Returns False when no .myorg/ ancestor exists; True otherwise (file
    unlinked if present, no-op if already absent).
    """
    path = _find_repo_marks_path(cwd)
    if path is None:
        return False
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            # Best-effort: file already gone, or no permission. Either
            # way the next write_mark will recreate it from scratch.
            return True
    return True


__all__ = ["load_marks", "find_mark_for_command", "write_mark", "reset_marks"]
