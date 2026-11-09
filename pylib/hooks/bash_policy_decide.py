"""Decision-policy slice for the bash validator.

Owns the *decide* slice: resolving the operator's ``hook_strictness``
selection (env var or .myorg/config.yaml), computing the
counterfactual ``would_action`` for the shadow log, and routing the
raw verdict through the optional ``_policy_overlay`` (graduated-
autonomy gate). Kept distinct from ``bash_policy_evaluate`` so the
pure pattern-matching code has no env-var or overlay coupling.

The lever is the guard level, ``autonomy.hook_strictness`` in
``.myorg/config.yaml`` (``MYORG_HOOK_STRICTNESS`` overrides it for tests).
"""

from __future__ import annotations

import os
import re
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


# Guard levels, strictest first (#1089). Any preset may pick any level; there
# is no price ceiling. ``guarded`` is the default whenever nothing valid is
# configured: no .myorg/config.yaml, an unreadable one, an unknown value, or a
# folder that never ran /myorg:init.
LEVELS = ("strict", "guarded", "relaxed", "off")
DEFAULT_LEVEL = "guarded"

__all__ = [
    "DEFAULT_LEVEL",
    "LEVELS",
    "apply_policy_gate",
    "compute_would_action",
    "read_level_from_text",
    "resolve_hook_strictness",
    "strictness_to_default_action",
]

_TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:(.*)$")
_LEVEL_KEY = re.compile(r"^\s+hook_strictness\s*:\s*(.*)$")
_FLOW_LEVEL = re.compile(r"[{,]\s*hook_strictness\s*:\s*([^,}]*)")


def _scalar(raw: str) -> str:
    value = raw.split(" #", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip().lower()


def read_level_from_text(text: str) -> str | None:
    """Return ``autonomy.hook_strictness`` from config.yaml text, or None.

    A small line reader instead of PyYAML: a user's python3 often doesn't
    have it, and a missing module must not silently change the level. It
    understands the block form (``autonomy:`` then an indented key) and the
    flow form (``autonomy: {hook_strictness: x}``).
    """
    in_autonomy = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        top = _TOP_KEY.match(line)
        if top:
            in_autonomy = top.group(1) == "autonomy"
            rest = top.group(2).strip()
            if in_autonomy and rest.startswith("{"):
                flow = _FLOW_LEVEL.search(rest)
                return _scalar(flow.group(1)) if flow else None
            continue
        if in_autonomy:
            key = _LEVEL_KEY.match(line)
            if key:
                return _scalar(key.group(1))
    return None


def _config_path(cwd: str) -> Path | None:
    """The .myorg/config.yaml that governs cwd, or None.

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
            cfg = parent / ".myorg" / "config.yaml"
            return cfg if cfg.is_file() else None
    for parent in chain:
        cfg = parent / ".myorg" / "config.yaml"
        if cfg.is_file():
            return cfg
    return None


def _config_level(cwd: str) -> str | None:
    """Level from the .myorg/config.yaml that governs cwd, or None."""
    if not cwd:
        return None
    cfg = _config_path(cwd)
    if cfg is None:
        return None
    try:
        return read_level_from_text(cfg.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def resolve_hook_strictness(cwd: str | None = None) -> str:
    """Resolve the guard level for a hook running in ``cwd``.

    Order: ``MYORG_HOOK_STRICTNESS`` (tests and short-lived overrides), then
    ``autonomy.hook_strictness`` in the nearest .myorg/config.yaml, then
    ``guarded``. An invalid value at either source falls through to the
    next one rather than tightening or loosening silently.
    """
    env_val = os.environ.get("MYORG_HOOK_STRICTNESS", "").strip().lower()
    if env_val in LEVELS:
        return env_val
    configured = _config_level(cwd or os.getcwd())
    if configured in LEVELS:
        return configured
    return DEFAULT_LEVEL


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
