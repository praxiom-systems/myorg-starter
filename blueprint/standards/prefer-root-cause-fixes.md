---
name: Prefer Root-Cause Fixes
description: "In agent orgs, ship proper fixes that address causes — not band-aids that hide symptoms. Diagnose before patching."
scope: org-wide
source: https://github.com/praxiom-systems/getmyorg
license: MIT
visibility: public
---

# Prefer Root-Cause Fixes

In a human org, expedient shortcuts are sometimes worth it: humans are slow, review catches what shortcuts hide, and the cost of a proper fix can outweigh the cost of patching the symptom. **In an agent org, that math inverts.** Agents iterate fast, and downstream review of agent output is sparse and partial. A band-aid that a human reviewer would catch in a code review goes silently to main. Hidden problems compound. Suppressed errors fail later, in worse places. Disabled tests rot.

This standard makes the agent default explicit: **diagnose before patching, and fix the cause — not the symptom.**

Each rule below is tagged:
- **Principle** — cultural norm enforced through review and peer feedback

The principle/control distinction matters: the patterns this standard targets are judgment-quality, not syntactic. There is no single regex that catches a band-aid. Mechanical enforcement is intentionally narrow (see *Mechanical leverage* below).

---

## Definitions

- **Proper fix** — a change that addresses the cause of the failure. Minimum change required to make the cause go away.
- **Expedient** — a smaller-than-ideal but legitimate fix that addresses *part* of the cause and leaves the rest visible (failing test, open issue, logged warning). The remaining problem is *known and findable*.
- **Band-aid** — a change that makes the symptom disappear without addressing the cause. The remaining problem is *hidden*. Suppressed errors, disabled tests, swallowed exceptions, broadened types, hardcoded fallbacks, "TODO fix later" merged to main.

The line between expedient and band-aid is whether the unfixed part stays visible.

---

## Rules

1. **Diagnose Before Patching** — on any failure (test red, hook deny, runtime error, unexpected output), reproduce and locate the cause before changing code. State the cause in the commit, PR, or task update. `principle`

2. **Fix Causes, Not Symptoms** — fix the defect where it lives, not where it surfaces. If a value arrives wrong, trace it to its source rather than coercing it at the consumer. The proper fix is the *minimum change that addresses the cause* — not less, not more. `principle`

3. **No Silencing** — do not make a failure disappear without resolving it. This includes: suppressing or swallowing exceptions (`except: pass`, empty `catch {}`, broadening exception classes); coercing or broadening types to bypass type errors (`@ts-ignore`, `# type: ignore`, casting to `any`/`Any`); disabling tests, lints, or hooks (`it.skip`, `pytest.mark.skip`, `# noqa`, `eslint-disable`, `--no-verify`); adding fallbacks that mask the real path. Each of these is occasionally legitimate — when used, the diff must justify *why* and the underlying problem must remain visible (filed issue, logged warning, named exception class). `principle`

4. **Expedient OK, Band-aid Not** — if a proper fix is out of scope or out of authority, ship a smaller proper fix and **file the residual** (see *File-residual mechanic* below). Never ship a change that masks the unfixed part. The shape of the residual is: a GitHub issue with the diagnosis, the chosen expedient, and what the proper fix would be. `principle`

---

## Over-Engineering Boundary

This standard pushes toward the cause line, not past it.

- A proper fix is the **minimum change that addresses the cause**.
- It is **not** an opportunistic refactor, a defensive rewrite of nearby code, or a generalization for hypothetical future cases.
- Three similar lines is better than a premature abstraction.
- "While I'm here" cleanup is out of scope of a fix unless explicitly requested.

Over-engineering is a different failure mode than band-aiding, and its remedy is different — but both violate "minimum change that addresses the cause."

---

## User-Directed Expedient

A user may explicitly request a quick fix, hotfix, or temporary patch. **This is allowed**, with two requirements:

1. The agent labels the change as expedient in the commit and/or PR description.
2. The agent files the residual issue documenting the proper fix.

The user's authority overrides the default. What this standard rules out is the agent *electing* a band-aid on its own — not the user choosing one with eyes open.

---

## File-Residual Mechanic

When shipping an expedient, the agent must create a follow-up GitHub issue containing:

- **Diagnosis** — what the cause is, in 1–3 sentences.
- **Chosen expedient** — what was actually shipped, and why.
- **Proper fix** — what a complete fix would look like, including affected files or systems.
- **Why deferred** — out of scope, out of authority, blocked on dependency, time-bounded, etc.

Link the issue from the PR. The residual issue is what makes an expedient distinguishable from a band-aid: the unfixed part is *named, located, and findable*.

If the agent cannot create issues in the host (no auth, no repo permissions), it must surface the residual in the PR description under a `## Residual` heading with the same fields. The residual must not live only in commit history.

---

## Escalation Instead of Patching

If the cause is out of the agent's authority or scope, or is cross-cutting, or would require an irreversible change, **escalate with the diagnosis** rather than patch. The diagnosis is more valuable than a guess at a fix. Hand off to the role that owns the affected area. Do not attempt a band-aid in lieu of escalation.

---

## Mechanical Leverage

Two narrow controls are enforced at the diff boundary, aligned with this standard:

1. **Commit-bypass gate** — `git commit --no-verify` and `git push --no-verify` are gated through `[ask.*]` in `blueprint/hooks/bash-patterns.toml`, requiring user confirmation before bypassing pre-commit hooks. Bypassing hooks is one of the most common band-aid patterns and the most syntactically detectable.

2. **Edit-time band-aid detector** — A `PreToolUse` hook on `Edit`, `Write`, and `MultiEdit` scans added lines against `blueprint/hooks/band-aid-patterns.toml`. The high-precision subset of patterns from this standard that are syntactically unambiguous is enforced here:
   - **Deny** (stops the edit at the `strict` guard level; a note at `guarded`, the default): bare `except:`, `except Exception: pass`, file-level `/* eslint-disable */`, naked `// @ts-ignore` without a justification comment.
   - **Advise** (non-blocking, surfaces in transcript): unlinked `TODO`/`FIXME`/`HACK`/`XXX`, line-scoped suppressions (`# noqa`, `# type: ignore`, `// eslint-disable-next-line`, `# pylint: disable`, `# pragma: no cover`), skipped tests (`it.skip`, `@pytest.mark.skip`, etc.).
   - Legitimate uses are accommodated: `@ts-ignore` with a trailing explanation, `TODO(#NNN)` with an issue link, `it.skip` in test files while iterating. The hook gates the *unjustified* form, not the justified one.
   - The `relaxed` and `off` guard levels (`/myorg:guards`) skip the scan.

Most band-aid patterns require judgment to distinguish from legitimate uses. Mechanical enforcement is intentionally narrow — the patterns above are the highest-signal, lowest-false-positive subset. Broader detection (JS empty `catch {}`, semantic type-coercion, hardcoded fallbacks) relies on review and self-discipline.

---

## Why This Matters in Agent Orgs

The cost ratios that justify shortcuts in human orgs do not hold in agent orgs:

| | Human org | Agent org |
|---|---|---|
| Cycle time | Slow | Fast |
| Review density | High (humans read every diff) | Low (sparse, partial) |
| Cost of proper fix | Often dominates schedule | Marginal — agents are cheap |
| Cost of band-aid | Recoverable in next sprint | Compounds silently across many fast iterations |
| Memory of "we patched this" | Persists in team knowledge | Evaporates between sessions |

Shortcuts that humans take and remember to come back to, agents take and forget. Proper fixes by default is not perfectionism — it is the only stable equilibrium for fast-iterating, sparsely-reviewed work.
