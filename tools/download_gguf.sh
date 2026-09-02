#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Télécharge les GGUF des 3 modèles retenus du tuteur ACP (qwen3.5, ornith,
# gemma4) + le template Qwen3.5 via `uvx --from huggingface-hub hf download`,
# dans $GGUF_TUTORDIR. (Ministral n'est plus servie.)
#
# Usage :
#   GGUF_TUTORDIR=/chemin/vers/les/gguf ./download_gguf.sh            # tout
#   GGUF_TUTORDIR=/chemin ./download_gguf.sh qwen3.5                 # un seul
#
# Repo gated (clef requise) : gemma (google/…) → exporter HF_TOKEN (utilisé
# par `hf download` sans être jamais écrit ni loggé).
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GGUF_TUTORDIR="${GGUF_TUTORDIR:-$(cd "$SCRIPT_DIR/.." && pwd)/models}"
mkdir -p "$GGUF_TUTORDIR"

# format : repo|fichier|tag|description
FILES=(
  "Manojb/Qwen3.5-4B-UD-Q8_K_XL.gguf|Qwen3.5-4B-UD-Q8_K_XL.gguf|qwen3.5|Qwen3.5-4B-UD-Q8_K_XL"
  "ornith-ai/Ornith-1.5-9B-GGUF|Ornith-1.5-9B-Q4_K_M.gguf|ornith|Ornith-1.5-9B-Q4_K_M"
  "google/gemma-4-E4B-it-qat-q4_0-gguf|gemma-4-E4B_q4_0-it.gguf|gemma4|gemma-4-E4B_q4_0-it (gated : HF_TOKEN requis)"
  "Qwen/Qwen3.5-4B|chat_template.jinja|template|chat_template.jinja (template Qwen3.5)"
)

filter="${1:-}"
for entry in "${FILES[@]}"; do
  IFS='|' read -r repo file tag desc <<<"$entry"
  if [[ -n "$filter" && "$tag" != "$filter" ]]; then
    echo "== $file : ignoré (filtre '$filter')"
    continue
  fi
  echo "== $desc"
  echo "   uvx --from huggingface-hub hf download $repo $file --local-dir $GGUF_TUTORDIR"
  uvx --from huggingface-hub hf download "$repo" "$file" --local-dir "$GGUF_TUTORDIR"
done

# Le template HF s'appelle chat_template.jinja ; le preset local attend
# qwen3.5-chat-template.jinja (config.json → paths.external_template).
TPL_SRC="$GGUF_TUTORDIR/chat_template.jinja"
TPL_DST="$GGUF_TUTORDIR/qwen3.5-chat-template.jinja"
if [[ -f "$TPL_SRC" && ! -f "$TPL_DST" ]]; then
  cp "$TPL_SRC" "$TPL_DST"
  echo "== template copié vers $TPL_DST"
fi

echo "== Terminé. GGUF_TUTORDIR=$GGUF_TUTORDIR"
