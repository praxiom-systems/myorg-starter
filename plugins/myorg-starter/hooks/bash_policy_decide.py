"""Decision-policy slice for the bash validator.

Owns the *decide* slice: resolving the operator's ``hook_strictness``
selection (env var or .myorg/config.yaml), computing the
counterfactual ``would_action`` for the shadow log, and routing the
raw verdict through the optional ``_policy_overlay`` (graduated-
autonomy gate). Kept distinct from ``bash_policy_evaluate`` so the
pure pattern-matching code has no env-var or overlay coupling.

The lever is the guard level, the user's ``autonomy.hook_strictness`` (#1178):
the repo's gitignored ``.myorg/config.local.yaml``, then the user's
``config.yaml`` next to their getmyorg credential (``MYORG_HOOK_STRICTNESS``
overrides both for tests). The committed ``.myorg/config.yaml`` isn't read.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from _policy_overlay import apply_policy_gate  # type: ignore[import-not-found]
except ImportError:
    def apply_policy_gate(  # noqa: D401
        decision: str,
        reason: str,
        matched_category: str,
        matched_rule: str | None,
        cwd: str,
    ) -> tuple[str, str, str, str | None]:
        """Public starter: policy overlay not present; passthrough."""
        return decision, reason, matched_category, matched_rule


# Guard levels, strictest first (#1089). ``relaxed`` is the default whenever
# nothing valid is set (#1178): no settings file, an unreadable one, or an
# unknown value.
LEVELS = ("strict", "guarded", "relaxed", "off")
DEFAULT_LEVEL = "relaxed"

# Autonomy profiles, least autonomous first (config.ProfileLadder in Go).
PROFILES = ("assisted", "structured", "directed", "autonomous")

# Gate mode (#1168), tightest first: with it on, commands no rule calls
# routine ask the user through the host's permission prompt.
GATES = ("on", "off")
DEFAULT_GATE = "off"

__all__ = [
    "DEFAULT_GATE",
    "DEFAULT_LEVEL",
    "GATES",
    "LEVELS",
    "PROFILES",
    "apply_policy_gate",
    "compute_would_action",
    "read_autonomy_key",
    "read_level_from_text",
    "repo_override",
    "resolve_gate",
    "resolve_hook_strictness",
    "strictness_to_default_action",
    "user_config_path",
]

_TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:(.*)$")



def _scalar(raw: str) -> str:
    value = raw.split(" #", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip().lower()


def read_autonomy_key(text: str, key: str) -> str | None:
    """Return ``autonomy.<key>`` from config text, or None.

    A small line reader instead of PyYAML: a user's python3 often doesn't
    have it, and a missing module must not silently change the level. It
    understands the block form (``autonomy:`` then an indented key) and the
    flow form (``autonomy: {hook_strictness: x}``).
    """
    block_key = re.compile(r"^\s+" + re.escape(key) + r"\s*:\s*(.*)$")
    flow_key = re.compile(r"[{,]\s*" + re.escape(key) + r"\s*:\s*([^,}]*)")
    in_autonomy = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        top = _TOP_KEY.match(line)
        if top:
            in_autonomy = top.group(1) == "autonomy"
            rest = top.group(2).strip()
            if in_autonomy and rest.startswith("{"):
                flow = flow_key.search(rest)
                return _scalar(flow.group(1)) if flow else None
            continue
        if in_autonomy:
            m = block_key.match(line)
            if m:
                return _scalar(m.group(1))
    return None


def read_level_from_text(text: str) -> str | None:
    """Return ``autonomy.hook_strictness`` from config text, or None."""
    return read_autonomy_key(text, "hook_strictness")


def _myorg_dir(cwd: str) -> Path | None:
    """The .myorg/ dir that governs cwd, or None.

    Inside a git repo, only the repo root's counts — the same anchor the Go
    side uses — so a nested .myorg/ can't shadow the level. Outside git, the
    nearest one at or above cwd.
    """
    try:
        cur = Path(cwd).resolve()
    except OSError:
        return None
    chain = [cur, *cur.parents]
    for parent in chain:
        if (parent / ".git").exists():
            d = parent / ".myorg"
            return d if d.is_dir() else None
    for parent in chain:
        d = parent / ".myorg"
        if d.is_dir():
            return d
    return None


def user_config_path() -> Path:
    """The user's getmyorg config, next to their credential.

    ``$XDG_CONFIG_HOME/getmyorg/config.yaml``, else
    ``~/.config/getmyorg/config.yaml``: the path
    ``config.UserConfigPath`` uses in Go.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "getmyorg" / "config.yaml"


