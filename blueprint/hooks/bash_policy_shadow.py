"""Shadow-log slice for the bash validator.

Extracted from ``validate-bash.py`` (issue #584) — owns the *shadow log*
slice: sanitizing commands for the JSONL audit row, hashing the repo
path, and writing the row through ``hook_log``. Failures are silent —
the shadow log is observability-only and must never affect the
permission decision returned to the host.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_log

# Whitespace-separated tokens longer than this are hashed in the shadow
# log so long bearer tokens / base64 blobs don't leak into observability
# storage while the command shape stays recognizable.
_TOKEN_HASH_THRESHOLD = 32


def sanitize_command(cmd: str) -> str:
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


def hash_repo(cwd: str) -> str | None:
    """Return a stable opaque hash of the repo path, or None if no cwd."""
    if not cwd:
        return None
    return hashlib.sha256(cwd.encode("utf-8", errors="replace")).hexdigest()[:12]


def build_shadow_row(
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
        "repo_hash": hash_repo(cwd),
        "agent_slug": os.environ.get("MYORG_ACTIVE_AGENT"),
        "profile": os.environ.get("MYORG_ACTIVE_PROFILE"),
        "cmd_sanitized": sanitize_command(command),
        "cmd_sha256": hashlib.sha256(
            command.encode("utf-8", errors="replace")
        ).hexdigest(),
        "matched_rule": matched_rule,
        "matched_category": matched_category,
        "would_action": would_action,
        "actual_action": actual_decision,
        "default_mode": default_mode,
    }


def write_shadow_row(row: dict) -> None:
    """Append one JSONL row to the shadow log. Failures are silent."""
    log_dir = hook_log.resolve_log_dir()
    if log_dir is None:
        return
    hook_log.append_jsonl_row(hook_log.shadow_log_path(log_dir), row)


__all__ = ["sanitize_command", "hash_repo", "build_shadow_row", "write_shadow_row"]
