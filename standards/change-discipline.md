---
name: Change Discipline
description: "Atomic commits and scoped fixes: one reason to revert per commit, advisory bugfix size ceilings, and explicit override markers"
scope: org-wide
source: https://github.com/praxiom-systems/getmyorg
license: MIT
visibility: public
---

# Change Discipline

Every commit should have one reason to revert. A clean history is not decoration; it is the rollback surface for agent-authored work. When a commit bundles unrelated fixes, refactors, generated churn, and opportunistic cleanup, the next agent cannot safely bisect, revert, or review it.

This standard sits below [`agent-authored-changes`](agent-authored-changes.md): PRs are decomposed by bounded context and blast radius; commits are decomposed by atomic intent.

Each rule below is tagged:
- **Principle** — enforced through review, peer feedback, and advisory automation where the signal is strong enough

---

## Rules

1. **One Reason to Revert** — a commit should be expressible as one behavior, policy, documentation, or test change. If the revert explanation needs "and" to be accurate, split it. `principle`

2. **One Fix Per Bugfix Commit** — a bugfix commit addresses one defect path: one failing behavior, one root cause, one verification story. Adjacent cleanup belongs in a follow-up commit unless it is required to make the fix work. `principle`

3. **Separate Refactor From Behavior Change** — refactors, renames, formatting, and mechanical moves should be separate from functional changes unless the behavior change cannot be made safely without them. `principle`

4. **Keep Generated Churn Attached to Its Source** — generated files, lockfiles, snapshots, and compiled manifests travel with the source change that caused them. They do not count as a second reason to revert, but they must not be hand-edited. `principle`

5. **Bugfix Size Ceiling** — bugfix commits should stay under the product's advisory ceiling for non-generated changed lines. Oversized fixes require either decomposition or an explicit override marker in the commit body. `principle`

6. **Explicit Override Marker** — when a larger atomic commit is correct, acknowledge it with `Change-Discipline: allow` in the commit body and explain why splitting would be worse. `principle`

---

## Advisory Hook

The distributed `commit-discipline` git hook runs at `commit-msg`. It reads the commit subject and staged diff, then warns when a commit appears too broad:

- bugfix subject over the non-generated line ceiling
- bugfix subject touching too many non-generated files or top-level areas
- subject containing "and", which is often a bundled-intent smell
- very large non-generated staged diffs

The hook is advisory and exits successfully after printing guidance. It is meant to create friction and visibility, not to make git history unrecoverable during local work.

---

## Over-Application Boundary

Atomic does not mean tiny. A schema change plus generated types can be one commit when the generated files derive directly from the schema. A bugfix plus a regression test is one commit because the test proves the fix. A large but single-cause patch can remain one commit when splitting it would create broken intermediate states.

The standard rejects mixed intent, not meaningful size.
