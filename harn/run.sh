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
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec harn serve acp "$HERE/main.harn" "$@"
