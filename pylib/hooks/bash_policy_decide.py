"""Decision-policy slice for the bash validator.

Owns the *decide* slice: resolving the operator's ``hook_strictness``
selection (env var or .myorg/config.yaml), computing the
counterfactual ``would_action`` for the shadow log, and routing the
raw verdict through the optional ``_policy_overlay`` (graduated-
autonomy gate). Kept distinct from ``bash_policy_evaluate`` so the
pure pattern-matching code has no env-var or overlay coupling.

Per #923 the legacy ``MYORG_BASH_DEFAULT`` env var is removed. The
strictness lever is now ``hook_strictness`` — read from
``.myorg/config.yaml`` under ``autonomy.hook_strictness`` (with
``MYORG_HOOK_STRICTNESS`` available as an explicit override for tests).
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


_VALID_STRICTNESS = {"strict", "balanced", "lax"}
_VALID_PROFILES = {"assisted", "structured", "directed", "autonomous"}
_STRICTNESS_RANK = {"strict": 0, "balanced": 1, "lax": 2}

# Per-preset hook_strictness ceiling. Kept in lockstep with the
# `orgs[*].hook_strictness_ceiling` declarations in the org-blueprint
# (single source of truth in the model layer) and the design table in
# #923. Duplicated here so the hook can clamp WITHOUT loading the full
# blueprint at hook time — the hook is a child process and cold-loading
# the blueprint on every command would add real latency to every Bash
# invocation. lint-blueprint.py catches drift between this table and
# the blueprint via the ceiling enum check; the runtime resolver here
# is the gate that actually fires when commands run.
_PRESET_HOOK_STRICTNESS_CEILING = {
    "sovereign": "balanced",
    "lean":      "balanced",
    "focused":   "lax",
    "scaled":    "lax",
}


def _read_config(cwd: str) -> dict:
    """Walk up from cwd looking for .myorg/config.yaml and return the
    parsed mapping. Returns an empty dict when no config is found, the
    YAML is malformed, or PyYAML is unavailable.

    Best-effort: any IO or parse error returns {} and lets callers fall
    back to defaults rather than crashing the hook on a corrupt file.
    """
    if not cwd:
        return {}
    try:
        import yaml  # local import — keep startup lean
    except Exception:
        return {}
    cur = Path(cwd).resolve()
    for parent in [cur, *cur.parents]:
        cfg = parent / ".myorg" / "config.yaml"
        if not cfg.is_file():
            continue
        try:
            data = yaml.safe_load(cfg.read_text()) or {}
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _ceiling_for_preset(preset: str | None) -> str:
    """Return the hook_strictness ceiling for a preset slug.

    Unknown / missing preset falls back to ``balanced`` — the safest
    ceiling that doesn't unnecessarily widen on a typo, but also doesn't
    deny everything by clamping to ``strict`` and forcing reflection
    on every Bash call.
    """
    if not preset:
        return "balanced"
    return _PRESET_HOOK_STRICTNESS_CEILING.get(preset.strip().lower(), "balanced")


def _clamp_strictness(selected: str, ceiling: str) -> str:
    """Clamp `selected` so it is no looser (higher rank) than `ceiling`.

    Strictness ranks low-to-high (looser = higher rank): strict < balanced < lax.
    Clamping picks the stricter (lower-rank) of the two values, never
    the looser. So `lax` selected with a `balanced` ceiling → `balanced`.
    """
    s = _STRICTNESS_RANK.get(selected, _STRICTNESS_RANK["strict"])
    c = _STRICTNESS_RANK.get(ceiling, _STRICTNESS_RANK["balanced"])
    if s > c:
        return ceiling
    return selected


def resolve_hook_strictness(cwd: str | None = None) -> str:
    """Resolve the operator-selected hook strictness, clamped to the
    preset's ceiling (#923 review #1).

    Order:
      1. ``MYORG_HOOK_STRICTNESS`` env var (testing override). Still
         clamped — the env var cannot widen past the preset ceiling.
      2. ``autonomy.hook_strictness`` in ``.myorg/config.yaml``.
      3. Inferred from ``autonomy.profile`` in the same config.
      4. Failure-closed default: ``strict``.

    The clamp is the bug-fix: previously a sovereign repo could set
    ``autonomy.hook_strictness: lax`` and bypass the design's ceiling
    table because the hook did not consult the preset. Now the hook
    walks the same config-resolution path the Go side uses and applies
    the same ceiling. Invalid values (typos) still fall through to the
    next source rather than silently widening the fence.
    """
    data = _read_config(cwd or os.getcwd())
    preset = ""
    if isinstance(data, dict):
        preset_val = data.get("preset")
        if isinstance(preset_val, str):
            preset = preset_val.strip().lower()
    ceiling = _ceiling_for_preset(preset)

    env_val = os.environ.get("MYORG_HOOK_STRICTNESS", "").strip().lower()
    if env_val in _VALID_STRICTNESS:
        return _clamp_strictness(env_val, ceiling)

    autonomy = (data.get("autonomy") or {}) if isinstance(data, dict) else {}
    explicit = (autonomy.get("hook_strictness") or "").strip().lower()
    if explicit in _VALID_STRICTNESS:
        return _clamp_strictness(explicit, ceiling)

    profile = (autonomy.get("profile") or "").strip().lower()
    if profile in _VALID_PROFILES:
        inferred = "strict" if profile == "assisted" else "balanced"
        return _clamp_strictness(inferred, ceiling)

    return "strict"


def strictness_to_default_action(strictness: str) -> str:
    """Map a resolved hook strictness to the evaluator's internal
    ``default_action`` parameter.

    The evaluator's contract still uses the legacy "ask"/"allow"
    vocabulary internally; the boundary translates "ask" verdicts to
    Claude Code's ``deny`` with a reflection prompt (see
    ``entries/validate_bash.py:output_decision``). So strict and
    balanced both map to "ask"; only lax maps to "allow".
    """
    if strictness == "lax":
        return "allow"
    return "ask"


# Compatibility shim — older call sites may still reach for this name.
# Resolves to the same value the new pipeline emits.
def resolve_default_action(cwd: str | None = None) -> str:
    """Deprecated: use ``resolve_hook_strictness`` + ``strictness_to_default_action``."""
    return strictness_to_default_action(resolve_hook_strictness(cwd))


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


__all__ = [
    "apply_policy_gate",
    "resolve_hook_strictness",
    "resolve_default_action",
    "strictness_to_default_action",
    "compute_would_action",
]
