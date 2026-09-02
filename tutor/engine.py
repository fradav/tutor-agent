"""Moteur tuteur socratique — portage de ``run_turn`` du harnais « Ask ».

Chaque tour :
1. reçoit le message étudiant (éventuellement embarqué avec les consignes tuteur
   pour les profils sans système, cf. ministral-3-8B-Reasoning) ;
2. passe les outils natifs (``grep_files`` / ``read_lines``, spec OpenAI ``tools``)
   au backend : c'est **le modèle** qui demande lui-même les lectures du corpus
   (mode outils standard, Option B) — l'engine exécute le vrai outil (lecture
   seule, résultats réels fichier:ligne ou « no match ») et lui renvoie le
   résultat en ``role:"tool"`` ;
3. exécute éventuellement le code étudiant → bloc PYTHON-RUN ;
4. construit les messages selon le profil (system + un user pour qwen3.5-4B /
   ornith-1.5-9B ; tout fusionné en un seul user pour ministral-3-8B-Reasoning) ;
5. appelle le backend en **streaming** (``stream_complete``), boucle outillée
   (jusqu'à ``MAX_TOOL_ROUNDS`` appels si tool_calls), **retry si contenu vide** ;
6. réinjecte le raisonnement multi-tours selon le profil (BRUT
   ministral-3-8B-Reasoning / reasoning_content qwen3.5-4B-ornith-1.5-9B) ;
7. persiste état + transcript (même format que ``transcripts/*.json`` du
   Playground) et retourne le dict du tour.

``run_turn_stream`` est le cœur : un **générateur** de ``(kind, text)`` (``kind``
∈ {"reasoning", "content"}) qui streame le raisonnement et le contenu au fil de
l'eau ; le dict du tour est porté par ``StopIteration.value``. Le raisonnement
est séparé du contenu chunk par chunk par un splitter
(``BrutStreamSplitter`` [THINK]…[/THINK] pour ministral-3-8B-Reasoning,
``QwenStreamSplitter`` `` thinking\n…\n response\n`` pour
qwen3.5-4B/ornith-1.5-9B, ``GemmaStreamSplitter``
``<|channel>thought\n…<channel|>`` pour gemma-4-E4B) — un marqueur peut
être coupé entre deux chunks.

``run_turn`` (non streamé, pour compat) est le drain de ``run_turn_stream``.

Mode STUB (``TUTOR_STUB=1``) : réponse déterministe sans serveur ni corpus,
pour les tests unitaires — le tour est retourné sans écrire sur disque.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from . import config
from .llm import stream_complete
from .tools import format_python, format_quote, grep_files, read_lines, run_python

# Nombre raisonnable de tokens pour le body /v1/chat/completions : rester sur
# ~1/8 du contexte (la config stocke le contexte -c 32768 ; le passer en entier
# au body gaspillerait énormément / risquerait le timeout sur les 3–9B locaux).
CALL_MAX_TOKENS = max(1024, config.max_tokens() // 8)

# Retry « contenu visible vide » (ministral-3-8B-Reasoning « pense sans
# répondre » : think émis mais [/THINK] non fermé → le splitter classe tout en
# raisonnement). Le taux de fermeture est probabiliste (~40-60 % par essai,
# instable dans le temps) ; le retry multiple fiabilise, puis la recovery
# close-and-continue (étape 5) récupère la réponse avant le fallback ultime.
RETRY_ATTEMPTS = 3

FALLBACK_RESPONSE = ("Je réfléchis à ta question — peux-tu la reformuler ou "
                     "préciser ce que tu cherches à faire ?")

# Récupération « close-and-continue » (ministral-3-8B-Reasoning). Quand le
# modèle a « pensé sans répondre » ([THINK] ouvert jamais fermé → réponse
# englobée, invisible pour le splitter), on ferme le tag nous-mêmes, on
# réinjecte le raisonnement, et on demande la réponse à l'étudiant. Testé 8/8
# sur T2 (probe_close) ; le format neutre (sans tags) ne marche que 4/8 — le
# `[/THINK]` littéral est le signal fort qui débloque le modèle.
RECOVERY_ATTEMPTS = 2
RECOVERY_PROMPT = ("Écris maintenant ta réponse à l'étudiant, en français, "
                   "directement après le [/THINK], sans séparateur ni "
                   "introduction. Ne mets pas ta réponse dans le raisonnement.")

# Spécification OpenAI des outils natifs du tuteur (mode outils standard,
# Option B) : `grep_files` / `read_lines` — lecture seule sur le corpus. C'est
# **le modèle** qui déclenche les appels (tool_choice="auto" dans llm.py) ;
# l'engine exécute le vrai outil (résultats réels fichier:ligne ou « no match »)
# et renvoie le résultat en `role:"tool"`. Volontairement minimal : 2 outils,
# le socle dont les petits modèles se saisissent sans se disperser.
# Rappel des fichiers du corpus (clé=nom), renvoyé dans les descriptions d'outil
# pour que le modèle passe une clé (« 01 ») ou un nom de fichier au lieu d'un
# chemin imaginaire (« Courses », « asyncio.qmd ») qui ne résout à rien.
_CORPUS_HINT = ", ".join(
    f"{k}={f}" for k, f in sorted(config.corpus_files().items())
)

TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "grep_files",
            "description": (
                "Cherche un motif regex dans des fichiers du corpus du cours. "
                "Fichiers du corpus (clé=nom) : " + _CORPUS_HINT + ". "
                "Passez une clé (ex. « 01 ») ou un nom de fichier en `paths`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex à chercher"},
                    "paths": {"type": "array", "items": {"type": "string"},
                              "description": "Fichiers du corpus (clés ou noms), ex. [\"01\"]"},
                    "max_shown": {"type": "integer", "description": "Nb max de lignes", "default": 8},
                },
                "required": ["pattern", "paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_lines",
            "description": (
                "Lit une plage de lignes d'un fichier du corpus. "
                "Fichiers du corpus (clé=nom) : " + _CORPUS_HINT + ". "
                "Passez une clé (ex. « 01 ») ou un nom de fichier en `path`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                              "description": "Clé du corpus (ex. « 01 ») ou nom de fichier"},
                    "start": {"type": "integer", "description": "Première ligne (>= 1)"},
                    "num": {"type": "integer", "description": "Nombre de lignes (>= 1)"},
                },
                "required": ["path", "start", "num"],
            },
        },
    },
]

# Borne la boucle outillée : sans cap, un modèle qui re-demande un outil en
# boucle (path non résolu → 0 match, arguments invalides…) ne s'arrêterait
# jamais. 8 appels backend max par tour, puis on rend ce qu'on a.
MAX_TOOL_ROUNDS = 8

THINK_OPEN = "[THINK]"
THINK_CLOSE = "[/THINK]"


def _partial_suffix(buf: str, marker: str) -> str:
    """Plus long suffixe de ``buf`` qui est un préfixe propre de ``marker``.

    Détecte un marqueur ``[THINK]`` / ``[/THINK]`` coupé entre deux chunks :
    le suffixe partiel est gardé en buffer en attendant la suite.
    """
    for i in range(len(marker) - 1, 0, -1):
        if buf.endswith(marker[:i]):
            return marker[:i]
    return ""


def reinject_raw(reasoning: str, content: str) -> str:
    """Ré-injection ministral-3-8B-Reasoning au format natif "[THINK]…[/THINK]" + réponse.

    Recommandation officielle Mistral (rejouer la trace complète, y compris le
    ThinkChunk). Le modèle, voyant ses propres tags dans l'historique, continue
    de raisonner à chaque tour ; s'il ne ferme pas `[/THINK]`, la procédure
    close-and-continue (étape 5) récupère la réponse.
    """
    r = (reasoning or "").strip()
    c = (content or "").strip()
    if r and c:
        return f"[THINK]{r}[/THINK]\n\n{c}"
    if r:
        return f"[THINK]{r}[/THINK]"
    return c


class BrutStreamSplitter:
    """Sépare en direct un flux BRUT ``[THINK]…[/THINK]`` en canaux.

    Chunk par chunk : le texte hors ``[THINK]`` part en contenu visible, le texte
    dans ``[THINK]…[/THINK]`` en raisonnement — jamais de fuite du raisonnement
    dans le visible (le suffixe partiel d'un marqueur est bufferisé en attente).
    """

    def __init__(self) -> None:
        self.buf = ""
        self.in_think = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        """Ajoute un chunk, rend la liste des ``(kind, text)`` produits."""
        self.buf += chunk
        emitted: list[tuple[str, str]] = []
        while True:
            if self.in_think:
                idx = self.buf.find(THINK_CLOSE)
                if idx == -1:
                    hold = _partial_suffix(self.buf, THINK_CLOSE)
                    if hold:
                        body, self.buf = self.buf[: -len(hold)], hold
                    else:
                        body, self.buf = self.buf, ""
                    if body:
                        emitted.append(("reasoning", body))
                    break
                body, self.buf = self.buf[:idx], self.buf[idx + len(THINK_CLOSE):]
                self.in_think = False
                if body:
                    emitted.append(("reasoning", body))
            else:
                idx = self.buf.find(THINK_OPEN)
                if idx == -1:
                    hold = _partial_suffix(self.buf, THINK_OPEN)
                    if hold:
                        visible, self.buf = self.buf[: -len(hold)], hold
                    else:
                        visible, self.buf = self.buf, ""
                    if visible:
                        emitted.append(("content", visible))
                    break
                visible, self.buf = self.buf[:idx], self.buf[idx + len(THINK_OPEN):]
                self.in_think = True
                if visible:
                    emitted.append(("content", visible))
        return emitted

    def finish(self) -> list[tuple[str, str]]:
        """Fin de flux : vide les buffers. Un ``[THINK]`` resté ouvert ne fuit
        pas dans le visible — son contenu est classé en raisonnement."""
        emitted: list[tuple[str, str]] = []
        if self.in_think:
            if self.buf:
                emitted.append(("reasoning", self.buf))
        else:
            if self.buf:
                emitted.append(("content", self.buf))
        self.buf = ""
        return emitted


class QwenStreamSplitter:
    """Sépare un flux Qwen (qwen3.5-4B / ornith-1.5-9B) en canaux.

    Même logique de bufferisation que ``BrutStreamSplitter`` mais pour le format
    réellement émis par qwen3.5-4B / ornith-1.5-9B (avec ``--reasoning-preserve``
    + ``reasoning_format: "none"``) : le raisonnement sort dans le ``content``
    balisé en XML
    `` thinking\n…\n response\n`` — mesuré sur le serveur réel par od (octets
    ``3c 74 68 69 6e 6b 3e`` : ``<`` + ``think``) ; le tag utilise ``think``
    (5 lettres), pas ``thinking``. On l'extrait au vol pour que ``reasoning`` reste
    non vide et le visible sans artefact. Sans marqueurs, le flux passe tel quel
    en contenu.
    """

    OPEN = "\x3cthink\x3e"
    CLOSE = "\x3c/think\x3e"

    def __init__(self) -> None:
        self.buf = ""
        self.in_think = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        """Ajoute un chunk, rend la liste des ``(kind, text)`` produits."""
        self.buf += chunk
        emitted: list[tuple[str, str]] = []
        while True:
            if self.in_think:
                idx = self.buf.find(self.CLOSE)
                if idx == -1:
                    hold = _partial_suffix(self.buf, self.CLOSE)
                    if hold:
                        body, self.buf = self.buf[: -len(hold)], hold
                    else:
                        body, self.buf = self.buf, ""
                    if body:
                        emitted.append(("reasoning", body))
                    break
                body, self.buf = self.buf[:idx], self.buf[idx + len(self.CLOSE):]
                self.in_think = False
                if body:
                    emitted.append(("reasoning", body))
            else:
                idx = self.buf.find(self.OPEN)
                if idx == -1:
                    hold = _partial_suffix(self.buf, self.OPEN)
                    if hold:
                        visible, self.buf = self.buf[: -len(hold)], hold
                    else:
                        visible, self.buf = self.buf, ""
                    if visible:
                        emitted.append(("content", visible))
                    break
                visible, self.buf = self.buf[:idx], self.buf[idx + len(self.OPEN):]
                self.in_think = True
                if visible:
                    emitted.append(("content", visible))
        return emitted

    def finish(self) -> list[tuple[str, str]]:
        """Fin de flux : vide les buffers. Un ``<thinking>`` resté ouvert ne fuit
        pas dans le visible — son contenu est classé en raisonnement."""
        emitted: list[tuple[str, str]] = []
        if self.in_think:
            if self.buf:
                emitted.append(("reasoning", self.buf))
        else:
            if self.buf:
                emitted.append(("content", self.buf))
        self.buf = ""
        return emitted


class GemmaStreamSplitter(QwenStreamSplitter):
    """Sépare un flux gemma-4-E4B (template embarqué) en canaux.

    Même logique de bufferisation que ``QwenStreamSplitter`` mais pour les
    marqueurs de canal réellement émis par gemma-4-E4B (avec
    ``--reasoning-preserve`` + ``reasoning_format: "none"``) : les pensées
    interleaved sortent dans le ``content`` balisé
    ``<|channel>thought\n…<channel|>`` puis le texte visible en clair — mesuré
    sur le serveur réel. Start/stop jamais trouvés → le flux passe tel quel en
    contenu. C'est une sous-classe (OPEN/CLOSE seuls diffèrent).
    """

    OPEN = "\x3c|channel>thought"
    CLOSE = "\x3cchannel|>"


def _opening_splitter(model: str) -> BrutStreamSplitter | QwenStreamSplitter | GemmaStreamSplitter:
    """Splitter du content selon le profil : BRUT ([THINK]…) vs Qwen XML vs
    gemma (``<|channel|>``)."""
    if config.is_brut(model):
        return BrutStreamSplitter()
    if config.is_gemma(model):
        return GemmaStreamSplitter()
    return QwenStreamSplitter()


def initial_state(
    model: str,
    session_id: str,
    label: str,
    cwd: str = "",
    persona: str | None = None,
    title: str | None = None,
    module: str | None = None,
    focus: str | None = None,
    tool_plan: list | None = None,
) -> dict[str, Any]:
    """État initial d'une session tuteur (structure du harnais, §cmd_new).

    ``tool_plan`` : specs brutes du plan d'outils de la session (le format
    ``_grep``/``_read`` de ``SESSION_DEFS``), utilisées en mode « Ask » (profil
    ``tools: "ask"``) — l'engine les exécute et injecte les QUOTE. Ignoré en
    mode outils natifs.
    """
    prof = config.profile(model)
    no_system_embed = config.embeds_instructions(model)
    system_text = config.build_system(model)
    return {
        "id": session_id,
        "model": model,
        "alias": prof.get("alias", model),
        "model_path": config.model_path(model),
        "cwd": cwd,
        "persona": persona,
        "session": label,
        "title": title if title else "Session tuteur socratique (conversation libre)",
        "module": module if module else "corpus MIASHS — programmation avancée (00→06)",
        "focus": focus if focus else "ancrage sur le matériel du cours, démarche socratique, anti-invention",
        "corpus_root": config.corpus_root(),
        "reasoning_preserve": True,
        "no_system_embed": no_system_embed,
        "think_off": False,
        "system_text": system_text if no_system_embed else "",
        "sampling": prof["sampling"],
        "max_tokens": CALL_MAX_TOKENS,
        "initial_files": [],
        "prompt_variant": prof.get("prompt", "<socle>"),
        "messages": [] if no_system_embed else [{"role": "system", "content": system_text}],
        "turns": [],
        "tool_plan": tool_plan or [],
    }


def _clamp_int(value, default: int = 1) -> int:
    """Entier >= 1 sûr pour `read_lines` (start/num) : la valeur non fournie ou
    non numérique retombe sur le défaut — jamais de `lines[-1]` ni d'index 0."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(default, n)


