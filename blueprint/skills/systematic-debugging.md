---
name: systematic-debugging
description: When a bug, test failure, or unexpected behavior appears, diagnose root cause before proposing or implementing fixes.
agents: []
source: https://github.com/obra/superpowers
license: MIT
visibility: public
---

# Systematic Debugging

Random fixes waste time and create new bugs. Symptom patches mask underlying issues that resurface elsewhere. This skill is the procedure that operationalizes the `prefer-root-cause-fixes` standard at the moment of failure.

**Iron law:** no fixes without root-cause investigation first.

If you have not completed Phase 1, you cannot propose a fix.

## When to Use

Any technical failure: test red, hook deny, runtime error, unexpected output, build failure, integration issue, performance regression. Especially when:

- Under time pressure (urgency makes guessing tempting — and wrong)
- A "just one quick fix" feels obvious
- You have already tried one or two fixes that did not work
- You do not fully understand what is failing

Do not skip when:
- The bug looks simple (simple bugs have causes too — diagnosis is fast)
- You are in a hurry (systematic is faster than thrashing)

## Input

- **Failure signal**: failing test, hook denial, runtime error, unexpected output, build failure, or performance regression
- **Reproduction context**: command, request, environment, or steps that trigger the failure
- **Recent change context**: relevant diff, commit, dependency, configuration, or environment changes

## Output

- **Root-cause statement**: one to three sentences naming the diagnosed cause
- **Targeted fix**: one cause-level change, with a failing test when practical
- **Verification evidence**: command output or observed behavior showing the symptom is gone and no relevant checks regressed

## The Four Phases

Complete each phase before moving to the next.

### Phase 1 — Root-Cause Investigation

1. **Read the error.** Read it completely — message, stack trace, line numbers, codes. Do not skim.
2. **Reproduce consistently.** Can you trigger it reliably? Exact steps? If not reproducible, gather more data — do not guess.
3. **Check recent changes.** `git log`, `git diff`, recent commits, dependency or config changes, environment differences.
4. **Trace data flow.** Where does the bad value originate? What called this with a bad value? Trace upward to the source. Fix at the source, not at the symptom.
5. **Instrument multi-component systems.** When the failure crosses boundaries (CI → build → deploy, API → service → DB), add diagnostic output at each boundary so the failing layer is identified by evidence, not by guess.

Output of Phase 1: a one- to three-sentence statement of the cause. If you cannot write it, you are not done with Phase 1.

**Stop and report if:** the failure cannot be reproduced, the root cause cannot be stated, or three attempted fixes have failed.

### Phase 2 — Pattern Analysis

1. Find similar code in the same codebase that works.
2. If implementing a known pattern, read the reference implementation completely. Do not skim.
3. List every difference between the working and broken cases — even small ones.
4. Identify the dependencies and assumptions of the pattern.

### Phase 3 — Hypothesis and Test

1. Form a single, specific hypothesis: "I think X is the cause because Y."
2. Test minimally — one variable at a time. Do not bundle changes.
3. Verify before continuing. If wrong, form a new hypothesis. Do not stack fixes on top of fixes.

### Phase 4 — Implementation

1. **Write a failing test** for the cause (use the test-driven-development discipline). The test must fail for the diagnosed reason — not just for any reason.
2. **Implement a single fix** at the cause. One change. No "while I'm here" cleanup. No bundled refactor.
3. **Verify** — failing test now passes, no other tests break, original symptom is gone.
4. **If the fix did not work**, return to Phase 1 with new information. Do not attempt fix #2 by changing something else.
5. **If three fixes have failed**, stop. Three failures usually mean the architecture is wrong, not the fix. Escalate or open a design conversation rather than attempt fix #4.

## Red Flags — Stop and Restart Phase 1

If you catch yourself thinking any of these, you are about to ship a band-aid:

- "Quick fix for now, investigate later."
- "Just try changing X and see if it works."
- "Skip the test, I will manually verify."
- "It is probably X — let me fix that."
- "I do not fully understand, but this might work."
- Proposing solutions before tracing data flow.
- Each fix reveals a new problem in a different place.

## Common Rationalizations

| Excuse | Reality |
|---|---|
| "Issue is simple, no need for process" | Simple bugs have causes too. Diagnosis is fast for simple bugs. |
| "Emergency, no time for process" | Systematic debugging is *faster* than guess-and-check. |
| "Just try this first, then investigate" | First fix sets the pattern. Do it right the first time. |
| "I will write the test after confirming the fix" | Untested fixes regress. Failing test first proves the fix landed. |
| "Multiple fixes at once saves time" | You cannot tell which one worked. Causes new bugs. |
| "I see the problem, let me fix it" | Seeing a symptom is not understanding a cause. |
| "One more attempt" (after 2+ failures) | Three failures means the architecture is wrong. Stop fixing. |

## When the Cause Truly Is External

Sometimes investigation reveals that the cause is environmental, timing-dependent, or external (rate limit, DNS, third-party flakiness). In that case:

1. Document what you investigated.
2. Implement appropriate handling — retry with backoff, timeout, explicit error message.
3. Add monitoring or logging so the next occurrence is debuggable.

But: most "no root cause" outcomes are incomplete investigations. Suspect yourself before suspecting the universe.

## Relationship to `prefer-root-cause-fixes` Standard

The `prefer-root-cause-fixes` standard is the *contract*: ship proper fixes, not band-aids. This skill is the *procedure* that makes the contract operational. When you hit a failure, this is what "diagnose before patching" looks like step-by-step.

If diagnosis reveals the proper fix is out of scope or out of authority, follow the standard's *file-residual* mechanic — open an issue with the diagnosis, ship a smaller proper fix, and link the residual from the PR. Do not let "out of scope" become an excuse for a band-aid.

## Guardrails

- Never propose a fix before completing root-cause investigation.
- Never bundle unrelated fixes while testing a hypothesis.
- Never skip reproduction unless the failure is genuinely non-reproducible after investigation.
- Never treat an external cause as proven without evidence.
