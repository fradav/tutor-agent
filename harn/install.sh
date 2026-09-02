#!/usr/bin/env bash
#
# Install the whole tutoring stack for the harn-based Socratic tutor,
# EXCEPT the (private) French model prompts.
#
# What this installs:
#   1. the generic ACP runner  (this folder: main.harn, run.sh, harn.toml,
#      providers.toml.dist, agent/instructions.md, README.md)
#   2. the course corpus       (Courses/*.qmd, www/, sections.json)
#   3. the harn provider config  (~/.config/harn/providers.toml, from .dist)
#   4. a prompts/ scaffold     (a README, NOT the French prompts themselves)
#   5. env + Zed wiring        (env.example.sh, zed-agent-servers.json,
#      start-docs.sh) with absolute paths
#
# The harn runtime is registry-led: this script does NOT install a global
# binary. Zed's ACP Registry downloads harn for its "Harn" external agent;
# install.sh and run.sh resolve that copy (or a harn already on PATH). If none
# is found, the script tells you how to fetch it. To install a global binary
# anyway (terminal use, non-Zed machines):
#   curl -fsSL https://harnlang.com/install.sh | sh
#
# The French model prompts are intentionally NOT part of this script. It creates
# a `prompts/` folder with an explanatory README; whoever holds the prompts
# places a compiled prompt file there and points TUTOR_SYSTEM at it.
#
# Usage:
#   ./install.sh --prefix ~/tutorat            # install to a directory
#   ./install.sh --dry-run                     # print plan, change nothing
#   ./install.sh --no-corpus                   # runner only (no course corpus)
#   ./install.sh --help
#
# Configuration overrides:
#   --runner DIR    where the runner lives (default: this folder)
#   --corpus DIR    where the corpus lives   (default: <runner>/../corpus)
#   --system-prompt FILE   TUTOR_SYSTEM target (default: installed instructions.md)
#   --models DIR    where GGUF files live (only used to print the model server line)
#   --with-fallback emit TUTOR_FALLBACK=llamacpp_remote (user supplies TUTOR_API_KEY)
#   --force-providers         overwrite an existing ~/.config/harn/providers.toml
#   --yes                     non-interactive (no confirmation prompts)
set -euo pipefail

# --- locate our own directory (location-independent) -------------------------
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- defaults ----------------------------------------------------------------
PREFIX="${PREFIX:-$HOME/tutorat}"
RUNNER_SRC="$HERE"
CORPUS_SRC="$(cd "$HERE/.." && pwd)/corpus"
SYSTEM_PROMPT=""                # empty => <prefix>/harn/agent/instructions.md
MODELS_DIR=""                   # empty => placeholder in the model-server line
DOCS_PORT=8765
PROVIDER=llamacpp
MODEL=qwen3.5-4B
THINKING=off
TEMPERATURE=0.6
MAX_TOKENS=2048
DOCS_BASE="http://127.0.0.1:$DOCS_PORT"
WITH_FALLBACK=0
FORCE_PROVIDERS=0
WITH_CORPUS=1
DRY_RUN=0
ASSUME_YES=0
PREFIX_DEFAULTED=1

usage() {
  sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' | grep -v '^$' | grep -v '^set -euo'
  exit 0
}

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }

die() { err "$*"; exit 1; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

# --- parse arguments ----------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)          PREFIX="${2:?--prefix needs a directory}"; PREFIX_DEFAULTED=0; shift 2 ;;
    --runner)          RUNNER_SRC="${2:?--runner needs a directory}"; shift 2 ;;
    --corpus)          CORPUS_SRC="${2:?--corpus needs a directory}"; shift 2 ;;
    --system-prompt)   SYSTEM_PROMPT="${2:?--system-prompt needs a file}"; shift 2 ;;
    --models)          MODELS_DIR="${2:?--models needs a directory}"; shift 2 ;;
    --docs-port)       DOCS_PORT="${2:?--docs-port needs a number}"; DOCS_BASE="http://127.0.0.1:$DOCS_PORT"; shift 2 ;;
    --provider)        PROVIDER="${2:?--provider needs a name}"; shift 2 ;;
    --model)           MODEL="${2:?--model needs a name}"; shift 2 ;;
    --no-corpus)       WITH_CORPUS=0; shift ;;
    --with-fallback)   WITH_FALLBACK=1; shift ;;
    --force-providers) FORCE_PROVIDERS=1; shift ;;
    --yes|-y)          ASSUME_YES=1; shift ;;
    --dry-run)         DRY_RUN=1; shift ;;
    -h|--help)         usage ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

