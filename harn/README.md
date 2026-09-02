# acp-doc-helper

A generic, model-agnostic **ACP agent runner** for [harn](https://harnlang.com),
designed to be embedded in Zed. It gives small local models a **fully
controlled system prompt**, scoped read-only tools, a local→remote provider
fallback, and **clickable citations** into a rendered documentation book.

Everything is driven by environment variables, so this repository stays generic:
no model names, no prompts, no course content. End-user configuration (prompts,
student-facing guidance) lives in a separate repository.

## Why

Zed's built-in agent scaffolding injects stance/scratchpad directives into the
system prompt. Small models behave better when every token of the system prompt
is under your control. `main.harn` implements its own minimal tool loop over the
ACP lifecycle — no `agent_loop` help — so the model sees only what you put in
`agent/instructions.md` (or any file via `TUTOR_SYSTEM`).

## Layout

```
harn/
  main.harn                # the ACP runner (single file, no dependencies)
  agent/instructions.md    # default system prompt (EN, generic)
  harn.toml                # project manifest
  providers.toml.dist      # template for ~/.config/harn/providers.toml
  run.sh                   # convenience launcher for `harn serve acp`
  README.md                # this file
```

## Requirements

- `harn` binary (`>= 0.10`; this file is developed and validated against
  `0.10.127`). `harn serve acp` is the entry point.
- An OpenAI-compatible chat endpoint. Typically llama.cpp running in "router"
  mode locally (default provider `llamacpp`, base `http://127.0.0.1:8025/v1`).
  Any local or remote OpenAI-compatible server works.

## Provider configuration

Copy `providers.toml.dist` to `~/.config/harn/providers.toml` and adjust:

```toml
[providers.llamacpp]
base_url = "http://127.0.0.1:8025/v1"   # local router, no auth
auth_style = "none"
chat_endpoint = "/chat/completions"

[providers.llamacpp_remote]             # optional remote fallback
base_url = "https://your-endpoint.example/v1"
auth_style = "bearer"
auth_env = "TUTOR_API_KEY"              # env var holding the API key
chat_endpoint = "/chat/completions"
```

Never commit your real `providers.toml`. `auth_env` accepts a single variable
name or an array of names tried in order.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `TUTOR_PROVIDER` | `llamacpp` | provider name to call first |
| `TUTOR_MODEL` | `qwen3.5-4B` | model name (alias as configured on the endpoint) |
| `TUTOR_DOCROOT` | `.` | root for the read-only file tools |
| `TUTOR_SYSTEM` | `agent/instructions.md` | path to the system prompt file (relative to cwd) |
| `TUTOR_DOCS_BASE` | *(empty)* | base URL for citation→link rewriting; empty disables it |
| `TUTOR_SECTIONS` | *(empty)* | path to the `sections.json` citation map |
| `TUTOR_TOOL_FORMAT` | `json` | tool calling format passed to the provider (`json`/`native`) |
| `TUTOR_THINKING` | `off` | `off` / `on` / `adaptive` / `dict` |
| `TUTOR_EFFORT` | *(empty)* | reasoning effort string (provider-dependent) |
| `TUTOR_TEMPERATURE` | `0.6` | sampling temperature |
| `TUTOR_MAX_TOKENS` | `2048` | completion cap |
| `TUTOR_FALLBACK` | *(empty)* | comma-separated provider names; tried in order if the first fails |
| `TUTOR_DEBUG` | `false` | emit `RUN_LOOP`/`RUN_CFG` traces to the harn log |

### System prompt

The system prompt is read from `TUTOR_SYSTEM` (default `agent/instructions.md`)
and passed verbatim to the model. There is no other scaffolding. Point it at the
file that actually describes your behavior:

```bash
export TUTOR_SYSTEM=/absolute/path/to/my-prompt.md
```

### Local → remote fallback

`TUTOR_FALLBACK=llamacpp_remote` (comma-list supported) makes the runner retry
through the remote provider when the local one returns no response — e.g. when
the router is stopped. Combined with the bearer provider above and
`TUTOR_API_KEY` in the environment, a session survives local outages.

### Tool scoping

`agent_read_tools` is built with `{root: docroot, cwd: docroot}`. Tools are
read-only (read/list/search/git-inspect only) and reject absolute paths;
anything outside the root is refused by the runtime, and the model sees the
rejection rather than file content.

### Citation → link rewriting

The corpus marks sections by line number. When the model cites
`01_sample.qmd:4`, the runner rewrites it into a clickable link
`[01_sample.qmd:4](http://127.0.0.1:8765/01_sample.html#introduction)` before
emitting the answer, using `TUTOR_SECTIONS` (`sections.json`) to map a file/line
to a rendered HTML page + anchor slug.

`sections.json` shape (same as the one emitted by the book build):

```json
{
  "01_asynchronous.qmd": {
    "html": "01_asynchronous.html",
    "sections": [
      {"line": 1, "slug": "introduction", "title": "Introduction"},
      {"line": 42, "slug": "main-part", "title": "Main part"}
    ]
  }
}
```

The rewrite is deterministic and applied on the final emitted text. Unknown
files or files without a base URL pass through unchanged. The local static doc
server that serves the HTML is **not** part of this runner — run it however you
like (the companion repo ships one). The links are only clickable when that
server is up.

## Running

```bash
harn check main.harn          # validate (zero diagnostics)
./run.sh                       # harn serve acp main.harn (stdio)
```

### Embedding in Zed (custom agent server)

Zed launches the agent through a custom agent server. Point it at the runner and
export the `TUTOR_*` variables:

```bash
TUTOR_PROVIDER=llamacpp \
TUTOR_MODEL=qwen3.5-4B \
TUTOR_DOCROOT=/path/to/docroot \
TUTOR_SYSTEM=/path/to/instructions.md \
TUTOR_DOCS_BASE=http://127.0.0.1:8765 \
TUTOR_SECTIONS=/path/to/sections.json \
harn serve acp /path/to/harn/main.harn
```

For a remote (WebSocket) agent server, Zed supports websocket ACP:

```bash
./run.sh --transport websocket --bind 127.0.0.1:8789 --api-key SECRET
```

Use `--hmac-secret <secret>` instead of `--api-key` when the client supports
HMAC authentication through the ACP `authenticate` method.

### Debugging

```bash
TUTOR_DEBUG=true ./run.sh     # RUN_CFG / RUN_LOOP traces on stdio.log
```

- If a session returns empty output with no crash, check the model:
  `tool_format:"json"` with the `<tool_call>{...}</tool_call>` marker in the
  system prompt is required for several small models to emit tool calls.
- `TUTOR_THINKING=on` produces no visible output on some llama.cpp models (no
  crash, just empty text) — leave it `off` unless your model supports it.

## Security notes

- Read tools only, absolute paths forbidden, root-scoped by construction.
- API keys travel exclusively through the environment (`TUTOR_API_KEY`); nothing
  writes them to disk in this repo.
- The system prompt is a file you control. Keep any sensitive or course-specific
  instructions out of this public repository.

## License

MIT