def _file_key(path: Path | None, key: str) -> str | None:
    if path is None:
        return None
    try:
        return read_autonomy_key(path.read_text(encoding="utf-8", errors="replace"), key)
    except OSError:
        return None


def _git_tracks(path: Path) -> bool:
    """Whether git tracks ``path``. Unsure (no git, a timeout) is no."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path.parent), "ls-files", "--error-unmatch", "--", path.name],
            capture_output=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def repo_override(cwd: str | None) -> tuple[Path | None, Path | None]:
    """``(override, ignored)``: the repo's .myorg/config.local.yaml when it
    counts, and the one ignored because git tracks it.

    The override is the user's own, for one repo. A file committed to the
    repo came with the checkout, not from this user, so it can't change
    their settings: a repo shipping ``gate: off`` or ``hook_strictness: off``
    is ignored (#1182 review). A file that isn't there is neither.
    """
    myorg = _myorg_dir(cwd or os.getcwd())
    path = myorg / "config.local.yaml" if myorg else None
    if path is None or not path.is_file():
        return None, None
    if _git_tracks(path):
        return None, path
    return path, None


def _resolve(cwd: str | None, key: str, env: str, allowed: tuple[str, ...], default: str) -> str:
    """``env``, then the repo's config.local.yaml, then the user's config,
    then ``default``. An invalid value at any source falls through."""
    env_val = os.environ.get(env, "").strip().lower()
    if env_val in allowed:
        return env_val
    for path in (repo_override(cwd)[0], user_config_path()):
        configured = _file_key(path, key)
        if configured in allowed:
            return configured
    return default


def resolve_hook_strictness(cwd: str | None = None) -> str:
    """Resolve the guard level for a hook running in ``cwd``.

    Order: ``MYORG_HOOK_STRICTNESS`` (tests and short-lived overrides), then
    the repo's ``.myorg/config.local.yaml``, then the user's config, then
    ``relaxed``. An invalid value at any source falls through to the next
    one rather than tightening or loosening silently.
    """
    return _resolve(cwd, "hook_strictness", "MYORG_HOOK_STRICTNESS", LEVELS, DEFAULT_LEVEL)


def resolve_gate(cwd: str | None = None) -> str:
    """Resolve gate mode (``on`` or ``off``) for a hook running in ``cwd``.

    Same order as the guard level: ``MYORG_HOOK_GATE``, the repo's
    ``.myorg/config.local.yaml``, the user's config, then ``off``.
    """
    return _resolve(cwd, "gate", "MYORG_HOOK_GATE", GATES, DEFAULT_GATE)


def strictness_to_default_action(strictness: str) -> str:
    """Map a guard level to the evaluator's no-match ``default_action``.

    Only ``strict`` bounces commands that match no rule; every other level
    lets them through to Claude Code's own permission rules.
    """
    return "ask" if strictness == "strict" else "allow"


def compute_would_action(
    actual_decision: str, matched_category: str, default_action: str
) -> str:
    """Compute what the decision would have been under the *other* default.

    The flip only matters when the load-bearing path was the default
    fallback — deny/ask/allow matches are stable across the flip.
    """
    if matched_category != "default":
        return actual_decision
    return "ask" if default_action == "allow" else "allow"
