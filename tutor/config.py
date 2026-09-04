"""Configuration du tuteur : profils modèles, chemins, corpus, prompt système.

Lit ``config.json`` à la racine de `Tutor-agent/` — le chemin est résolu
relativement à ce module (``Path(__file__)``), donc indépendant du ``cwd`` de
la session ACP (l'élève peut ouvrir l'agent depuis n'importe quel dossier).

Un fichier ``config.local.json`` (optionnel, **jamais committé**) est fusionné
par-dessus ``config.json`` : il permet de rediriger ``paths.course_dir`` /
``docs.py_dir`` vers une autre copie du corpus (machine de labo, clé USB). Par
défaut ``config.json`` pointe vers le dépôt jumeau privé
``MIASHS-Configuration-Tutorat``, qui centralise le cours à référencer.

``STUB`` (variable d'env ``TUTOR_STUB=1``) : mode sans serveur LLM ni corpus,
utilisé par les tests unitaires (le moteur renvoie une réponse déterministe).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import docs as _docs_module

BASE_DIR = Path(__file__).resolve().parent.parent            # Tutor-agent/
CONFIG_PATH = BASE_DIR / "config.json"
CONFIG_LOCAL_PATH = BASE_DIR / "config.local.json"
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


def _deep_merge(base: dict, over: dict) -> dict:
    """Fusion récursive : les clefs de ``over`` écrasent celles de ``base``, les
    sous-dicts sont mergés en profondeur (README du jumeau)."""
    out = dict(base)
    for key, value in over.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


_CONFIG = _load(CONFIG_PATH)
if CONFIG_LOCAL_PATH.exists():
    _CONFIG = _deep_merge(_CONFIG, _load(CONFIG_LOCAL_PATH))


def load_config() -> dict:
    """Renvoie le dict brut de config.json (pour introspection / tests)."""
    return _CONFIG


def default_model() -> str:
    return _CONFIG.get("default_model", "ornith-1.5-9B")


def base_url() -> str:
    host = _CONFIG["server"]["host"]
    port = _CONFIG["server"]["port"]
    return f"http://{host}:{port}"


def is_remote(model: str) -> bool:
    """Le modèle est servi par un llama-server distant : il ne faut alors ni
    démarrer, ni redémarrer, ni arrêter de llama-server local pour lui (§4)."""
    return bool((profile(model).get("endpoint") or "").strip())


def fallback_endpoint() -> str | None:
    """Endpoint de secours quand le routeur localhost 8025 n'est pas joignable.

    Où : `config.json` → `fallback.endpoint`, vide par défaut (fallback désactivé).
    Peut être un hôte distant (<https://llm-serve.exemple.fr>) servi par
    llama-swap/llama-server, distinct de `localhost:8025`.
    """
    return (_CONFIG.get("fallback", {}).get("endpoint") or "").strip() or None


def fallback_api_key() -> str | None:
    """Clef d'API pour le fallback distant (`fallback.api_key`), si l'endpoint
    exige une authentification. `` → aucun header d'auth envoyé.

    La clef circule uniquement (1) dans le header `Authorization: Bearer …` vers
    le fallback, (2) dans un éventuel ping de disponibilité de `server.py::ensure`.
    """
    return (_CONFIG.get("fallback", {}).get("api_key") or "").strip() or None


_fallback_engaged: set[str] = set()


def set_fallback_active(model: str, active: bool) -> None:
    """Marque (en mémoire) que `model` est actuellement desservi par le fallback
    distant plutôt que par un llama-server local — pour que `backend_for` et
    `model_base_url` routent vers `fallback.endpoint` pendant la session."""
    (lambda: (_fallback_engaged.update([model]) if active else _fallback_engaged.discard(model)))()


def is_fallback_active(model: str) -> bool:
    return model in _fallback_engaged


def model_base_url(model: str) -> str:
    """Base URL de l'API OpenAI-compatible pour `model`.

    Utilise dans l'ordre : l'endpoint du profil (`profiles.<m>.endpoint`), le
    fallback distant (`fallback.endpoint`) si le modèle y est basculé par
    `server.py::ensure` (routeur local injoignable), sinon le routeur local.
    """
    prof = profile(model)
    endpoint = (prof.get("endpoint") or "").strip()
    if endpoint:
        return endpoint
    if is_fallback_active(model):
        fallback = fallback_endpoint()
        if fallback:
            return fallback
    return base_url()


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


def course_dir() -> str:
    """Racine du cours référencé (côté enseignant), ou ``""``.

    ``paths.course_dir`` (défaut : le dépôt jumeau `MIASHS-Configuration-Tutorat`)
    pointe vers le répertoire qui contient ``Courses/``, ``www/`` et
    ``sections.json`` — le corpus y est centralisé ; on le redirige au besoin via
    ``config.local.json`` (machine de labo, clé USB). Vide → repli sur
    ``paths.corpus_root``.
    """
    value = (_CONFIG.get("paths", {}).get("course_dir") or "").strip()
    return _resolve_path(value) if value else ""


def gguf_dir() -> str:
    return _resolve_path(_CONFIG["paths"]["gguf_dir"])


def external_template() -> str:
    return _resolve_path(_CONFIG["paths"]["external_template"])


def corpus_root() -> str:
    """Racine des fichiers ``.qmd`` du corpus : ``<cours>/Courses`` si le cours
    est déclaré (``paths.course_dir``), sinon ``paths.corpus_root`` du dépôt."""
    cd = course_dir()
    if cd:
        return os.path.join(cd, "Courses")
    return _resolve_path(_CONFIG["paths"]["corpus_root"])


def sessions_dir() -> Path:
    return SESSIONS_DIR


def docs() -> dict:
    """Section « docs » de config.json (doc locale cliquable)."""
    return dict(_CONFIG.get("docs") or {})


def docs_base_url() -> str:
    """URL de base du serveur de doc ; sans slash final."""
    return str(docs().get("base_url", "http://127.0.0.1:8765")).rstrip("/")


def book_base_url() -> str:
    """URL de base du book public pour les liens du modèle.

    Retourne la base effective du serveur docs (port de repli si besoin),
    sans slash final. C'est la valeur injectée dans le prompt système
    pour que le modèle produise des liens markdown natifs.
    """
    return _docs_module.effective_base_url().rstrip("/")


def docs_port() -> int:
    return int(docs().get("port", 8765))


def www_dir() -> str:
    """Dossier servi par tutor.docs (copie locale des pages HTML du book)."""
    cd = course_dir()
    if cd:
        return os.path.join(cd, "www")
    return _resolve_path(docs().get("www_dir", "corpus/www"))


def sections_json() -> Path:
    """Carte ligne→section générée par tools/build_docs_map.py."""
    cd = course_dir()
    if cd:
        return Path(os.path.join(cd, "sections.json"))
    return Path(_resolve_path(docs().get("sections_json", "corpus/sections.json")))


def py_dir() -> str:
    """Dossier de la doc Python officielle locale (``docs.py_dir``), ou ``""``.

    Si renseigné, ``tutor.docs`` le sert sous ``/py/`` et les citations
    ``python:<ref>`` sont réécrites vers ce miroir local (hors-ligne) ; sinon
    elles pointent vers https://docs.python.org/3/ (en ligne).
    """
    value = (docs().get("py_dir") or "").strip()
    return _resolve_path(value) if value else ""


def python_doc_base_url() -> str:
    """Base des citations ``python:<ref>`` (doc Python officielle).

    Miroir local ``BASE/py`` quand ``docs.py_dir`` est configuré (hors-ligne), sinon
    ``docs.python_doc_url`` (défaut https://docs.python.org/3/ — en ligne)."""
    if py_dir():
        return f"{docs_base_url()}/py"
    return (docs().get("python_doc_url") or "https://docs.python.org/3/").rstrip("/")


def corpus_files() -> dict[str, str]:
    """Carte clé -> nom de fichier .qmd du corpus (12 chapitres + 14 sujets de
    TP de l'annexe B, sous ``Applications/``). Les TP ne sont pas rendus en HTML
    par le book : leur page locale ancrée est générée par
    ``tools/build_docs_map.py`` dans ``www/Courses/Applications``."""
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


def build_system(model: str, cwd: str = "") -> str:
    """Prompt système projet-local, convention AGENTS : ``AGENTS.<model>.md``
    prime sur ``AGENTS.md``, tous deux lus dans le projet ouvert (``cwd``).

    Aucun prompt par défaut : si aucun de ces fichiers n'existe, renvoie ``""``
    (pas de message système — le modèle ne reçoit que la conversation et les
    outils). Les prompts FR vivent côté enseignant (jumeau
    `MIASHS-Configuration-Tutorat/prompts/`) et sont **déposés** dans le
    workspace projet comme ``AGENTS.md``/``AGENTS.<model>.md`` — aucun script,
    jamais embarqués dans le runner.

    Un bloc **Références** est préfixé au texte (avant le contenu AGENTS) pour
    donner au modèle la base URL du book et les conventions de liens. C'est
    injecté ici (pas dans les fichiers de prompt) car la base URL change par
    session (port dynamique).
    """
    base_url = book_base_url()
    refs = (
        "## Références\n"
        "- Base URL du book : **" + base_url + "**\n"
        "- Cours : `[nom](BASE_URL/Courses/nom.html)`\n"
        "- TP / applications : `[nom](BASE_URL/Courses/Applications/nom.html)`\n"
        "- Doc Python : `[python:module](https://docs.python.org/3/library/module.html)`\n"
        "- Écris les liens en markdown natif `[texte](url)` — ne réécris jamais "
        "un lien existant dans `[...]([...]url...)](url)` ni utilise la syntaxe "
        "wiki `[[...]]`.\n"
        "- Si tu ne connais pas l'ancre HTML exacte, un lien vers la page est "
        "suffisant.\n"
    )
    if cwd:
        root = Path(cwd)
        for fname in (f"AGENTS.{model}.md", "AGENTS.md"):
            candidate = root / fname
            if candidate.is_file():
                return refs + candidate.read_text(encoding="utf-8").strip()
    return refs


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
    message user (alternance stricte user/assistant).

    Coïncide avec le mode brut d'un profil (``mode: "brut"``, cf. ``is_brut``).
    Les trois profils actuels (qwen3.5-4B, ornith-1.5-9B, gemma-4-E4B) acceptent
    tous un vrai message système."""
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
