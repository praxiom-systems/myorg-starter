---
name: decompose-changeset
description: "When a body of work spans multiple contexts, reversibility classes, or contract boundaries, produce an ordered changeset plan — PR-sized stubs each declaring a contract boundary. Operationalizes agent-authored-changes."
agents:
  - tech-lead
  - product-owner
  - fullstack-engineer
source: "https://github.com/praxiom-systems/getmyorg"
license: MIT
visibility: public
---

# Decompose Changeset

A changeset plan is an ordered, contract-bounded sequence of PR stubs — each stub one bounded context, one reversibility class, one blast radius — that together deliver a planned body of work. Run this skill before the first commit on any body of work that spans more than one stub. It does not size work (see `estimate-work`) and does not shape the issue that triggers the work (see `agent-ready-issues`). It operationalizes the `agent-authored-changes` standard: that standard is the rule; this skill is the procedure for satisfying it systematically.

## When to Use

| Condition | Action |
|-----------|--------|
| Work touches more than one bounded context | **Use this skill** |
| Work spans more than one reversibility class | **Use this skill** |
| Work crosses a published contract boundary (schema, public API, MCP signature, hook interface, registry entry) | **Use this skill** |
| Work touches files under more than one of the planes listed in `agent-authored-changes` | **Use this skill** |
| Single-stub work passes the coherence test immediately | **Do not use** — commit directly |
| You only need to confirm that an existing plan is still valid | **Do not use** — re-read the plan and compare to the current map |

## Input

- Issue body, epic description, scope summary, or freeform plan (required)
- Change-scoped context availability assumed — `change_brief` is called first when paths or a concrete task are known; `map_overview` and `map_neighborhood` remain the fallback and deeper drilldown route
- Any existing partial plan or prior decomposition attempt (optional; surface conflicts explicitly)
- Org profile (Sovereign / Lean / Focused / Scaled) — determines whether a changeset plan is recommended or required per the standard's per-profile defaults table

## Output

- A fenced `changeset-plan` block (schema defined in § Output Format) written into the issue body, PR description, or posted as an issue comment
- Optionally: child `changeset-plan` blocks for any stub that triggered Recursive XL Handling (§ Recursive XL Handling), each referenced from its parent stub via a `decomposed_into:` key

## Workflow

### Phase 1 — Read change-scoped context

When the work names paths or a concrete task, call `change_brief` or `getmyorg change-brief` first. Use its affected areas, ownership, trust state, candidate files/spans, and verification hints as the starting context. For work that is still broad or area-shaped, call `map_overview` to understand the overall structure of the affected repo. For each area you expect to touch or that the brief identifies, call `map_neighborhood` with the area name when you need deeper architectural context. Do not begin decomposition without this first-pass context. Blind decomposition against an unfamiliar codebase produces stubs that cross undetected contract boundaries. If readiness reports `needs_full_refresh`, run `getmyorg project-map <dir>` before proceeding.

**Stop and report if:** the project map is unavailable, stale, or cannot identify the affected areas.

### Phase 2 — Enumerate touched contexts

List every distinct bounded context that the body of work touches. Write one sentence per context — no conjunctions. A bounded context is the unit a single PR's purpose statement covers: the change is expressible in one sentence without "and," and reverting it leaves the codebase in a coherent state. If your sentence requires a conjunction to cover two independent concerns, those are two contexts. Work through the full plan; do not stop at the first two or three obvious stubs. Contexts that surface only in Phase 4 (contract boundaries) are common — iterate back here if needed.

### Phase 3 — Classify reversibility per context

Tag each context with exactly one reversibility class from `agent-authored-changes`:

- `additive` — new code, new files, new configuration; nothing existing changes behavior
- `behavior-changing` — modifies how existing code behaves; callers or users may observe a difference
- `migration` — transforms existing data or structure; old and new states coexist during rollout
- `irreversible` — cannot be undone without out-of-band action; requires explicit human sign-off

