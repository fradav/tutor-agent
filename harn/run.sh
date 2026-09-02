#!/usr/bin/env bash
#
# Launch the generic ACP runner: serve main.harn over ACP.
#
#   ./run.sh                         # stdio (default; what Zed expects)
#   ./run.sh --transport websocket   # remote/WebSocket transport
#   ./run.sh --api-key SECRET        # require ACP authenticate
#
# Configuration lives in environment variables (see README.md):
#   TUTOR_PROVIDER TUTOR_MODEL TUTOR_DOCROOT TUTOR_SYSTEM TUTOR_DOCS_BASE
#   TUTOR_SECTIONS TUTOR_TOOL_FORMAT TUTOR_THINKING TUTOR_EFFORT
#   TUTOR_TEMPERATURE TUTOR_MAX_TOKENS TUTOR_FALLBACK TUTOR_DEBUG
#
# The harn runtime is registry-led: no global install is required. We resolve
# `harn` from PATH first, then fall back to the binary that Zed's ACP Registry
# downloaded for its own "Harn" external agent (install.sh does not copy it).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Point harn at the in-prefix provider config installed by install.sh, unless
# the user already set HARN_PROVIDERS_CONFIG. When the file is absent (dev
# checkout), leave the variable unset so harn falls back to its global config.
if [ -f "$HERE/providers.toml" ]; then
  export HARN_PROVIDERS_CONFIG="${HARN_PROVIDERS_CONFIG:-$HERE/providers.toml}"
fi

# Numeric version comparison: ver_gt A B is true when A > B (numeric, dot-split).
ver_gt() {
  local a b
  IFS=. read -r -a a <<< "$1"
  IFS=. read -r -a b <<< "$2"
  while [ ${#a[@]} -lt 3 ]; do a+=(0); done
  while [ ${#b[@]} -lt 3 ]; do b+=(0); done
  [ "${a[0]}" -gt "${b[0]}" ] && return 0
  [ "${a[0]}" -lt "${b[0]}" ] && return 1
  [ "${a[1]}" -gt "${b[1]}" ] && return 0
  [ "${a[1]}" -lt "${b[1]}" ] && return 1
  [ "${a[2]}" -gt "${b[2]}" ] && return 0
  return 1
}

# Print the path of a usable harn binary, or nothing.
find_harn() {
  local bin
  bin="$(command -v harn 2>/dev/null)" && [ -x "$bin" ] && { printf '%s\n' "$bin"; return 0; }
  local base dir ver best bestv
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
if [ -z "$HARN_BIN" ]; then
  echo "harn runtime not found (checked PATH and Zed's ACP registry)." >&2
  echo "Fetch it one of two ways:" >&2
  echo "  1. enable the 'Harn' external agent once in Zed" >&2
  echo "     (Agent Settings > External Agents > Harn) — this downloads it;" >&2
  echo "  2. or install a global binary:  curl -fsSL https://harnlang.com/install.sh | sh" >&2
  exit 1
fi

exec "$HARN_BIN" serve acp "$HERE/main.harn" "$@"