def _resolve_tool_paths(path: str) -> list[str]:
    """Résout un chemin demandé par le modèle vers des chemins absolus du corpus.

    Ordre de tolérance : clé courte (« 01 ») → fichier .qmd ; nom de fichier
    exact ; glob `*`/`?` → tous les fichiers ; sous-chaîne (le nom demandé est
    contenu dans un nom du corpus ou l'inverse). Sans aucune résolution, rend
    ``[]`` : ``_exec_tool`` produit alors un message « chemin inconnu » explicite
    au lieu d'un 0-match muet que le modèle re-demanderait en boucle.
    """
    files = config.corpus_files()
    root = config.corpus_root()
    if path in files:
        return [os.path.join(root, files[path])]
    if "*" in path or "?" in path:
        return [os.path.join(root, f) for f in files.values()]
    base = os.path.basename(path)
    exact = [os.path.join(root, f) for f in files.values() if base == f]
    if exact:
        return exact
    needle = base.lower().removesuffix(".qmd")
    hits = [
        os.path.join(root, f)
        for f in files.values()
        if needle and (needle in f.lower() or f.lower() in needle)
    ]
    return hits


def _exec_tool(name: str, args: dict) -> tuple[dict, str]:
    """Exécute un tool_call du modèle : lecture seule **réelle** du corpus.

    Rend ``(trace, result)`` — trace au format transcript §3 (``grep`` /
    ``read_file``, cf. l'ancien ``_expand_trace``) pour ``turn["tools"]``, et
    ``result`` texte à renvoyer au modèle en ``role:"tool"``. Arguments JSON
    invalides → résultat d'erreur sans jamais faire tomber la boucle outillée.
    """
    if name == "grep_files":
        requested = args.get("paths") or []
        paths: list[str] = []
        unknown: list[str] = []
        for p in requested:
            resolved = _resolve_tool_paths(p)
            if resolved:
                paths += resolved
            else:
                unknown.append(p)
        total, shown = grep_files(paths, args.get("pattern", ""),
                                  max_shown=args.get("max_shown", 8))
        files = ", ".join(requested)
        trace = {"tool": "grep", "query": args.get("pattern", ""),
                 "files": files, "matches": total}
        if unknown and not paths:
            result = (
                "grep_files: 0 correspondance — chemin(s) inconnu(s) du corpus : "
                + ", ".join(unknown)
                + f". Clés valides : {_CORPUS_HINT}"
            )
        else:
            result = (f"grep_files: {total} correspondances (fichiers: {files})\n"
                      + "\n".join(shown))
        return trace, result
    if name == "read_lines":
        requested = args.get("path", "")
        resolved = _resolve_tool_paths(requested)
        if not resolved:
            return ({"tool": "read_file", "section": requested, "lines": "−"},
                    "read_lines: chemin inconnu du corpus : " + _CORPUS_HINT)
        start = _clamp_int(args.get("start"), 1)
        num = _clamp_int(args.get("num"), 1)
        shown = read_lines(resolved[0], start, num)
        trace = {"tool": "read_file", "section": requested,
                 "lines": f"{start}-{start + num - 1}"}
        return trace, "read_lines\n" + "\n".join(shown)
    return {"tool": "other", "name": name}, f"outil inconnu: {name}"


