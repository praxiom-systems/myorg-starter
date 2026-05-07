---
name: Silent Failure Standard
description: "Caller-visible failures must be surfaced explicitly with what happened, why it matters, and what to do next."
scope: org-wide
source: "https://github.com/praxiom-systems/getmyorg"
license: MIT
visibility: public
---

# Silent Failure Standard

This standard extends the org-wide "No Silent Failure" rule with concrete output requirements for code and reviews.

## Rules

1. **No swallowed caller-visible failures.** If a step fails, degrades, or is skipped in a way that changes user trust, completeness, or correctness, surface it in the caller-facing output. Logging alone is insufficient. `control`

2. **Use the three-part contract.** Every surfaced failure must tell the caller:
   1. what happened
   2. why it matters
   3. what to do next
   `control`

3. **Structured surfaces stay structured.** MCP tool failures return `isError: true`. HTTP failures return a stable machine-readable error code plus a human-readable message. Generated artifacts expose degraded steps and warnings as explicit fields, not just stderr logs. `control`

4. **Degraded success must still confess.** If a flow continues after a non-fatal failure, the successful result must include the degraded step names or warnings so consumers do not mistake partial output for complete output. `control`

5. **Review for silence on critical paths.** PR review for changes touching `server/`, `worker/`, or `pylib/` must include a silent-failure pass that looks for swallowed errors, generic 500s, empty fallbacks, and missing user guidance. `principle`

## Preferred Output Shape

- **CLI / markdown / text tools**
  - one-line summary of the failure or degraded step
  - why the result may be incomplete or stale
  - next command, file, or remediation step

- **HTTP APIs**
  - stable `error.code`
  - plain-English `error.message`
  - optional `error.next_action`
  - optional `error.retryable`

- **Generated artifacts**
  - `scan_warnings`
  - `degraded_steps`

## Review Checklist

1. Could this path fail and still look like success to the caller?
2. If a dependency is missing or unhealthy, does the response say what broke?
3. Does the response explain whether the result is empty, partial, stale, or wrong?
4. Is there a concrete next action for the user or operator?
