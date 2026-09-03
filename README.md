# Socratic tutor — Python ACP agent for Zed

A generic, model-agnostic **external agent** for Zed (Agent Client Protocol,
ACP) that gives small local models a **fully controlled system prompt**. Zed
only hosts the conversation thread: the agent owns the model call (llama.cpp
router), the system prompt, and the tools — none of Zed's `agent_loop`
scaffolding reaches the model.

Two properties make this useful for tutoring:

- **100% controlled prompt** — no default prompt is baked in. The agent reads
  the `AGENTS.md` / `AGENTS.<model>.md` convention from the **project opened in
  Zed** (the session `cwd`). A course drops its pre-assembled persona files there
  (the private `MIASHS-Configuration-Tutorat` repo ships them in `agents/`) —
  nothing French or course-specific lives in this public repo.
- **Read-only, scoped tools** — the model can read the course material (`Courses/
  *.qmd` — 12 chapters plus the annexe-TP subjects under `Courses/Applications/` —,
  the rendered book, the official Python doc) *and* the files of the open
  project, with bounded paths (no absolute path, no `..`). References the model
  writes as `fichier.qmd:line` and `python:<ref>` come back as **clickable links**
  into the locally served docs.

Language: **English**. French content (the tuteur prompts) is kept out of this
repo — see the private twin at the end.

## Models

Three local models (llama.cpp), served by a single **router** `llama-server` on
port 8025:

| Model | GGUF | Template | Notes |
|--------|------|----------|-------|
| `qwen3.5-4B` | `Qwen3.5-4B-UD-Q8_K_XL.gguf` | external (`models/qwen3.5-chat-template.jinja`) | **default** — light, reliable anchoring |
| `ornith-1.5-9B` | `Ornith-1.5-9B-Q4_K_M.gguf` | embedded | longer socratic sessions |
| `gemma-4-E4B` | `gemma-4-E4B_q4_0-it.gguf` | embedded, gemma channel | accuracy option, to supervise |

Sampling (all three): `temperature 0.6`, `top_p 0.95`, `top_k 20`, `min_p 0.0`,
`repeat_penalty 1.0`. Sources of truth: `config.json` → `profiles`.

---

## 1. Prerequisites

- Python **3.10+**
- `uv` — `brew install uv` (<https://docs.astral.sh/uv/>; `uv sync` creates and
  manages `.venv/`)
- `llama-server` (llama.cpp) — Homebrew `brew install llama.cpp` (**≥ 0.3.0**:
  this is the version with the **router mode**) or a prebuilt binary on `PATH`.
- ~17 GB free (the three `.gguf` are ~4–6 GB each).

## 2. Install

```bash
cd Tutor-agent

# 1. Python deps — creates .venv/ (only dependency: the ACP SDK)
uv sync

# 2. Download the models (resumable: `curl -L -C -`, rerunning is enough)
uv run python models/fetch_models.py            # all 3 .gguf → models/
uv run python models/fetch_models.py --list     # names + expected sizes
uv run python models/fetch_models.py --only Qwen3.5-4B-UD-Q8_K_XL.gguf
```

Without `uv`: `python3 -m venv .venv && .venv/bin/pip install
'agent-client-protocol>=0.12'`, then replace `uv run python` with
`.venv/bin/python`.

The external template `models/qwen3.5-chat-template.jinja` is shipped. The
course corpus is **not**: it is centralized in the private twin (see §9 and
§12).

## 3. System prompt: AGENTS convention, no default

The runner has **no default system prompt**. At session creation it reads, from
the project opened in Zed (the session `cwd`, `tutor/config.py::build_system`):

1. `<cwd>/AGENTS.<model>.md` — used when that model is active;
2. otherwise `<cwd>/AGENTS.md` — the shared base;
3. if neither exists → **no system message** (only the conversation + tools).

The model variant *replaces* the base (nothing is appended at load time), so a
deployed `AGENTS.<model>.md` is expected to be the full prompt. Course-specific
prompts are **not** committed here: the private `MIASHS-Configuration-Tutorat`
repo keeps their sources (`prompts/`) and ships the pre-assembled
`AGENTS.*.md` variants (`agents/`) that you copy into a workspace by hand —
there is no install script.

