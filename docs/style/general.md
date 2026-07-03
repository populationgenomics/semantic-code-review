# Code style — language-agnostic

Principles independent of language; the language layer builds on it
([`python.md`](python.md)). On conflict, the language doc wins for its
language. Code properties only — behavioral directives (push back, resist
the minimal fix) live in [`../../CLAUDE.md`](../../CLAUDE.md).

## Fail loud; never silently degrade

Raise on missing or malformed data; don't paper over it with `x or []`,
`x or {}`, or a bare `if x:` that skips the real case — that turns a
missing input into a silent wrong answer. Validate and fail early.

## Comments

Comment the non-obvious *mechanism* or *constraint*, tersely. The *why*
(why this shape was chosen) belongs in a design doc or the docstring,
not inline — and never duplicate what a doc already states. No history
narration ("removed X", "switched from Y"), commented-out code, or
persuasion: write as if the current shape always existed.

```python
# Bad — rationale, persuasion, and the design doc already states this
conn = connect(dsn)  # one connection not a pool: pooling adds reconnect
# complexity we don't need yet, only pays off above N writers — the
# whole point of staying simple ...

# Good — one non-obvious fact; the why stays in the doc
conn = connect(dsn)  # single connection: the writer is single-threaded
```