# --- resolve sourced paths ----------------------------------------------------
RUNNER_SRC="$(cd "$RUNNER_SRC" && pwd)"
[ "$WITH_CORPUS" = 1 ] && CORPUS_SRC="$(cd "$CORPUS_SRC" && pwd)"
[ "$SYSTEM_PROMPT" != "" ] && SYSTEM_PROMPT="$(cd "$(dirname "$SYSTEM_PROMPT")" && pwd)/$(basename "$SYSTEM_PROMPT")"
[ "$MODELS_DIR" != "" ] && MODELS_DIR="$(cd "$MODELS_DIR" && pwd)"

RUNNER_DEST="$PREFIX/harn"
CORPUS_DEST="$PREFIX/corpus"
PROMPTS_DEST="$PREFIX/prompts"
SYSTEM_DEST="$PREFIX/harn/agent/instructions.md"
[ "$SYSTEM_PROMPT" != "" ] && SYSTEM_DEST="$SYSTEM_PROMPT"

# harn user config directory (Linux/macOS)
if [ -n "${XDG_CONFIG_HOME:-}" ]; then
  HARN_CONFIG_DIR="$XDG_CONFIG_HOME/harn"
else
  HARN_CONFIG_DIR="$HOME/.config/harn"
fi
HARN_PROVIDERS="$HARN_CONFIG_DIR/providers.toml"