## 4. Wiring in Zed

`settings.json`:

```json
{
  "agent_servers": {
    "tuteur": {
      "type": "custom",
      "command": ["uv", "run", "--project", "/abs/path/to/Tutor-agent", "python", "/abs/path/to/Tutor-agent/acp_agent.py"]
    }
  }
}
```

`uv` resolves (and creates if missing) the `.venv/`. If `uv` is not on Zed's
PATH, use its absolute path (e.g. `/opt/homebrew/bin/uv`). Reload Zed (or the
settings), then pick **tuteur** in the agent picker.

## 5. First launch

One `llama-server` **router** serves the three models on port 8025; the agent
ensures it automatically (`server.ensure`), preloading the default model. Manual
control:

```bash
uv run run.py start qwen3.5-4B   # ensure the router serves the model
uv run run.py status             # port + served aliases + provenance
uv run run.py stop               # stop the managed router
```

`start` / `ensure` synchronize the router preset: if the in-memory preset no
longer serves the current `config.json` aliases it regenerates + restarts once;
if it already serves a current-alias model it is **adopted without restart**
(instant switch per request remains in effect).

Sessions persist (`sessions/<id>.json`): on `session/load` the agent restores
the transcript and continues. State is written at `session/new`, so even an
empty conversation reloads.

## 6. Switching model

The model is chosen per session via the ACP **Modèle** selector (one option per
profile). A `<cwd>/.tutor-model` file (containing a profile key) still sets the
**default** read at `session/new`; `config.json → default_model` applies if
neither is present. Changing the model recreates the session (the thread is not
carried across models). With the router, the switch is instant, without killing
the process.

> Remote models: set `profiles.<model>.endpoint` to a URL — the agent then
> drives nothing locally (no start/stop), it just pings the endpoint.

### Model cards

- Qwen3.5-4B — <https://huggingface.co/unsloth/Qwen3.5-4B-GGUF>
- Ornith-1.5-9B — <https://huggingface.co/ornith-ai/Ornith-1.5-9B-GGUF>
- Gemma-4-E4B (qat) — <https://huggingface.co/google/gemma-4-E4B-it-qat-q4_0-gguf>

## 7. Tools (read-only, two roots)

`TOOLS_SPEC` (English): `grep_files`, `read_lines`, `list_directory`, plus two
in parity with the Zed **Ask** profile — `find_path` (glob for file names across
the corpus **and** the open project) and `diagnostics` (report Python syntax
errors). The model triggers them via native `tool_calls`; `_exec_tool` performs
the real read.

**Ask-parity caveat**: the ACP SDK (0.12.1) has **no host-provided-tools
mechanism** — the tool surface is agent→client only. Zed's Ask `find_path` and
`diagnostics` are *host* tools backed by Zed's project index + LSP state, which
an external ACP process cannot borrow. Both are re-implemented locally:

- `find_path` is a **full filesystem glob** (recursive on `**`, noise dirs —
  `.venv`, `__pycache__`, `node_modules`, … — skipped), paginated (page 50,
  cap 500) and returning paths reusable directly in `grep_files`/`read_lines`;
- `diagnostics` is a **static subset**: `ast.parse` per `.py` file (no temp
  artifact), **errors only, 0 warnings**, no linters and no type checking — the
  LSPs that power the real tool are host-owned. Course `.qmd` files are never
  checked.

Paths resolve against two roots, in order (`_resolve_tool_paths`):

1. **course material** — tolerance order: short key (`"01"`) → `.qmd` file;
   exact filename; glob `*`/`?` (only globs matching a corpus filename);
   substring match;
2. **open project** (`session cwd`) — project-relative paths, resolved via
   `_resolve_project_paths`: **absolute paths and `..` climbs are refused**, and
   every result is bounded under the resolved project root.

Unresolved → the tool replies with an explicit "unknown path" message (no
silent 0-match the model would re-request in a loop). Only the model's reads are
executed — no write tool exists.

## 8. Clickable local docs

The engine rewrites outgoing citations into HTTP links (never into the model
context):

