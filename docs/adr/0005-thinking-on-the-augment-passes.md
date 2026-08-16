# ADR 0005 — Thinking on the augment passes, and the output mode it forces

- Status: Accepted (implemented)
- Date: 2026-08-16

## Context

The two backend families reached the per-hunk pass with different
reasoning defaults, and neither was chosen:

- **CLI backend** — `claude -p` on an adaptive-thinking model reasons by
  default. `--effort` is passed only by `_build_text_invocation` (the
  console); the structured path omits it deliberately, but omitting the
  flag does not disable thinking, it selects the CLI's default depth.
- **SDK backends** — pydantic-ai sends no `anthropic_thinking` unless
  asked, and the Anthropic default is off. The per-hunk pass therefore
  ran with **no** reasoning.

That asymmetry was invisible and load-bearing. Measured on the same
commit (33 hunks), the SDK arm spent 565 tool calls and failed 9 hunks
against the CLI arm's 32 calls and 0 failures — a gap we had been
attributing to the backends, and had spent effort chasing through prompt
and seed changes (removed-symbol seeding, head-vs-base search semantics,
a request cap). Those were real improvements — 565 → 233 calls — but
they were treating the symptom of a missing capability.

Turning thinking on constrains the output mode. Anthropic rejects
thinking combined with forced tool choice, and pydantic-ai's
`ToolOutput` works by forcing a synthetic submit tool. pydantic-ai
#2425 was closed by a change on their side: `prepare_request` now
upgrades the output mode to native automatically when thinking is on,
and raises only when the caller pinned `ToolOutput` explicitly. So a
plain `output_type=HunkSubmission` would get native selected for us —
naming `NativeOutput` is a choice to be explicit, not a requirement.

## Decision

**SDK backends use `NativeOutput` and adaptive thinking where the model
supports it.** `NativeOutput` constrains the response through
Anthropic's own structured outputs (`output_config.format`) rather than
a forced tool, so it composes with thinking and leaves ordinary tools
working.

Native output is a per-*model* capability, so the flag is derived from
the model profile rather than the backend class. Setting it per backend
family broke two paths silently, since the pipeline catches per-hunk
exceptions and a run then exits 0 with an empty viewer: Google raises on
native output plus function tools below Gemini 3 (and `gemini-2.5-pro`
is that backend's default model), and pydantic-ai's list of Anthropic
models supporting native output is hard-coded, so a model outside it had
no working configuration at all — native raised, and falling back to
`ToolOutput` raised on thinking instead. Thinking is applied only
alongside native output for the same reason.

Only the per-hunk and batch passes are affected. Overview, fold-summary
and extra-review still use `ToolOutput` unconditionally; they are
single-request passes with no tool loop, so there is nothing for
reasoning to change.

**The CLI backend stays on `ToolOutput`.** `_cli_driver`'s structured
path builds `--json-schema` from `output_tools[0]`; with no output tool
it takes the free-form branch instead and returns a `TextPart`, which
is the console path (ADR 0002), not something the augment pass can use.
It needs no thinking setting — `claude -p` on an adaptive-thinking model
already reasons at the CLI's default depth. That is the most plausible
explanation for it being the better-behaved arm, though it is inferred:
the CLI is not a versioned surface and we cannot read its default.

**The per-hunk output schema is the submission shape, not the storage
shape.** `HunkSubmission` is what the model is asked for;
`HunkAnnotations` extends it with `fold_descriptions`, written later by
the fold-summary pass. Native structured output compiles the schema into
a grammar, so every field carries a cost the tool-call path did not
charge as visibly.

**"Grammar compilation timed out" is retried, not counted as a failed
hunk.** Anthropic compiles a schema's grammar on first use and caches it
for 24 hours; a run fans out dozens of hunks concurrently, so a changed
schema means dozens of simultaneous first compiles and some lose the
race. Bounded retry, that error only.

## Consequences

Measured on the same commit, SDK arm, prompt p18:

| | before | after |
|---|---|---|
| billed tokens | 5,403,539 | 961,016 |
| est. cost | $17.11 | $3.94 |
| failed hunks | 4 | 0 |
| tool calls | 233 | 28 |
| hunks using tools | 33/33 | 16/33 |

Quality rose on every axis at once (context 19 → 26 hunks, refs 0 → 10,
segments 3 → 11, line notes 8 → 16), which is the unusual part: cost and
quality normally trade.

Two caveats on those numbers. The before-run failed 4 hunks, so its
annotation counts are over 29 hunks against the after-run's 33 — context
is 66% -> 79% per annotated hunk, not the raw 19 -> 26. And the change
is confounded: thinking, the switch to `NativeOutput`, and dropping
`fold_descriptions` from the output grammar all landed together, on a
new prompt revision. n=1 for each arm.

The mechanism shows in the `purpose` string now required on every tool
call — each of the 28 is a distinct answerable question, none repeated.
The reading is that thinking lets the model settle from the diff alone
whether it needs to look at anything, and for half the hunks the answer
is no; previously it had to start searching to find that out, and an
empty result is indistinguishable from a bad query, so it rephrased.
That is an interpretation of one sample, not an isolated variable.

The request cap (`DEFAULT_REQUEST_LIMIT = 20`) is now slack, not a
binding constraint: the busiest hunk makes 4 tool calls, so about 5
requests — the cap counts model requests, which is the unit
`UsageLimits` takes and one step removed from `--max-turns` on the CLI
side. It stays as a runaway backstop.

Grammar compilation makes schema changes cost something the tool-call
path did not: the first run after any change to `HunkSubmission` pays a
compile stampede. The retry absorbs it.

Anthropic's grammar cache TTL is quoted as 24 hours from their docs; we
have not measured it, and nothing here depends on the number — the retry
is a fixed 3 attempts.

Two conclusions were reached and discarded on the way here, recorded so
they are not re-derived: the grammar timeout is *not* caused by schema
complexity (a hand-written model of the same shape compiles fine), and
thinking and tool use are *not* in conflict (only thinking and *forced*
tool choice are).

## Alternatives considered

**`PromptedOutput`** — asks for JSON in the prompt with no enforcement.
Composes with thinking, but gives up the schema guarantee that lets the
pipeline treat a returned object as valid. Reserved as a fallback if
native structured output becomes unavailable.

**Keep `ToolOutput` everywhere, no thinking.** Uniform across backends
and simpler to reason about, at roughly 4× the cost and worse
annotations. The uniformity was already false — the CLI was thinking and
the SDK was not.

**Pass a plain `output_type` and let pydantic-ai pick native.** Fewer
moving parts, and it is what #2425's fix does. Rejected because the mode
would then be implicit in a library's dispatch: the CLI backend needs
`ToolOutput` specifically, so the two paths have to differ on purpose,
and naming both makes the split visible at the call site.

**`--effort high` on the CLI structured path.** Tested once the SDK
result made the lever worth checking rather than assuming, since the
existing "tuned separately" comment asserted it without a measurement.
It costs 14% more (1,170,184 → 1,331,864 tokens) for flat quality —
context 27 → 26 hunks, refs 12 → 11, segments unchanged, and one hunk
stretching to 7 turns against a previous max of 2. Line notes (16 → 22)
were the only gain. The structured path keeps omitting the flag; the
console keeps setting it.
