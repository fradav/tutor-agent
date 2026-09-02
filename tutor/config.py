"""Configuration du tuteur : profils modèles, chemins, corpus, prompt système.

Lit ``config.json`` à la racine de `Tutor-agent/` — le chemin est résolu
relativement à ce module (``Path(__file__)``), donc indépendant du ``cwd`` de
la session ACP (l'élève peut ouvrir l'agent depuis n'importe quel dossier).

``STUB`` (variable d'env ``TUTOR_STUB=1``) : mode sans serveur LLM ni corpus,
utilisé par les tests unitaires (le moteur renvoie une réponse déterministe).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent            # Tutor-agent/
CONFIG_PATH = BASE_DIR / "config.json"
PROMPTS_DIR = BASE_DIR / "tutor" / "prompts"
SESSIONS_DIR = BASE_DIR / "sessions"                         # runtime (transcripts + état)

# Mode test : pas de serveur, pas de corpus, réponses déterministes.
STUB = os.environ.get("TUTOR_STUB") == "1"

# Sampling socle Qwen (défaut embarqué de complete(), comme dans le harnais).
DEFAULT_SAMPLING = {
    "temperature": 0.6,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 0.0,
    "repeat_penalty": 1.0,
}


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_CONFIG = _load(CONFIG_PATH)


def load_config() -> dict:
    """Renvoie le dict brut de config.json (pour introspection / tests)."""
    return _CONFIG


def default_model() -> str:
    return _CONFIG.get("default_model", "ornith-1.5-9B")


def base_url() -> str:
    host = _CONFIG["server"]["host"]
    port = _CONFIG["server"]["port"]
    return f"http://{host}:{port}"


def model_base_url(model: str) -> str:
    """URL effective du backend d'un modèle : endpoint distant si le profil le
    définit (clef ``endpoint`` non vide), sinon le serveur local de config.json.

    Permet de servir un modèle via un llama-server hébergé ailleurs (machine
    distante, SSH, clé USB montée chez l'élève…) sans rien changer au moteur.
    """
    endpoint = (profile(model).get("endpoint") or "").strip()
    if endpoint:
        return endpoint.rstrip("/")
    return base_url()


def is_remote(model: str) -> bool:
    """Le modèle est servi par un llama-server distant : il ne faut alors ni
    démarrer, ni redémarrer, ni arrêter de llama-server local pour lui (§4)."""
    return bool((profile(model).get("endpoint") or "").strip())


def max_tokens() -> int:
    return _CONFIG["server"]["max_tokens"]


def llama_bin() -> str:
    return _CONFIG["server"]["llama_bin"]


def _resolve_path(value: str) -> str:
    """Résout un chemin de config.json en chemin absolu.

    Les chemins **relatifs** sont ancrés sur ``BASE_DIR`` (Tutor-agent/) — c'est
    ce qui rend le livrable autonome (``models/``, ``corpus/Courses/``…) robuste
    quel que soit le ``cwd`` de la session ACP de l'élève. Les chemins absolus
    (machine de dev, clé USB montée ailleurs) passent tels quels.
    """
    value = os.path.expanduser(value)
    if os.path.isabs(value):
        return os.path.abspath(value)
    return str((BASE_DIR / value).resolve())


def gguf_dir() -> str:
    return _resolve_path(_CONFIG["paths"]["gguf_dir"])


def external_template() -> str:
    return _resolve_path(_CONFIG["paths"]["external_template"])


def corpus_root() -> str:
    return _resolve_path(_CONFIG["paths"]["corpus_root"])


def sessions_dir() -> Path:
    return SESSIONS_DIR


def docs() -> dict:
    """Section « docs » de config.json (doc locale cliquable)."""
    return dict(_CONFIG.get("docs") or {})


def docs_base_url() -> str:
    """URL de base du serveur de doc ; sans slash final."""
    return str(docs().get("base_url", "http://127.0.0.1:8765")).rstrip("/")


def docs_port() -> int:
    return int(docs().get("port", 8765))


def www_dir() -> str:
    """Dossier servi par tutor.docs (copie locale des pages HTML du book)."""
    return _resolve_path(docs().get("www_dir", "corpus/www"))


def sections_json() -> Path:
    """Carte ligne→section générée par tools/build_docs_map.py."""
    return Path(_resolve_path(docs().get("sections_json", "corpus/sections.json")))


def corpus_files() -> dict[str, str]:
    """Carte clé courte -> nom de fichier .qmd du corpus (00…06)."""
    return dict(_CONFIG["corpus_files"])


def profiles() -> dict:
    return dict(_CONFIG["profiles"])


def profile(model: str | None) -> dict:
    """Profil de sampling/template pour un modèle ; défaut config.json."""
    prof = _CONFIG["profiles"].get(model or default_model())
    if prof is None:
        raise KeyError(f"profil inconnu: {model}")
    return prof


def model_path(model: str) -> str:
    """Chemin absolu du GGUF du modèle (pour le transcript / démarrage serveur)."""
    return os.path.join(gguf_dir(), profile(model)["gguf"])


def build_system(model: str) -> str:
    """Système tuteur = variante ``tuteur-<model>.md`` + ``PREAMBLE.md``.

    Pour ministral-3-8B-Reasoning (mode brut, aucun message système), le texte
    renvoyé ici est embarqué dans le premier message étudiant (voir
    engine.run_turn) ; pour qwen3.5-4B/ornith-1.5-9B/gemma-4-E4B il est posé
    comme premier message ``role: system``.
    """
    prof = profile(model)
    variant = PROMPTS_DIR / prof["prompt"]
    parts = []
    if variant.exists():
        parts.append(variant.read_text(encoding="utf-8").strip())
    else:
        socle = PROMPTS_DIR / "tuteur-socratique-AGENTS.md"
        if socle.exists():
            parts.append(socle.read_text(encoding="utf-8").strip())
        else:
            parts.append("Tu es un tuteur de programmation socratique "
                         "pour un·e étudiant·e de master MIASHS.")
    preamble = PROMPTS_DIR / "PREAMBLE.md"
    if preamble.exists():
        parts.append(preamble.read_text(encoding="utf-8").strip())
    return "\n\n".join(parts)


def is_brut(model: str) -> bool:
    """Mode BRUT : le raisonnement vit dans le content ([THINK]…[/THINK]),
    extrait côté moteur ; réinjection du content brut multi-tours."""
    return profile(model).get("mode") == "brut"


def is_gemma(model: str) -> bool:
    """Format de canal gemma-4-E4B (template embarqué) : le raisonnement
    interleaved sort dans le content balisé
    ``<|channel>thought\n…<channel|>`` — extrait côté moteur par
    ``GemmaStreamSplitter`` (pensées/visible), réinjection ``reasoning_content``
    (mode normal, pas exactement le format des autres modèles)."""
    return profile(model).get("marker") == "gemma"


def embeds_instructions(model: str) -> bool:
    """Aucun message système : les consignes tuteur sont embarquées dans le
    premier message étudiant et tout le contexte du tour est fusionné en un seul
    message user (alternance stricte user/assistant du template ministral).

    Pour nos 4 profils, coïncide avec le mode brut (seul
    ministral-3-8B-Reasoning est sans système ; gemma-4-E4B accepte un vrai
    message système comme qwen3.5-4B/ornith-1.5-9B)."""
    return is_brut(model)


def uses_native_tools(model: str) -> bool:
    """Mode d'exécution des lectures du corpus pour un profil.

    ``tools: "ask"`` dans le profil → mode « Ask » : l'engine exécute le
    ``tool_plan`` de la session et injecte les blocs QUOTE dans le prompt,
    et **aucun** ``tools`` n'est passé au backend (le modèle lit des blocs
    de texte, jamais d'outil à appeler). Clef absente (défaut de tous les
    autres profils) → outils natifs : le modèle déclenche lui-même
    ``grep_files``/``read_lines`` via ``tool_calls``.
    """
    return profile(model).get("tools") != "ask"