def _expand_plan(plan: list[dict]) -> list[tuple]:
    """Transforme les specs du plan de session en ``(tool, query, [paths], label, extra)``

    Reprend ``_expand_plan`` du harnais « Ask » (run-session.py.bak-pre-acp) :
    les specs ``_grep``/``_read`` de ``SESSION_DEFS`` sont résolues contre
    ``config.corpus_files()`` + ``config.corpus_root()``. grep → ``extra`` = borne
    max_shown ; read → ``extra`` = (start, num) ; la section de read accepte une
    clé courte (« 01 ») ou le nom de fichier complet.
    """
    files_map = config.corpus_files()
    root = config.corpus_root()
    out: list[tuple] = []
    for spec in plan:
        tool = spec.get("tool")
        if tool == "grep":
            flist = spec["files"] if isinstance(spec["files"], list) else [spec["files"]]
            paths = [os.path.join(root, files_map[f]) for f in flist if f in files_map]
            label = ", ".join(files_map[f] for f in flist if f in files_map)
            out.append((tool, spec["query"], paths, label, spec.get("max_shown", 6)))
        elif tool == "read":
            fname = files_map.get(spec["section"], spec["section"])
            path = os.path.join(root, fname)
            out.append((tool, spec["section"], [path], fname,
                        (spec["start"], spec["num"])))
    return out