If a single context carries two classes (for example: a new field added alongside a change to an existing field's semantics), split it into two contexts and return to Phase 2. Mixed-class stubs are not allowed.

### Phase 4 — Identify contract boundaries

For each pair of adjacent stubs, name the contract that separates them. A contract is a boundary another module or consumer reads: a schema, a public API surface, a hook interface, an MCP tool signature, a file-path plane, or a registry entry. Be explicit — "the `skills:` registry entry" is a contract boundary; "some YAML" is not.

**Structural anti-splitting check:** if two adjacent stubs share the same context tag and no contract separates them, they must be re-merged into one stub. Adjacent stubs with no boundary between them are mechanical splitting — the plan fails validation and must be revised before proceeding to Phase 5. This check exists to prevent plans that decompose by file count rather than by coherence.

**Stop and report if:** the work cannot be split without violating a contract boundary or creating a broken intermediate state.

### Phase 5 — Order and declare dependencies

For each stub, declare `depends_on` as a list of slug references to earlier stubs it requires. Build a directed acyclic graph (DAG) from these edges. A cycle means two stubs each require the other — re-decompose to break the cycle, typically by introducing a shared foundation stub that both can depend on instead.

Order the stubs so that every prefix of the sequence builds and passes tests cleanly. A mid-series stub that leaves the codebase in a broken intermediate state violates bisectability — work backwards from that stub to find the missing dependency or the incorrect ordering. Cross-plane stubs (touching more than one plane from `agent-authored-changes`) must also satisfy the `## Why one PR` requirement from that standard; declare the coupling in `cross_plane_rationale` on the stub.

### Phase 6 — Validate per stub, then emit

For each stub in order:

1. Apply the criteria from the `agent-ready-issues` **standard** to validate shape — the stub title and context must form a self-contained, executable unit. The standard is org-wide and inherited by every agent; there is no availability check.
2. Invoke the `estimate-work` **skill** to validate size — any stub returning XL re-enters this skill on that stub alone (see § Recursive XL Handling for depth limits and escalation rules).

When all stubs pass both validations, emit the fenced `changeset-plan` block as defined in § Output Format. Write it into the issue body or PR description, or post it as an issue comment if the work is tracked in an existing issue.

**Stop and report if:** any stub remains XL after the permitted recursive decomposition depth.

## Output Format

Emit a single fenced block using the `changeset-plan` language tag. Use YAML syntax. Markdown tables are rejected — field values are uneven length and tables render poorly when one column wraps.

Required top-level keys:

- `work` — one-sentence summary of the full body of work
- `profile` — org profile (informational; does not change stub shape)
- `stubs` — ordered list of stubs

Required keys per stub:

- `slug` — kebab-case identifier, unique within this plan
- `title` — one sentence, no conjunction; matches the PR title the agent will open
- `context` — one-phrase bounded context label (same vocabulary as Phase 2)
- `reversibility_class` — one of: `additive`, `behavior-changing`, `migration`, `irreversible`
- `blast_radius` — who is affected if this stub is reverted or misbehaves (one phrase)
- `contract_boundary` — the contract this stub crosses or introduces (name it explicitly)
- `depends_on` — list of slugs (empty list `[]` if none)

Optional keys per stub:

- `cross_plane_rationale` — required when the stub touches files under more than one plane from `agent-authored-changes`; explains why splitting is not feasible
- `decomposed_into` — slug of a child `changeset-plan` block produced by Recursive XL Handling

Example:

````
```changeset-plan
work: "<one-sentence summary of the body of work>"
profile: focused          # informational; does not change shape
stubs:
  - slug: pr1-skill-file
    title: "Add decompose-changeset skill file and registry entry"
    context: "blueprint asset addition"
    reversibility_class: additive
    blast_radius: "blueprint readers; no published contract crossed"
    contract_boundary: "registry entry — skills registry"
    depends_on: []
  - slug: pr2-agent-bindings
    title: "Bind decompose-changeset to tech-lead, product-owner, fullstack-engineer"
    context: "agent registry inheritance"
    reversibility_class: additive
    blast_radius: "three agent registry files"
    contract_boundary: "agent registry — required_skills field"
    depends_on: [pr1-skill-file]
```
````

The plan is the source of truth. Do not duplicate it as a markdown table or a prose list — divergence between representations introduces ambiguity.

## Recursive XL Handling

When Phase 6's `estimate-work` invocation returns XL for a stub, re-enter this skill on that stub alone. The recursion is bounded:

- **Depth 1** — first re-entry: decompose the XL stub into child stubs; emit a child `changeset-plan` block. Reference the child plan from the parent stub via `decomposed_into: <child-plan-slug>`. The child plan is a separate fenced block; do not mutate the parent plan in place.
- **Depth 2** — second re-entry: if the child stub still sizes XL after one level of decomposition, surface an `unbounded-decomposition` flag on that stub and stop recursing.

An `unbounded-decomposition` flag means the work resists decomposition at this level of understanding. The calling agent must escalate: in Focused or Scaled profile, escalate to tech-lead for a scoping decision before any code is written. In Sovereign or Lean profile, annotate the stub with `accepted_risk: true` and a one-sentence justification — the calling agent takes accountability for the oversized unit.

Do not silently absorb a depth-2 XL. Surface the flag.

## Composition Seams

Phase 6 has two sibling validations, with intentional asymmetry: `agent-ready-issues` is a **standard** (org-wide, inherited by every agent automatically — no availability check needed) and `estimate-work` is a **skill** (bound per-agent via the `agents:` field in the skill registry — availability is the membership of the calling agent in that list).

**Shape validation** — apply the criteria from the `agent-ready-issues` standard inline. The standard is always in scope; if the criteria fail on a stub, fix the stub before emit.

**Size validation** — invoke the `estimate-work` skill. If the calling agent is **not** bound to `estimate-work` in the skill registry (today: tech-lead, product-owner, fullstack-engineer), do not silently skip:

1. Emit the plan with a top-level `validation: unverified` key.
2. Surface a warning in the output naming the missing seam (`estimate-work`) and which agent should be invoked to complete sizing.

Example:

```changeset-plan
work: "..."
profile: lean
validation: unverified    # calling agent is not bound to estimate-work in the skill registry
stubs: [...]
```

A plan with `validation: unverified` is provisional. It must be size-validated by a bound agent before the first stub's PR is opened.

## Worked Examples

---

### Example 1 — Issue #394: agent-authored-changes standard + decompose-changeset skill

**Body of work:** Add the `agent-authored-changes` org-wide standard and the `decompose-changeset` skill that operationalizes it. Filed as a single epic; required decomposition before any code was written.

**Phase 1 — map check:** `map_overview` shows the repo has a `blueprint/` plane (distributable assets) and separate agent registry files. `map_neighborhood` for the standards and skills areas confirms they are independent planes with separate contract boundaries (the `standards:` registry key and the `skills:` registry key).

**Phase 2 — contexts:** Two contexts surface immediately:

1. Standard file + registry registration + enforcement note in `agent-authored-changes.md` — the output-side rule
2. Skill file + registry registration + agent bindings — the procedure that operationalizes the rule

**Phase 3 — reversibility:** Both are `additive` — new files, new registry entries, no existing behavior changed.

**Phase 4 — contract boundary:** The contract separating PR1 and PR2 is the registry entry (`standards:` key for PR1; `skills:` key for PR2). PR2 also introduces the `required_skills` binding in three agent YAML files — a second contract boundary within PR2, but internal to PR2 since all three files are additive reads of the same registry key.

**Phase 5 — order:** PR1 must land first because PR2's skill body references `agent-authored-changes` as the rule it operationalizes. PR2 depends on PR1.

**Emitted plan:**

```changeset-plan
work: "Add agent-authored-changes standard and decompose-changeset skill"
profile: focused
stubs:
  - slug: pr1-standard
    title: "Add agent-authored-changes org-wide standard"
    context: "blueprint standard addition"
    reversibility_class: additive
    blast_radius: "blueprint readers; references in agent-authored-changes.md"
    contract_boundary: "registry entry — standards registry"
    depends_on: []
  - slug: pr2-skill
    title: "Add decompose-changeset skill and bind to tech-lead, product-owner, fullstack-engineer"
    context: "blueprint skill addition with agent registry bindings"
    reversibility_class: additive
    blast_radius: "three agent registry files; blueprint readers"
    contract_boundary: "registry entries — skills registry and agent required_skills fields"
    depends_on: [pr1-standard]
```

---

### Example 2 — Hypothetical: cross-host delegation epic (#328 style)

**Body of work:** Add cross-host delegation support — peer CLI subprocess delegation, API fallback when the CLI is absent, and org-map tooling to surface the peer graph. Modelled on #328's actual decomposition.

**Phase 1 — map check:** `map_overview` shows the repo spans a Go MCP server, skill definitions, and a Python compiler. `map_neighborhood` for the server internals surfaces the `delegate-to-peer` skill path and the org-map package as distinct modules with their own interfaces.

**Phase 2 — contexts:** Three contexts:

1. The `delegate-to-peer` skill procedure file and its registry entry — blueprint plane only
2. The Go subprocess delegation handler and API fallback path — server plane only
3. The org-map aggregate tooling that reads the peer graph — server + pylib planes (cross-plane)

**Phase 3 — reversibility:** Stubs 1 and 2 are `additive`. Stub 3 is `behavior-changing` — the org-map output format changes for consumers already reading `org.json`.

**Phase 4 — contract boundary:** Stub 1 → Stub 2: the skill procedure references the Go handler's MCP tool signature — that signature is the contract. Stub 2 → Stub 3: the org-map output schema (`org.json`) is the contract; Stub 3 extends it with peer-graph edges.

**Phase 5 — order:** Stub 1 first (no dependencies). Stub 2 depends on Stub 1 (implements what the skill describes). Stub 3 depends on Stub 2 (reads the peer graph the handler produces).

**Phase 6 — validation:** Stub 3 returns XL on `estimate-work`. Recursive XL Handling (depth 1) decomposes it into two child stubs: one that adds the org-map schema extension, one that adds the rendering path. Both size at L.

**Emitted plan (abbreviated):**

```changeset-plan
work: "Add cross-host delegation — skill, subprocess handler, API fallback, and org-map peer graph"
profile: scaled
stubs:
  - slug: s1-skill
    title: "Add delegate-to-peer skill file and registry entry"
    context: "blueprint skill addition"
    reversibility_class: additive
    blast_radius: "blueprint readers"
    contract_boundary: "registry entry — skills registry"
    depends_on: []
  - slug: s2-handler
    title: "Add subprocess delegation handler and API fallback in Go server"
    context: "server delegation implementation"
    reversibility_class: additive
    blast_radius: "MCP tool consumers reading the delegate tool signature"
    contract_boundary: "MCP tool signature — delegate-to-peer tool"
    depends_on: [s1-skill]
  - slug: s3-orgmap
    title: "Add org-map peer graph tooling (schema extension and renderer)"
    context: "org-map output extension"
    reversibility_class: behavior-changing
    blast_radius: "consumers reading org.json — org.json schema change"
    contract_boundary: "org.json output schema"
    cross_plane_rationale: "Schema extension in the org-map package and rendering path in the compiler are load-bearing together — landing the schema without the renderer leaves org.json invalid for readers"
    depends_on: [s2-handler]
    decomposed_into: s3-orgmap-child
```

## Guardrails

- Never emit a plan where adjacent stubs share a context tag with no contract boundary between them — that is mechanical splitting, not coherent decomposition; re-merge those stubs
- Never recurse beyond depth 2 silently — surface the `unbounded-decomposition` flag and escalate
- Cross-plane stubs (touching more than one plane listed in `agent-authored-changes`) must populate `cross_plane_rationale` — do not omit it
- A stub whose `contract_boundary` is described as "the cross-plane coupling itself" is not a valid boundary — the coupling is not a contract; split the stub
- Never silently skip the per-stub validation seams in Phase 6 — if `agent-ready-issues` or `estimate-work` is unavailable, emit `validation: unverified` and name the missing skill in a warning
- The `changeset-plan` block is the source of truth — do not duplicate it as a markdown table or prose list; divergence introduces ambiguity
- Do not restate or reproduce the plane list from `agent-authored-changes` — it lives there; reference it by name
- A plan that cannot be validated (both seams unavailable) must not be used to open PRs until a capable agent completes Phase 6