- `fichier.qmd:ligne` → link to the rendered book page + section anchor
  (`www/` served on port 8765, anchors from `sections.json` — generated by
  `tools/build_docs_map.py`);
- `python:<ref>` → link to the official Python doc. Two targets,
  `config.docs.python_doc_url` (online) or the local mirror
  `config.docs.py_dir` served under `/py/` when set (offline).

The docs server (`tutor/docs.py`) therefore serves **two roots**: the course
book at `/` and the Python doc at `/py/` (when configured). It is auto-started
by `acp_agent.py`.

`docs.ensure()` verifies the port is really **our** server (marker
`/__tutor_docs__`) with the right root (it fetches a real page from
`www/`). If the configured port is taken by another process — e.g.
Betterbird/Thunderbird grabbing 8765 — `ensure()` falls back to a nearby
free port (8766, …) and `tutor.docslinks` rewrites the citations to that real
base (`docs.effective_base_url()`), so the links stay clickable no matter what
happened to the configured port.

Re-sync the rendered book cache (the corpus lives in the private twin):

```bash
# 1. copy the fresh book render into the twin's served www/
#    (the twin corpus is the 2025 course book: Cours-programmation-MIASHS-2025,
#    solutions kept out — the Solutions/ notebook dir is excluded below)
rsync -a --delete \
  --exclude slides/ --exclude downloads/ \
  --exclude docs-resources/Notebooks/Courses/Solutions/ \
  ../Cours-programmation-MIASHS-2025/docs/ \
  ../MIASHS-Configuration-Tutorat/www/
# 2. rebuild the twin's sections.json + regenerate the local TP pages
#    (annexe-B subjects — not rendered by quarto — get anchored pages under
#    www/Courses/Applications/, solutions never indexed)
uv run python tools/build_docs_map.py
```

Authoring rule for anchors: in book mode, quarto only anchors headings of level
`##` and deeper. `#` in the source is the page title — quarto folds it (or any
section whose text equals the `title:`) into the header without an anchor, so
`build_docs_map.py` excludes it. A section missing from `sections.json` almost
always means its source heading is `#` (demote it to `##`).

Exclusions: `slides/`, `downloads/`, the
`docs-resources/Notebooks/Courses/Solutions/` folder, and deliberately **no
solutions** anywhere (no leak of corrected exercises into the model context —
`sections.json` and `www/` never reference them, and the twin's annexe pages
`applications.html` / `applications.llms.md` are kept solution-free). The
annexe-TP **subjects** (`Courses/Applications/*.qmd`) come from the 2025 book
**with their answers embedded** in quarto cells tagged `#| tags: [solution]`;
these cells are stripped everywhere: the runner's `grep_files`/`read_lines`
never show them to the model, and `build_docs_map.py` regenerates the local TP
pages from the sanitized text. The strip is line-preserving (blank lines stay
in place), so `fichier:ligne` citations and the anchor map keep working. The
exercise *scaffolds* (`#| eval: false` cells with `…` placeholders) are kept
verbatim — only completed answers are removed.

Questions ("where does a TP subject live?") are answered through the
synthesized anchored pages: quarto does not render the annexe-B subjects, so
`build_docs_map.py` creates `www/Courses/Applications/<stem>.html` from the
sanitized `.qmd` (headings → anchors, rest escaped), then indexes their anchors
in `sections.json` — run it after every rsync, it is the step that writes those
pages.

## 9. config.json — centralized corpus by default, optional local override

`config.json` resolves paths from the **repo root** (`tutor/config.py` anchors on
`Path(__file__)`, never the session `cwd`), so the agent works no matter which
workspace links to it in Zed. The corpus paths point by default at the **private
twin** (the corpus is centralized there, not shipped here):

```json
"paths": {
  "gguf_dir": "models",
  "external_template": "models/qwen3.5-chat-template.jinja",
  "corpus_root": "/…/MIASHS-Configuration-Tutorat/Courses",
  "sessions_dir": "sessions",
  "course_dir": "/…/MIASHS-Configuration-Tutorat"
},
"docs": {
  "base_url": "http://127.0.0.1:8765",
  "port": 8765,
  "www_dir": "/…/MIASHS-Configuration-Tutorat/www",
  "sections_json": "/…/MIASHS-Configuration-Tutorat/sections.json",
  "py_dir": "",
  "python_doc_url": "https://docs.python.org/3/"
}
```

