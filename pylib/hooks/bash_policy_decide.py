"""Decision-policy slice for the bash validator.

Extracted from ``validate-bash.py`` (issue #584) — owns the *decide*
slice: resolving the ``MYORG_BASH_DEFAULT`` env var, computing the
counterfactual ``would_action`` for the shadow log, and routing the raw
verdict through the optional ``_policy_overlay`` (graduated-autonomy
gate). Kept distinct from ``bash_policy_evaluate`` so the pure
pattern-matching code has no env-var or overlay coupling.
"""

from __future__ import annotations

import os
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


def resolve_default_action() -> str:
    """Resolve the default-action env var.

    Default is ``allow`` — deny+ask lists are the fence and anything
    that doesn't match either is silently approved. Set ``MYORG_BASH_DEFAULT=ask``
    to restore strict behavior (unknown commands require confirmation). Anything
    other than ``ask`` resolves to ``allow`` (defensive: typos don't silently
    tighten the fence in the wrong direction).
    """
    val = os.environ.get("MYORG_BASH_DEFAULT", "allow").strip().lower()
    return "ask" if val == "ask" else "allow"


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


__all__ = ["apply_policy_gate", "resolve_default_action", "compute_would_action"]
