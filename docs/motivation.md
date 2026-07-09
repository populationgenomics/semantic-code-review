# Motivation

Why scr exists and what belongs in it. Mechanics live in
[`README.md`](../README.md); domain vocabulary in
[`CONTEXT.md`](../CONTEXT.md). This doc is the scope filter: a feature
belongs in scr only if it sharpens attention-direction or tightens the
judge→iterate loop.

## What scr is for

Judging code you are accountable for but did not write by hand —
typically an agent's implementation of a plan you agreed. The
distinguishing situation is accountability without authorship: it is your
PR and your name, but you did not type it. Any agent-driven workflow puts
people here.

## Signpost, not cop

The LLM decides where the human should look, not what passes. It clusters
the diff into semantic groups and annotates what each block changed; the
human keeps every judgment. Nothing is gated, auto-posted, or auto-fixed.

This is a deliberate fork from the automated-PR-bot lineage (CodeRabbit,
Qodo, et al.), whose model is gate + auto-post + rules engine — under
which the human role degrades to dismissing comments. scr inverts the
control flow: the scarce resource is human attention, and scr spends LLM
budget allocating it.

## The loop

The workflow scr serves: agree a plan → agent implements it autonomously
→ human judges whether it implemented the plan and how it did so → agent
iterates to convergence. scr is the judge step. `--spec` supplies the
plan as ground truth, so the review judges the diff against agreed intent,
not a generic rubric.

Two things are judged, and the second is the point:

- **whether** the change matches intent.
- **how** it was built — approach, altitude, fidelity. A defect-finder
  (`/code-review --fix`, linters, tests) structurally cannot surface this:
  an implementation can solve the right problem the wrong way, pass every
  test, and still be wrong to accept.

## One loop, two radii

`scr review` and `scr pr` are the same loop at different radii, not two
tools.

- **Short loop** (Claude Code plugin): comments return into the session;
  the authoring agent, still holding context, iterates immediately.
- **Long loop** (`scr pr`): comments post as one GitHub review; the author
  or their agent picks them up later, across people and time.

The loop mechanism is agent-agnostic. The agnostic surface is the CLI
stdout contract: `scr review` blocks, opens the viewer, prints the
comments as structured markdown on Done, exits. Any TUI agent that can run
a subprocess and read stdout drives the loop. Only the packaging (slash
command, driver prompt, venv bootstrap) is Claude-specific.

## Scope: conformance vs adequacy

scr is the judgment layer on top of a conformance-verification substrate,
not a replacement for it.

- **Conformance** — does the code do what it was specified to do? Ceded to
  tests, `/code-review --fix`, the agent, verification platforms.
  Deterministic, machine-checkable, scales.
- **Adequacy** — was the model of the problem complete? Kept by scr. A
  verification substrate cannot detect an adequacy gap: it checks
  expectations that exist and never notices a missing one.

So the cede is of *conformance*, not *correctness*. "You did not consider
case X" is in scope even when X affects correctness — it is a judgment
about intent, not a conformance check. Correctness-relevant gaps that
arise from an incomplete model are adequacy, and stay in scope.

Suggesting a unit test is in scope as a **signpost** — a specific
unconsidered case the human or agent decides on and writes — but not as a
**generator**: coverage-driven auto-generation of a suite is conformance
tooling, and off-thesis.

## The failure mode to design against

The load-bearing assumption is that the accountable reviewer actually
judges rather than rubber-stamps. Under volume, human vigilance on
mostly-correct automation drops; a reviewer clearing a queue stamps past a
signpost as readily as they dismiss a bot's comment. scr's value then
evaporates exactly when it is most needed.

The only defense is precision: following the signpost must be cheaper than
the risk of skipping it. This makes precision of attention-direction the
one existential quality metric — not feature coverage. A noisy signpost
gets stamped past; a precise one gets read. Adequacy signposts are the
easiest class to over-generate ("considered null? empty? unicode?"), so
the precision bar bites hardest there: earn a slot only if the case
plausibly matters and plausibly was not considered.

## Off-thesis

Features that move scr toward gating, enforcing, or authoring — the cop
lineage:

- autofix / auto-patching (the authoring agent's job downstream).
- rules engine / check-runs / CI gating (converts signpost into
  enforcement).
- coverage-driven test generation (conformance tooling).
- codebase-wide vector index (the diff plus `--spec` is the context that
  matters for judging intent).

## Falsifier

The bet is that judging the *how*, and adequacy, is durable value a
verification substrate cannot absorb. Local test, watchable in use: does
scr regularly surface intent/design problems that tests and
`/code-review --fix` missed? If yes, the bet holds regardless of industry
trends.