def _transcript(state: dict[str, Any]) -> dict[str, Any]:
    """Format de transcript du Playground (write_transcript) pour comparaison
    avec les synthèses via mkdigest."""
    return {
        "label": state["model"],
        "profile": ("Ask (lecture seule: list_directory, find_path, grep, read_file, "
                    "diagnostics) + exécution Python"),
        "persona": state["persona"],
        "session": state["session"],
        "title": state["title"],
        "module": state["module"],
        "focus": state["focus"],
        "model_path": state["model_path"],
        "reasoning_preserve": state["reasoning_preserve"],
        "no_system_embed": state.get("no_system_embed", False),
        "think_off": state["think_off"],
        "sampling": state.get("sampling") or {},
        "corpus": state["corpus_root"],
        "turns": state["turns"],
    }


class TutorEngine:
    """Moteur tuteur d'une session — portage de ``run_turn`` du harnais.

    ``run_turn_stream`` est **synchrone** (appel réseau bloquant) : le protocole
    ACP l'appelle via ``asyncio.to_thread`` puis achemine ses chunks vers le
    client — le blocage backend ne gèle pas la boucle asyncio du protocole.
    """

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    # -- backend -----------------------------------------------------------

    def complete_model_stream(self, messages: list[dict]):
        """Appel backend STREAMÉ avec le sampling du profil + outils (natifs ou
        aucun) — générateur de ``(dr, dc, fr, us, tool_calls)`` par chunk (cf.
        llm.stream_complete). ``model`` = l'alias de llama.cpp (cf. tutor/llm.py).

        Mode outils natifs (défaut) : ``tools=TOOLS_SPEC``, le modèle déclenche
        lui-même les lectures. Mode « Ask » (profil ``tools: "ask"``) : aucun
        ``tools`` — le modèle lit les blocs QUOTE injectés par l'engine (via le
        ``tool_plan`` de la session) et n'émet jamais de ``tool_calls``."""
        prof = config.profile(self.state["model"])
        tools = TOOLS_SPEC if config.uses_native_tools(self.state["model"]) else None
        yield from stream_complete(
            messages,
            prof.get("alias", self.state["model"]),
            config.model_base_url(self.state["model"]),
            self.state.get("max_tokens") or CALL_MAX_TOKENS,
            sampling=prof["sampling"],
            reasoning_format="none",
            tools=tools,
        )

    # -- mode Ask ----------------------------------------------------------

    def _ask_plan(self) -> tuple[list[str], list[dict]]:
        """Mode « Ask » : exécute le ``tool_plan`` de la session sur le corpus.

        Rend ``(blocs_QUOTE, trace)`` — les blocs QUOTE (format neutre
        ``format_quote``, résultats réels fichier:ligne ou « no match ») sont
        injectés dans le message étudiant, la trace alimente ``turn["tools"]``
        au format transcript (``grep`` / ``read_file``, cf. ``_expand_trace`` du
        harnais). Jamais d'appel ``role:"tool"`` : le modèle ne voit que du
        texte.
        """
        blocks: list[str] = []
        trace: list[dict] = []
        for tool, query, paths, label, extra in _expand_plan(
                self.state.get("tool_plan") or []):
            if tool == "grep":
                total, shown = grep_files(paths, query, max_shown=extra)
                trace.append({"tool": "grep", "query": query,
                              "files": label, "matches": total})
                blocks.append(format_quote("grep", query, label, shown))
            else:  # read
                start, num = extra
                shown = read_lines(paths[0], start, num)
                trace.append({"tool": "read_file", "section": label,
                              "lines": f"{start}-{start + num - 1}"})
                query = f"lines {start}-{start + num - 1}"
                blocks.append(format_quote("read", query, label, shown))
        return blocks, trace

    # -- tour --------------------------------------------------------------

    def run_turn_stream(self, student_msg: str, code: str | None = None,
                        no_tools: bool = False):
        """Tour complet STREAMÉ : générateur de ``(kind, text)`` (kind ∈
        {"reasoning", "content", "tool_start", "tool_progress"}) ; le dict du
        tour est porté par ``StopIteration.value`` (``return turn``).

        Le modèle déclenche lui-même les lectures du corpus (outils natifs,
        boucle outillée dans ``_stream_call``) ; le bloc PYTHON-RUN (code
        étudiant) est produit avant le premier ``yield`` : on ne streame que la
        génération du modèle (le gros du temps). Aucun ``await`` ici — le
        protocole l'itère dans un thread.
        """
        state = self.state
        messages = state["messages"]
        first_turn = not state["turns"]
        if state.get("no_system_embed") and first_turn:
            # ministral-3-8B-Reasoning : pas de système, les consignes vivent
            # dans le premier message étudiant (sinon le [THINK]…[/THINK] du
            # template ne sort pas).
            student_user = ("[Consignes pour toi, le tuteur]\n"
                            + state.get("system_text", "")
                            + "\n\n[Message de l'étudiant]\n" + student_msg)
        else:
            student_user = student_msg

        if config.STUB:
            return (yield from self._stub_stream(student_msg, student_user))

        # 1) exécution réelle du code étudiant.
        py_block = None
        if code:
            full = os.path.join(config.sessions_dir(), "student-code", state["id"], code)
            if os.path.exists(full):
                result = run_python(full)
                py_block = format_python(code, result)

        # 1bis) mode Ask (profil ``tools: "ask"``) : le harnais exécute le
        # tool_plan de la session et injecte les blocs QUOTE dans le prompt —
        # aucun ``tools`` au backend (complete_model_stream), le modèle lit des
        # blocs de texte (jamais de role:"tool").
        uses_ask = not config.uses_native_tools(state["model"])
        quote_blocks: list[str] = []
        ask_trace: list[dict] = []
        if uses_ask and not no_tools:
            quote_blocks, ask_trace = self._ask_plan()

        # 2) construction des messages selon le profil. En outils natifs le corpus
        #    est lu par le modèle lui-même (boucle outillée dans _stream_call) ;
        #    en mode Ask les QUOTE du plan sont injectés ici.
        if state.get("no_system_embed"):
            # ministral-3-8B-Reasoning : alternance stricte user/assistant
            # (raise_exception → 500 sur des user consécutifs) ; tout le contexte
            # du tour (consignes, QUOTE Ask, PYTHON-RUN) est fusionné en un seul
            # message user.
            parts = [student_user]
            parts += quote_blocks
            if py_block:
                parts.append(py_block)
            messages.append({"role": "user", "content": "\n\n".join(parts)})
        else:
            messages.append({"role": "user", "content": student_user})
            for blk in quote_blocks:
                messages.append({"role": "user", "content": blk})
            if py_block:
                messages.append({"role": "user", "content": py_block})

        # 5) appel STREAMÉ du modèle — sampling par profil, retry si contenu
        # visible vide (ministral-3-8B-Reasoning « pense sans répondre » :
        # [/THINK] non fermé).
        # On streame au fil de l'eau : le raisonnement s'affiche avant/pendant le
        # contenu, sans attendre la fin de génération.
        reasoning, content, _raw, finish, usage, tools_used = yield from self._stream_call(
            messages)
        for _attempt in range(RETRY_ATTEMPTS - 1):
            if content.strip() or finish != "stop":
                break
            reasoning, content, _raw, finish, usage, tools_used = yield from self._stream_call(
                messages)
        if not content.strip():
            # Récupération « close-and-continue » : le modèle a « pensé sans
            # répondre » à tous les essais. On ferme [/THINK] nous-mêmes, on
            # réinjecte le raisonnement, et on demande la réponse à l'étudiant
            # (mieux qu'un fallback générique). Les messages de récupération
            # sont retirés après coup : le tour sera ré-injecté au format natif
            # (étape 6) sans tags [THINK] persistants en double dans l'historique.
            orig_reasoning = reasoning.strip()
            if orig_reasoning:
                messages.append({"role": "assistant",
                                 "content": orig_reasoning + "\n[/THINK]"})
                messages.append({"role": "user", "content": RECOVERY_PROMPT})
                for _attempt in range(RECOVERY_ATTEMPTS):
                    reasoning, content, _raw, finish, usage, tools_used = yield from self._stream_call(
                        messages)
                    if content.strip():
                        break
                messages.pop()
                messages.pop()
                if content.strip():
                    reasoning = orig_reasoning
                    # les réponses récupérées commencent parfois par un
                    # séparateur markdown « --- » : jamais du contenu légitime en
                    # tête de réponse, on le retire.
                    content = content.lstrip()
                    if content.startswith("---"):
                        content = content[3:].lstrip()
            if not content.strip():
                # Fallback ultime : même la récupération a échoué. On garde le
                # raisonnement (affiché + ré-injecté) et on répond avec un
                # message de secours plutôt que de laisser l'étudiant sans
                # réponse.
                content = FALLBACK_RESPONSE
                reasoning = orig_reasoning if orig_reasoning else reasoning.strip()

        turn = {
            "turn": len(state["turns"]) + 1,
            "student": student_msg,
            "code": code,
            "tools": ([] if no_tools
                       else (ask_trace if uses_ask else tools_used)),
            "python": py_block,
            "reasoning": reasoning,
            "content": content,
            "finish": finish,
            "usage": usage,
        }
        state["turns"].append(turn)

        # 6) ré-injection multi-tours selon le profil.
        if state.get("no_system_embed"):
            # ministral-3-8B-Reasoning : ré-injection au format natif
            # [THINK]…[/THINK] (recommandé par la doc Mistral : rejouer la trace
            # complète). Effet de bord connu : le modèle « rejoue » souvent le
            # think de façon dégradée à T2+ (ouvre [THINK] sans le fermer) — la
            # réponse est alors récupérée par la procédure close-and-continue de
            # l'étape 5, et la trace reste enregistrée. On garde le format natif
            # pour que le modèle raisonne à chaque tour.
            messages.append({"role": "assistant",
                             "content": reinject_raw(reasoning, content)})
        elif reasoning:
            # --reasoning-preserve natif (géré par llama-server pour
            # qwen3.5-4B / ornith-1.5-9B).
            messages.append({"role": "assistant", "content": content,
                             "reasoning_content": reasoning})
        else:
            messages.append({"role": "assistant", "content": content})

        self._persist()
        return turn

    def _stream_call(self, messages: list[dict]):
        """Un tour backend STREAMÉ avec boucle outillée — générateur de
        ``(kind, text)`` (kind ∈ {"reasoning", "content", "tool_start",
        "tool_progress"}) ; la valeur de retour (StopIteration.value) est
        ``(reasoning, content, raw, finish, usage, tools_used)``.

        Le modèle peut demander des lectures du corpus (``tool_calls`` sur
        ``grep_files`` / ``read_lines``) : on les exécute (vrais résultats,
        lecture seule), on les lui renvoie en ``role:"tool"`` et on relance —
        jusqu'à ``MAX_TOOL_ROUNDS`` appels. Le raisonnement est **accumulé** sur
        tous les ré-appels (le modèle « pense » aussi après un outil).

        Le raisonnement natif (``reasoning_content``) arrive sur son propre
        canal ; il est aussi extrait du content chunk par chunk par un splitter
        (``BrutStreamSplitter`` [THINK]…[/THINK] pour ministral-3-8B-Reasoning,
        ``QwenStreamSplitter`` ``<thinking>\n…\n</thinking>`` pour
        qwen3.5-4B / ornith-1.5-9B — **créé neuf à chaque
        ré-appel** : un splitter réutilisé repartirait en ``in_think`` à tort sur
        le 2e appel). Appliqué à **tous** les profils : sans marqueurs le contenu
        passe intact, et quand un tour outillé fait fuir le raisonnement dans le
        visible (``--reasoning-preserve``), il est re-classé sans artefact.
        """
        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        raw_parts: list[str] = []
        tools_used: list[dict] = []
        finish = ""
        usage: dict = {}
        for _round in range(MAX_TOOL_ROUNDS):
            splitter = _opening_splitter(self.state["model"])
            round_tool_calls: list[dict] = []
            for dr, dc, fr, us, tool_calls in self.complete_model_stream(messages):
                if dr:
                    reasoning_parts.append(dr)
                    yield ("reasoning", dr)
                if dc:
                    raw_parts.append(dc)
                    for kind, text in splitter.feed(dc):
                        (reasoning_parts if kind == "reasoning"
                         else content_parts).append(text)
                        if text:
                            yield (kind, text)
                if fr:
                    finish = fr
                if us:
                    usage = us
                if tool_calls:
                    round_tool_calls = tool_calls
            for kind, text in splitter.finish():
                (reasoning_parts if kind == "reasoning"
                 else content_parts).append(text)
                if text:
                    yield (kind, text)
            if not round_tool_calls:
                break
            # Le modèle veut lire le corpus : on exécute réellement l'outil et on
            # lui renvoie le résultat (alternance assistant(tool_calls)/tool
            # strictement respectée — requise par ministral-3-8B-Reasoning, sans
            # danger ailleurs).
            for tc in round_tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == "read_lines":
                    title = "Lecture des lignes du corpus"
                    path = args.get("path")
                elif name == "grep_files":
                    title = "Recherche dans le corpus (grep)"
                    path = None
                else:
                    title = name
                    path = None
                trace, result = _exec_tool(name, args)
                yield ("tool_start", {"tool_call_id": tc.get("id"), "tool": name,
                                       "title": title, "path": path, "args": args})
                yield ("tool_progress", {"tool_call_id": tc.get("id"),
                                          "tool": name, "result": result})
                tools_used.append(trace)
                messages.append({"role": "assistant", "content": None,
                                 "tool_calls": [tc]})
                messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "content": result})
        return ("".join(reasoning_parts), "".join(content_parts),
                "".join(raw_parts), finish, usage, tools_used)

    def run_turn(self, student_msg: str, code: str | None = None,
                 no_tools: bool = False) -> dict[str, Any]:
        """Exécute un tour complet, NON streamé — drain de ``run_turn_stream``
        (conservé pour compatibilité)."""
        gen = self.run_turn_stream(student_msg, code, no_tools)
        turn = None
        while True:
            try:
                next(gen)
            except StopIteration as e:
                turn = e.value
                break
        return turn

    def _stub_stream(self, student_msg: str, student_user: str):
        """Tour STUB streamé et déterministe : pas d'appel backend, pas d'outils,
        pas d'écriture disque (tests unitaires). Streame d'abord le raisonnement,
        puis le contenu en trois blocs (pour exercer session/cancel)."""
        state = self.state
        state["messages"].append({"role": "user", "content": student_user})
        content = f"[STUB] {state['model']}: {student_user}"
        reasoning = f"[STUB reasoning] {state['model']}"
        state["messages"].append({"role": "assistant", "content": content})
        turn = {
            "turn": len(state["turns"]) + 1,
            "student": student_msg,
            "code": None,
            "tools": [],
            "python": None,
            "reasoning": reasoning,
            "content": content,
            "finish": "stop",
            "usage": {},
        }
        state["turns"].append(turn)
        yield ("reasoning", reasoning)
        step = max(1, len(content) // 3)
        for i in range(3):
            chunk = content[i * step: (i + 1) * step]
            if chunk:
                yield ("content", chunk)
                # Cadence STUB en temps réel (le drain tourne dans un thread) :
                # espace les 3 blocs pour que session/cancel puisse couper entre
                # deux blocs — reprend le STREAM_CHUNK_DELAY de l'ancien _run_turn.
                time.sleep(0.05)
        return turn

    # -- persistance -------------------------------------------------------

    def _persist(self) -> None:
        """Écrit l'état + le transcript (format Playground) pour la session."""
        state = self.state
        base = config.sessions_dir()
        os.makedirs(base, exist_ok=True)
        with open(base / f"{state['id']}.json", "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tdir = base / "transcripts"
        os.makedirs(tdir, exist_ok=True)
        with open(tdir / f"{state['id']}.json", "w", encoding="utf-8") as f:
            json.dump(_transcript(state), f, ensure_ascii=False, indent=2)
