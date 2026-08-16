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

Turning thinking on is not free to configure. Anthropic rejects thinking
combined with forced tool choice, and pydantic-ai's `ToolOutput` works
by forcing a synthetic submit tool (pydantic-ai #2425, closed as
upstream behaviour). So the output mode and the reasoning setting are
coupled: choosing thinking chooses the output mode.

## Decision

**SDK backends use `NativeOutput` and enable adaptive thinking.**
`NativeOutput` constrains the response through Anthropic's own
structured outputs (`output_config.format`) rather than a forced tool,
so it composes with thinking and leaves ordinary tools working.

**The CLI backend stays on `ToolOutput`.** `_cli_driver` builds
`--json-schema` from `output_tools[0]` and raises without it. It needs
no thinking setting — it already reasons at the CLI's default depth,
which is why it was the better-behaved arm all along.

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
quality normally trade. The mechanism shows in the `purpose` string now
required on every tool call — each of the 28 is a distinct answerable
question, none repeated. Thinking lets the model settle from the diff
alone whether it needs to look at anything, and for half the hunks the
answer is no. Previously it had to start searching to find that out, and
an empty result is indistinguishable from a bad query, so it rephrased.

The request cap (`DEFAULT_REQUEST_LIMIT = 20`) is now slack, not a
binding constraint: the busiest hunk makes 4 calls. It stays as a
runaway backstop.

Grammar compilation makes schema changes cost something the tool-call
path did not: the first run after any change to `HunkSubmission` pays a
compile stampede. The retry absorbs it.

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
and simpler to reason about, at 4× the cost and worse annotations. The
uniformity was already false — the CLI was thinking and the SDK was not.

**`--effort high` on the CLI structured path.** Tested once the SDK
result made the lever worth checking rather than assuming, since the
existing "tuned separately" comment asserted it without a measurement.
It costs 14% more (1,170,184 → 1,331,864 tokens) for flat quality —
context 27 → 26 hunks, refs 12 → 11, segments unchanged, and one hunk
stretching to 7 turns against a previous max of 2. Line notes (16 → 22)
were the only gain. The structured path keeps omitting the flag; the
console keeps setting it.