# --- locate harn (registry-led: PATH first, then Zed's ACP registry) -----------
ver_gt() { # numeric version compare: ver_gt A B -> true if A > B
  local a b
  IFS=. read -r -a a <<< "$1"; IFS=. read -r -a b <<< "$2"
  while [ ${#a[@]} -lt 3 ]; do a+=(0); done
  while [ ${#b[@]} -lt 3 ]; do b+=(0); done
  [ "${a[0]}" -gt "${b[0]}" ] && return 0; [ "${a[0]}" -lt "${b[0]}" ] && return 1
  [ "${a[1]}" -gt "${b[1]}" ] && return 0; [ "${a[1]}" -lt "${b[1]}" ] && return 1
  [ "${a[2]}" -gt "${b[2]}" ] && return 0
  return 1
}
find_harn() { # print a usable harn path, or nothing
  local bin base dir ver best bestv
  bin="$(command -v harn 2>/dev/null)" && [ -x "$bin" ] && { printf '%s\n' "$bin"; return 0; }
  for base in \
    "$HOME/Library/Application Support/Zed/external_agents/registry/harn" \
    "${XDG_DATA_HOME:-$HOME/.local/share}/zed/external_agents/registry/harn"; do
    [ -d "$base" ] || continue
    for dir in "$base"/v_*; do
      [ -x "$dir/harn" ] || continue
      ver="${dir##*/v_}"; ver="${ver%%_*}"
      if [ -z "$best" ] || ver_gt "$ver" "$bestv"; then best="$dir/harn"; bestv="$ver"; fi
    done
  done
  [ -n "$best" ] && printf '%s\n' "$best"
}
HARN_BIN="$(find_harn || true)"

# --- pre-flight ---------------------------------------------------------------
need_cmd cp; need_cmd mkdir; need_cmd sed; need_cmd date
[ -f "$RUNNER_SRC/main.harn" ]   || die "runner folder does not look right: missing $RUNNER_SRC/main.harn"
[ -f "$RUNNER_SRC/run.sh" ]      || die "runner folder does not look right: missing $RUNNER_SRC/run.sh"
if [ "$WITH_CORPUS" = 1 ] && [ ! -f "$CORPUS_SRC/sections.json" ]; then
  die "corpus does not look right: missing $CORPUS_SRC/sections.json (use --corpus DIR or --no-corpus)"
fi

# --- dry-run / confirmation ----------------------------------------------------
if [ "$DRY_RUN" = 1 ]; then
  log "DRY RUN — nothing will be written"
  dry() { printf '    %s\n' "$*"; }
  dry "prefix            : $PREFIX"
  dry "runner source     : $RUNNER_SRC"
  [ "$WITH_CORPUS" = 1 ] && dry "corpus source     : $CORPUS_SRC" || dry "corpus            : (skipped)"
  dry "system prompt     : $SYSTEM_DEST"
  dry "providers target  : $HARN_PROVIDERS"
  dry "docs server       : python3 -m http.server $DOCS_PORT --directory $CORPUS_DEST/www"
  if [ -n "$HARN_BIN" ]; then
    dry "harn runtime      : $HARN_BIN"
  else
    dry "harn runtime      : not found (PATH or Zed registry) -> enable the Zed 'Harn' external agent to fetch it"
  fi
  exit 0
fi

log "This will install the tutoring stack at: $PREFIX"
log "  runner: $RUNNER_SRC -> $RUNNER_DEST"
[ "$WITH_CORPUS" = 1 ] && log "  corpus: $CORPUS_SRC -> $CORPUS_DEST"
log "  prompts scaffold: $PROMPTS_DEST (no prompt content is copied)"
if [ -n "$HARN_BIN" ]; then
  log "  harn runtime    : $HARN_BIN"
else
  log "  harn runtime    : not found -> enable the Zed 'Harn' external agent (downloads it)"
fi

if [ "$ASSUME_YES" != 1 ]; then
  if [ ! -t 0 ]; then
    die "no interactive terminal detected — pass --yes to install non-interactively"
  fi
  printf 'Continue? [y/N] '
  read -r yn
  case "$yn" in y|Y|yes|YES) : ;; *) die "aborted." ;; esac
fi

# --- 1. locate the harn runtime (registry-led, no install) ------------------------
if [ -n "$HARN_BIN" ]; then
  log "harn runtime: $HARN_BIN"
else
  warn "no harn runtime found (checked PATH and Zed's ACP registry)."
  warn "Fetch it by enabling the 'Harn' external agent once in Zed"
  warn "(Agent Settings > External Agents > Harn), or run:"
  warn "  curl -fsSL https://harnlang.com/install.sh | sh"
fi

# --- 2. copy the runner ----------------------------------------------------------
log "Copying the generic runner..."
mkdir -p "$RUNNER_DEST/agent"
cp "$RUNNER_SRC/main.harn"    "$RUNNER_DEST/main.harn"
cp "$RUNNER_SRC/run.sh"       "$RUNNER_DEST/run.sh"
cp "$RUNNER_SRC/harn.toml"    "$RUNNER_DEST/harn.toml"
cp "$RUNNER_SRC/providers.toml.dist" "$RUNNER_DEST/providers.toml.dist"
[ -f "$RUNNER_SRC/README.md" ] && cp "$RUNNER_SRC/README.md" "$RUNNER_DEST/README.md"
[ -f "$RUNNER_SRC/agent/instructions.md" ] && cp "$RUNNER_SRC/agent/instructions.md" "$RUNNER_DEST/agent/instructions.md"
chmod +x "$RUNNER_DEST/run.sh"

# --- 3. copy the corpus (optional) ----------------------------------------------
if [ "$WITH_CORPUS" = 1 ]; then
  log "Copying the corpus (this may take a few seconds)..."
  mkdir -p "$CORPUS_DEST"
  cp -R "$CORPUS_SRC/Courses" "$CORPUS_DEST/"
  cp -R "$CORPUS_SRC/www"     "$CORPUS_DEST/"
  cp "$CORPUS_SRC/sections.json" "$CORPUS_DEST/sections.json"
  rm -f "$CORPUS_DEST/Courses/.gitignore"
fi

# --- 4. provider config ------------------------------------------------------------
install_providers() {
  if [ -f "$HARN_PROVIDERS" ]; then
    if grep -q '\[providers\.'"$PROVIDER"'\]' "$HARN_PROVIDERS" && [ "$FORCE_PROVIDERS" = 0 ]; then
      log "Keeping existing provider config ($HARN_PROVIDERS already defines [$PROVIDER])."
      return
    fi
    local bak="$HARN_PROVIDERS.bak.$(date +%Y%m%d%H%M%S)"
    if [ "$FORCE_PROVIDERS" = 1 ]; then
      cp "$HARN_PROVIDERS" "$bak"
      warn "Backed up existing provider config to $bak, overwriting."
    fi
  fi
  mkdir -p "$HARN_CONFIG_DIR"
  cp "$RUNNER_DEST/providers.toml.dist" "$HARN_PROVIDERS"
  log "Wrote provider config: $HARN_PROVIDERS (template, no credentials)."
}
install_providers
[ -f "$HARN_PROVIDERS" ] && chmod 600 "$HARN_PROVIDERS"

# --- 5. prompts scaffold (never copies the French prompts) --------------------------
log "Creating the prompts/ scaffold (the French prompts are NOT installed)..."
mkdir -p "$PROMPTS_DEST"
cat > "$PROMPTS_DEST/README.md" <<EOF
# prompts/ — your tutor prompt goes here

This installer deliberately does NOT bundle any prompt. The generic runner ships
with \`../harn/agent/instructions.md\` (English, model-agnostic) as the default
\`TUTOR_SYSTEM\`.

To use a course-specific prompt:
  1. copy your compiled prompt file here (e.g. \`tutor.md\`);
  2. point \`TUTOR_SYSTEM\` at it (see \`../env.example.sh\` and
     \`../zed-agent-servers.json\`), or drop it as \`tutor.md\` and export
     \`TUTOR_SYSTEM=$PREFIX/prompts/tutor.md\`.
EOF
log "Scaffold ready: $PROMPTS_DEST/README.md"

# --- 6. env example + Zed wiring + docs server launcher -------------------------------
[ "$WITH_CORPUS" = 1 ] && CORPUS_WWW="$CORPUS_DEST/www" || CORPUS_WWW="(corpus not installed)"
[ "$WITH_CORPUS" = 1 ] && SECTIONS_PATH="$CORPUS_DEST/sections.json" || SECTIONS_PATH=""
[ "$WITH_CORPUS" = 1 ] && DOCROOT="$CORPUS_DEST" || DOCROOT="$PREFIX"

cat > "$PREFIX/env.example.sh" <<EOF
# env.example.sh — environment for the harn Socratic tutor (generated by install.sh)
# Source it in a shell session, or use these values in Zed agent_servers env.
export TUTOR_PROVIDER="$PROVIDER"
export TUTOR_MODEL="$MODEL"
export TUTOR_DOCROOT="$DOCROOT"
export TUTOR_SYSTEM="$SYSTEM_DEST"
export TUTOR_DOCS_BASE="$DOCS_BASE"
export TUTOR_SECTIONS="$SECTIONS_PATH"
export TUTOR_TOOL_FORMAT="json"
export TUTOR_THINKING="$THINKING"
export TUTOR_TEMPERATURE="$TEMPERATURE"
export TUTOR_MAX_TOKENS="$MAX_TOKENS"
EOF
if [ "$WITH_FALLBACK" = 1 ]; then
  cat >> "$PREFIX/env.example.sh" <<EOF

# Optional remote fallback when the local router is down.
# Set TUTOR_API_KEY in your environment (never commit it).
export TUTOR_FALLBACK="llamacpp_remote"
EOF
fi

# Zed agent_servers snippet (merge into ~/.config/zed/settings.json or use
# Agent Settings > External Agents > Add Custom Agent).
cat > "$PREFIX/zed-agent-servers.json" <<EOF
{
  "agent_servers": {
    "Socratic tutor": {
      "type": "custom",
      "command": "$RUNNER_DEST/run.sh",
      "args": [],
      "env": {
        "TUTOR_PROVIDER": "$PROVIDER",
        "TUTOR_MODEL": "$MODEL",
        "TUTOR_DOCROOT": "$DOCROOT",
        "TUTOR_SYSTEM": "$SYSTEM_DEST",
        "TUTOR_DOCS_BASE": "$DOCS_BASE",
        "TUTOR_SECTIONS": "$SECTIONS_PATH",
        "TUTOR_TOOL_FORMAT": "json",
        "TUTOR_THINKING": "$THINKING",
        "TUTOR_TEMPERATURE": "$TEMPERATURE",
        "TUTOR_MAX_TOKENS": "$MAX_TOKENS"
      }
    }
  }
}
EOF

cat > "$PREFIX/start-docs.sh" <<EOF
#!/usr/bin/env bash
# Serve the rendered book so citation links work (TUTOR_DOCS_BASE=$DOCS_BASE).
set -euo pipefail
exec python3 -m http.server $DOCS_PORT --directory "$CORPUS_WWW"
EOF
chmod +x "$PREFIX/start-docs.sh"

# --- validate the installed runner -----------------------------------------------------
if [ -n "$HARN_BIN" ]; then
  log "Validating the installed runner with harn check..."
  (cd "$RUNNER_DEST" && "$HARN_BIN" check main.harn && "$HARN_BIN" check --workspace)
else
  warn "no harn runtime found — skipping validation. Once harn is available, run from $RUNNER_DEST:"
  warn "  harn check main.harn && harn check --workspace"
fi

# --- summary ----------------------------------------------------------------------------
log "Install complete at: $PREFIX"
cat <<EOF

What was installed
  runner   : $RUNNER_DEST
  corpus   : $CORPUS_DEST  (doc root for the read-only tools)
  prompts  : $PROMPTS_DEST (scaffold only — add your prompt file there)
  provider : $HARN_PROVIDERS
  harn     : ${HARN_BIN:-not found (enable the Zed 'Harn' external agent)}

Next steps
  1. Serve one of the tutor models on port 8025:
     llama-server -m ${MODELS_DIR:-path/to/}Qwen3.5-4B-UD-Q8_K_XL.gguf -ngl 999 --port 8025
  2. Serve the rendered book (citation links):
     $PREFIX/start-docs.sh
  3. Register the runner in Zed — merge $PREFIX/zed-agent-servers.json into
     ~/.config/zed/settings.json, or Agent Settings > External Agents > Add Custom Agent
     (command: $RUNNER_DEST/run.sh), then pick "Socratic tutor" in the Agent panel.
     run.sh resolves the harn runtime itself (PATH, then Zed's ACP registry); if none
     is found it prints how to fetch it.
  4. Point TUTOR_SYSTEM at your prompt file once you have one (see $PROMPTS_DEST/README.md).
     The generic default is: $SYSTEM_DEST
  5. Env reference: $PREFIX/env.example.sh   (docs: $RUNNER_DEST/README.md)

French model prompts were intentionally NOT installed.
EOF
