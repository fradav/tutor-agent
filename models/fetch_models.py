#!/usr/bin/env python3
"""Télécharge les 4 modèles du tuteur dans ``models/`` (ou ``--dest``).

Décision §1.3 (TODO L70-78) : aucun .gguf n'est versionné dans le livrable ;
ce script les récupère à l'installation (machine de l'élève). La copie physique
vers la clé USB se fait à la construction, via ``scripts/assemble_usb.py``
(hors ce livrable).

Le template externe ``qwen3.5-chat-template.jinja`` n'est PAS à télécharger :
c'est un artefact adapté à la main pour llama.cpp (gestion think/response,
tool_instructions), déjà fourni localement dans ``models/``. Le
``chat_template.jinja`` distant de HF diffère (153 lignes vs 264) et ne doit
pas être re-fetched à sa place.

Usage :
    python3 models/fetch_models.py                 # les 4 → models/
    python3 models/fetch_models.py --dest /chemin  # autre destination
    python3 models/fetch_models.py --only Qwen3.5-4B-UD-Q8_K_XL.gguf
    python3 models/fetch_models.py --list          # résumé (noms + tailles)

Reprise : ``curl -L -C -`` reprend un téléchargement interrompu ; relancer le
script suffit. Un fichier déjà présent avec la taille attendue est laissé tel
quel (skippé).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# nom de fichier -> (URL de résolution Hugging Face, taille attendue en octets)
# Tailles relevées sur les .gguf de référence (machine de dev).
MODELS: dict[str, tuple[str, int]] = {
    # qwen3.5-4B : seul des 4 à utiliser le template externe (qwen3.5-chat-template.jinja)
    "Qwen3.5-4B-UD-Q8_K_XL.gguf": (
        "https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-UD-Q8_K_XL.gguf",
        5_952_048_288,
    ),
    # ornith-1.5-9B : template EMBARQUÉ (pas de --chat-template-file)
    "Ornith-1.5-9B-Q4_K_M.gguf": (
        "https://huggingface.co/ornith-ai/Ornith-1.5-9B-GGUF/resolve/main/Ornith-1.5-9B-Q4_K_M.gguf",
        5_780_090_816,
    ),
    # ministral-3-8B-Reasoning : template EMBARQUÉ, mode BRUT
    "Ministral-3-8B-Reasoning-2512-Q4_K_M.gguf": (
        "https://huggingface.co/mistralai/Ministral-3-8B-Reasoning-2512-GGUF/resolve/main/Ministral-3-8B-Reasoning-2512-Q4_K_M.gguf",
        5_198_910_368,
    ),
    # gemma-4-E4B : template EMBARQUÉ, preserve thinking interleaved
    # (<|channel>thought…<channel|>, mode normal)
    "gemma-4-E4B_q4_0-it.gguf": (
        "https://huggingface.co/google/gemma-4-E4B-it-qat-q4_0-gguf/resolve/main/gemma-4-E4B_q4_0-it.gguf",
        5_154_941_280,
    ),
}


def _fmt(n: int) -> str:
    return f"{n / 1e9:.2f} Go"


def _download(url: str, dest: str, expected: int) -> bool:
    """Télécharge (ou reprend) ; True si le fichier final est en place."""
    if os.path.exists(dest) and os.path.getsize(dest) >= expected:
        return True
    partial = dest + ".part"
    cmd = [
        "curl", "-L", "-C", "-", "--fail", "--retry", "3",
        "--progress-bar", "-o", partial, url,
    ]
    print(f"  téléchargement de {os.path.basename(dest)} (attendu ~{_fmt(expected)}) …")
    rc = subprocess.call(cmd)
    if rc != 0:
        print(f"  ⚠ curl a échoué (rc={rc}) — relancer le script reprendra le .part",
              file=sys.stderr)
        return False
    os.replace(partial, dest)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="fetch_models",
        description="Récupère les .gguf du tuteur (pas le template, fourni à part).",
    )
    ap.add_argument("--dest", default=HERE, help="répertoire cible (défaut : models/)")
    ap.add_argument("--only", nargs="+", choices=sorted(MODELS),
                    help="sous-ensemble de fichiers (défaut : les 4)")
    ap.add_argument("--list", action="store_true", help="résumé (noms + tailles) puis sortie")
    args = ap.parse_args()

    dest = os.path.abspath(os.path.expanduser(args.dest))
    targets = args.only or sorted(MODELS)

    if args.list:
        for name in targets:
            url, size = MODELS[name]
            print(f"{name:44s} ~{_fmt(size):>9s}  {url}")
        return 0

    os.makedirs(dest, exist_ok=True)
    failed = 0
    for name in targets:
        url, size = MODELS[name]
        path = os.path.join(dest, name)
        if os.path.exists(path) and os.path.getsize(path) >= size:
            print(f"  ✓ {name} déjà présent ({_fmt(os.path.getsize(path))}) — skippé")
            continue
        print(f"  → {path}")
        if not _download(url, path, size):
            failed += 1

    if failed:
        print(f"\n{failed} téléchargement(s) en échec — relancer pour reprendre.", file=sys.stderr)
        return 1
    if not args.only:
        print("\nTous les .gguf sont en place.", file=sys.stderr)
    if targets:
        print("(les .gguf pèsent ~5 Go chacun — prévoir ~22 Go libres)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
