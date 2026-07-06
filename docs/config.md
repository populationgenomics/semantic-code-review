# `scr` config reference

`scr`'s config is TOML. Every setting is optional — with no config at all,
`scr` uses `backend = "auto"` and builtin backends. The fastest way to a
working config is `scr init`; this doc is the full structure for hand-editing
(`scr config edit`) and for seeing what's resolvable.

## Files & precedence

Two files, both optional, merged in order:

1. **User** — `~/.config/scr/config.toml` (or `$XDG_CONFIG_HOME/scr/config.toml`).
2. **Repo** — the nearest `.scr/config.toml` walking up from the cwd.

The repo file is merged *after* the user file, so for scalar settings **repo
overrides user**. On top of the files, **CLI flags and environment variables
win at resolve time** (e.g. `--backend`, `--model`, an exported `$…_API_KEY`).
`scr config show` prints the merged result and where each setting came from.

Both files are written `0600` by `scr init` / `scr config edit`; the user
file's directory is `0700`.

## A complete config

```toml
# Default backend when --backend isn't passed. "auto" picks claude-api if
# ANTHROPIC_API_KEY is set, else claude-cli if `claude` is on PATH.
backend = "claude-api"

# Global model fallback: used when the chosen backend pins no model of its
# own and --model isn't passed.
[model]
default = "claude-opus-4-7"

# Define a new backend, or override fields of a builtin. The table name is
# the backend id you pass to --backend / set as `backend` above.
[backends.groq]
type = "openai-compat"                        # required for a NEW backend
model = "llama-3.3-70b-versatile"             # the backend's default model
base_url = "https://api.groq.com/openai/v1"   # openai-compat endpoint
api_key_env = "GROQ_API_KEY"                  # env var holding the key
# api_key_command = ["gh", "auth", "token"]   # or: fetch the key from a command
# description = "Groq — fast Llama inference"

# Override just the model of a builtin (no `type` needed):
[backends.claude-api]
model = "claude-sonnet-4-7"

# Environment variables applied if not already set (shell / .env win). Good
# for GCP project/location — and, USER config only, an API key (this file is
# 0600; `scr config show` redacts these values). Never put a key in a repo's
# .scr/config.toml — it sits inside the repository.
[env]
GOOGLE_CLOUD_PROJECT = "my-project"
GOOGLE_CLOUD_LOCATION = "global"

[augment]
# An extra per-hunk review prompt, run alongside the main pass; its output
# is line-anchored notes the reviewer can promote to comments.
extra_prompt = """
Flag missing tests and any public API change.
"""
# Extra globs to skip in the LLM passes, on top of the builtin lockfile /
# bundle / binary denylist. Accumulates across user + repo scopes.
skip_globs = ["go.sum", "gen/**", "*.generated.ts"]
```

## Section reference

### `backend`
`"auto"` or a backend id. Resolution: `--backend` flag > this > `"auto"`.
`"auto"` prefers `claude-api` (needs `ANTHROPIC_API_KEY`), then `claude-cli`
(needs `claude` on PATH).

### `[model] default`
The model used when the selected backend has no `model` and `--model` isn't
passed. Full model precedence: `--model` > the backend's `model` >
`[model] default` > `claude-opus-4-7`.

### `[backends.<name>]`
Defines a backend or overrides a builtin. Keys:

| key | type | notes |
|---|---|---|
| `type` | string | `anthropic-sdk` \| `claude-cli` \| `google-sdk` \| `openai-compat`. Required for a new backend; omit to override a builtin. |
| `model` | string | the backend's default model id |
| `base_url` | string | endpoint (mainly `openai-compat`) |
| `api_key_env` | string | env var the key is read from |
| `api_key_command` | list or string | argv that prints the key (`["gh","auth","token"]` or `"gh auth token"`, shlex-split). Run with `shell=False`. |
| `description` | string | shown in `scr init` / `scr config show` |

Credential resolution for a backend: `$api_key_env` (from the shell, a
loaded `.env`, or `[env]`) > `api_key_command`. `claude-cli` needs no key;
`google-sdk` also accepts Vertex AI via `GOOGLE_CLOUD_PROJECT` + ADC.

Builtins (run `scr config show` for the authoritative, resolved list):
`claude-api` (anthropic-sdk), `claude-cli`, `gemini-api` (google-sdk), and
`groq` / `github` / `cerebras` / `openrouter` / `mistral` / `ollama`
(openai-compat).

### `[env]`
`KEY = "value"` pairs applied with `os.environ.setdefault` at startup — so a
shell export or a `.env` entry always wins over the config value. May hold an
API key **only** in the user config (0600). `scr config show` redacts values.

### `[augment]`
- `extra_prompt` — inline text for a second per-hunk pass; produces extra
  line-notes. Edit ergonomically with `scr config edit prompt`.
- `skip_globs` — extra file globs excluded from the LLM passes, on top of the
  builtin denylist (lockfiles, minified/bundled JS/CSS, sourcemaps, images,
  fonts, …). Accumulated across user + repo scopes. Matched against both the
  full path and the basename.

## See also
- `scr init` — interactive setup that writes most of this for you.
- `scr config show` — the resolved config + provenance.
- `scr config edit [prompt] [--scope user|repo] [--template <name>]`.