`course_dir` is the anchor: `Courses/`, `www/` and `sections.json` are resolved
under it (`config.corpus_root()` / `www_dir()` / `sections_json()` fall back to
`paths.corpus_root` / `docs.*` when `course_dir` is empty).

A **machine-specific** `config.local.json` (deep-merged over `config.json`, see
`tutor/config.py::_deep_merge`, gitignored, never committed) overrides the paths
for one machine or USB key — typically just `paths.course_dir` (the folder
holding `Courses/`, `www/`, `sections.json`) and `docs.py_dir` (the local Python
doc). Nothing generates this file: create it by hand. With both empty, the online
Python doc (`docs.python_doc_url`) is used for `python:<ref>` citations.

### Remote fallback (endpoint + API key)

Beyond the local router, the harness can fall back to a **remote endpoint**
(lab machine / other host), per profile or as a last resort:

- `config.json → fallback`: `endpoint` (e.g. `http://<host>:8080`) and
  `api_key` (Bearer). Empty `endpoint` (shipped default) → fallback disabled.
  `profiles.<model>.endpoint` overrides the fallback for that model.
- See `tools/download_gguf.sh` and `tools/llama-swap-tuteur.example.yaml` for
  setting up the remote host.

## 10. Tests

```bash
# backend (router preset generation + llama-server cmd) — no models required
uv run python -m unittest tests/test_server.py -v

# protocol (engine in STUB) — no server or corpus required
TUTOR_STUB=1 uv run python -m unittest tests/test_protocol.py -v

# llm (remote fallback auth: endpoint + API key, mocked urlopen) — offline
uv run python -m unittest tests/test_llm.py -v

# everything (95 tests): test_server pure, test_protocol & test_runner_features
# in STUB, test_tools with real contents (sections.json + localhost server on
# the twin www/ as resolved by config), test_llm offline (mocked urlopen)
TUTOR_STUB=1 uv run python -m unittest discover -s tests -v
```

`test_runner_features` covers the refactor: AGENTS convention, project-relative
tool paths (absolute / `..` refused), `list_directory`, `config.local.json`
merge, `python:<ref>` rewriting (full + streaming), the two-root docs server,
and the Ask-parity tools `find_path` (project glob + noise-dir exclusion +
absolute / `..` refusal) and `diagnostics` (single file, whole project, .qmd
rejected). `test_tools` adds direct unit coverage for `find_paths` (corpus key /
glob / substring / pagination) and `py_syntax_errors` (good, bad, NUL, missing).

## 11. Why an ACP agent? (and why not Zed + Ask + erased prompt)

For a model declared in Zed's *LLM Providers* and used by the built-in agent:
**Zed cannot be made to send no system prompt** — it always prefixes a large one
(~3–4k tokens: communication rules, formatting, detailed tool use) that can only
be *appended* to, never replaced (open discussion: zed-industries/zed #58770).

For an **external ACP agent** (`agent_servers`, `"type": "custom"`), Zed hosts
the thread but the agent "owns its own runtime, auth, model selection, tools,
and native configuration" (External Agents docs). The prompt sent to the model is
therefore **entirely** the agent's — the exact requirement for small local
models (4–9B) that derail under Zed's scaffolding (tool-format confusion,
answer leakage, repetition). This repo is that agent.

## 12. Private twin

The course-specific configuration lives in the private
`MIASHS-Configuration-Tutorat/` repo (sibling): French tuteur prompt sources
(`prompts/`), their pre-assembled `AGENTS.*.md` variants (`agents/`), the
**centralized corpus** (`Courses/`, `www/`, `sections.json`) that `config.json`
points at by default, model-choice guidance and ACP pass findings. There is no
install script — the corpus is wired through `config.json` (overridable with
`config.local.json`) and the prompts are copied into a workspace by hand. This
repo stays generic, in English, and prompt-free.
