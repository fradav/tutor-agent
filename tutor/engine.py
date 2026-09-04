"""Moteur tuteur socratique — portage de ``run_turn`` du harnais « Ask ».

Chaque tour :
1. reçoit le message étudiant (éventuellement embarqué avec les consignes tuteur
   pour les profils en mode brut, cf. ``config.embeds_instructions``) ;
2. passe les outils natifs (``grep_files`` / ``read_lines``, spec OpenAI ``tools``)
   au backend : c'est **le modèle** qui demande lui-même les lectures du corpus
   (mode outils standard, Option B) — l'engine exécute le vrai outil (lecture
   seule, résultats réels fichier:ligne ou « no match ») et lui renvoie le
   résultat en ``role:"tool"`` ;
3. exécute éventuellement le code étudiant → bloc PYTHON-RUN ;
4. construit les messages selon le profil (system + un user pour qwen3.5-4B /
   ornith-1.5-9B ; tout fusionné en un seul user pour un profil en mode brut) ;
5. appelle le backend en **streaming** (``stream_complete``), boucle outillée
   (jusqu'à ``MAX_TOOL_ROUNDS`` appels si tool_calls), **retry si contenu vide** ;
6. réinjecte le raisonnement multi-tours selon le profil (BRUT pour un profil en
   mode brut / reasoning_content pour qwen3.5-4B et ornith-1.5-9B) ;
7. persiste état + transcript (même format que ``transcripts/*.json`` du
   Playground) et retourne le dict du tour.

``run_turn_stream`` est le cœur : un **générateur** de ``(kind, text)`` (``kind``
∈ {"reasoning", "content"}) qui streame le raisonnement et le contenu au fil de
l'eau ; le dict du tour est porté par ``StopIteration.value``. Le raisonnement
est séparé du contenu chunk par chunk par un splitter
(``BrutStreamSplitter`` [THINK]…[/THINK] pour un profil en mode brut,
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
from pathlib import Path
from typing import Any

from . import config
from .docslinks import rewrite_content
from .llm import stream_complete
from .tools import (
    find_paths,
    find_py_files,
    format_python,
    format_quote,
    grep_files,
    list_directory,
    py_syntax_errors,
    read_lines,
    run_python,
)

# Nombre raisonnable de tokens pour le body /v1/chat/completions : rester sur
# ~1/8 du contexte (la config stocke le contexte -c 32768 ; le passer en entier
# au body gaspillerait énormément / risquerait le timeout sur les 3–9B locaux).
CALL_MAX_TOKENS = max(1024, config.max_tokens() // 8)

# Retry « contenu visible vide » (un profil en mode brut « pense sans
# répondre » : think émis mais [/THINK] non fermé → le splitter classe tout en
# raisonnement). Le taux de fermeture est probabiliste (~40-60 % par essai,
# instable dans le temps) ; le retry multiple fiabilise, puis la recovery
# close-and-continue (étape 5) récupère la réponse avant le fallback ultime.
RETRY_ATTEMPTS = 3

FALLBACK_RESPONSE = ("Je réfléchis à ta question — peux-tu la reformuler ou "
                     "préciser ce que tu cherches à faire ?")

# Récupération « close-and-continue » (profil en mode brut). Quand le
# modèle a « pensé sans répondre » ([THINK] ouvert jamais fermé → réponse
# englobée, invisible pour le splitter), on ferme le tag nous-mêmes, on
# réinjecte le raisonnement, et on demande la réponse à l'étudiant. Testé 8/8
# sur T2 (probe_close) ; le format neutre (sans tags) ne marche que 4/8 — le
# `[/THINK]` littéral est le signal fort qui débloque le modèle.
RECOVERY_ATTEMPTS = 2
RECOVERY_PROMPT = ("Écris maintenant ta réponse à l'étudiant, en français, "
                   "directement après le [/THINK], sans séparateur ni "
                   "introduction. Ne mets pas ta réponse dans le raisonnement.")

# Spec of the tutor's native tools (standard-tool mode, Option B):
# grep_files / read_lines / list_directory / find_path / diagnostics — read-only
# over the course material AND the open project (session cwd). *The model*
# triggers the calls (tool_choice="auto" in llm.py); the engine executes the
# real tool (real file:line results or "no match") and returns the result in
# role:"tool". find_path/diagnostics are local re-implementations of the Zed
# Ask bios (glob + Python syntax check — LSP state is host-owned, not exposed
# over ACP, so diagnostics is a static subset, see README §7).
# Course files (key=name) are recalled in the descriptions so the model passes a
# key ("01") or a file name instead of a made-up path that resolves to nothing.
_CORPUS_HINT = ", ".join(
    f"{k}={f}" for k, f in sorted(config.corpus_files().items())
)

TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "grep_files",
            "description": (
                "Search a regex pattern in the course material or the open "
                "project files. Course files (key=name): " + _CORPUS_HINT + ". "
                "Pass a key (e.g. \"01\"), a file name, or a project-relative "
                "path in `paths`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex to search"},
                    "paths": {"type": "array", "items": {"type": "string"},
                              "description": "Course files (keys or names) or project-relative paths, e.g. [\"01\"] or [\"src/main.py\"]"},
                    "max_shown": {"type": "integer", "description": "Max lines shown", "default": 8},
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
                "Read a line range of a course file or an open-project file. "
                "Course files (key=name): " + _CORPUS_HINT + ". "
                "Pass a key (e.g. \"01\"), a file name, or a project-relative "
                "path in `path`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                              "description": "Course key (e.g. \"01\"), a file name, or a project-relative path"},
                    "start": {"type": "integer", "description": "First line (>= 1)"},
                    "num": {"type": "integer", "description": "Number of lines (>= 1)"},
                },
                "required": ["path", "start", "num"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "List a directory of the open project (or the course root). "
                "Pass a project-relative path (e.g. \".\" or \"src\")."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                              "description": "Project-relative directory path (e.g. \".\" or \"src\")"},
                    "max_entries": {"type": "integer", "description": "Max entries shown", "default": 15},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_path",
            "description": (
                "Find file paths matching a glob pattern in the course material "
                "or the open project, e.g. \"**/*.py\", \"src/*.py\", \"*.qmd\", "
                "or a course key (\"01\"). The returned paths are reusable: "
                "pass the same names to grep_files or read_lines."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "glob": {"type": "string",
                              "description": "Glob pattern (project-relative; a course file name, key, or substring also works)"},
                    "offset": {"type": "integer", "description": "Skip the first N matches (pagination, default 0)", "default": 0},
                },
                "required": ["glob"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diagnostics",
            "description": (
                "Report Python syntax errors in the open project (no `path`) "
                "or in a single file (`path`). Static local check (ast.parse) "
                "— errors only, lines like rel:line:col: msg; no linters, no "
                "type checking, and the course .qmd files are never checked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                              "description": "Optional: project-relative path of one Python file to check"},
                },
                "required": [],
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
    """Ré-injection d'un profil en mode brut au format natif "[THINK]…[/THINK]" + réponse.

    En mode brut, le modèle voit ses propres tags dans l'historique et continue
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
    system_text = config.build_system(model, cwd=cwd)
    return {
        "id": session_id,
        "model": model,
        "alias": prof.get("alias", model),
        "model_path": config.model_path(model),
        "cwd": cwd,
        "persona": persona,
        "session": label,
        "title": title if title else "Session tuteur socratique (conversation libre)",
        "module": module if module else "corpus MIASHS — programmation avancée (book 2025 : 12 chapitres + annexe B, 14 TP)",
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


def _resolve_tool_paths(path: str,
                       project_dir: str | None = None) -> list[str]:
    """Resolves a model-requested path to absolute paths under a read-only root.

    Two roots, in order:
    1. the course material — tolerance order: short key ("01") → .qmd file;
       exact file name; glob `*`/`?` → all corpus files; substring (requested
       name contained in a corpus name or the reverse);
    2. the open project (``project_dir`` = session cwd): project-relative
       paths, no absolute, no `..` (the agent must never escape the opened
       workspace).

    Unresolved → ``[]``: ``_exec_tool`` then emits an explicit "unknown path"
    message instead of a silent 0-match the model would re-request in a loop.
    """
    files = config.corpus_files()
    root = config.corpus_root()
    if path in files:
        return [os.path.join(root, files[path])]
    if "*" in path or "?" in path:
        matched = [
            os.path.join(root, f)
            for f in files.values()
            if Path(f).match(path)
        ]
        if matched:
            return matched
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
    if hits:
        return hits
    if project_dir:
        return _resolve_project_paths(path, project_dir)
    return []


def _resolve_project_paths(path: str, project_dir: str) -> list[str]:
    """Resolves a **project-relative** path under ``project_dir`` (read-only).

    Refuses absolute paths and ``..`` climb (the ACP must not expose arbitrary
    machine paths); `*`/`?` globs resolve via ``Path.glob``. Returns existing
    entries (file **or** directory) anchored under the resolved project root.
    """
    if os.path.isabs(path) or ".." in path.replace("\\", "/").split("/"):
        return []
    root = Path(project_dir).resolve()
    pattern = Path(path)
    try:
        candidates = (root.glob(str(pattern))
                      if ("*" in str(pattern) or "?" in str(pattern))
                      else [root / pattern])
    except (ValueError, OSError):
        return []
    out: list[str] = []
    for cand in candidates:
        try:
            resolved = cand.resolve()
            resolved.relative_to(root)
        except (ValueError, OSError):
            continue
        if resolved.exists():
            out.append(str(resolved))
    return out


def _exec_tool(name: str, args: dict,
               project_dir: str | None = None) -> tuple[dict, str]:
    """Executes a model tool_call: real read-only access to the course material
    and/or the open project.

    Returns ``(trace, result)`` — trace in transcript §3 format (``grep`` /
    ``read_file``, cf. the old ``_expand_trace``) for ``turn["tools"]``, and
    ``result`` text returned to the model in role:"tool". Invalid JSON args →
    error result without ever crashing the tool loop.
    """
    if name == "grep_files":
        requested = args.get("paths") or []
        paths: list[str] = []
        unknown: list[str] = []
        for p in requested:
            resolved = _resolve_tool_paths(p, project_dir)
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
                "grep_files: 0 matches — unknown path(s): " + ", ".join(unknown)
                + f". Valid corpus keys: {_CORPUS_HINT}"
            )
        else:
            result = (f"grep_files: {total} matches (files: {files})\n"
                      + "\n".join(shown))
        return trace, result
    if name == "read_lines":
        requested = args.get("path", "")
        resolved = _resolve_tool_paths(requested, project_dir)
        if not resolved:
            return ({"tool": "read_file", "section": requested, "lines": "−"},
                    "read_lines: unknown path: " + _CORPUS_HINT)
        start = _clamp_int(args.get("start"), 1)
        num = _clamp_int(args.get("num"), 1)
        shown = read_lines(resolved[0], start, num)
        trace = {"tool": "read_file", "section": requested,
                 "lines": f"{start}-{start + num - 1}"}
        return trace, "read_lines\n" + "\n".join(shown)
    if name == "list_directory":
        requested = args.get("path") or ""
        if project_dir and requested in ("", ".", "./"):
            target = [str(Path(project_dir).resolve())]
        else:
            target = [p for p in _resolve_tool_paths(requested, project_dir)
                      if os.path.isdir(p)]
        if not target:
            hint = (f". Valid corpus keys: {_CORPUS_HINT}"
                    if not project_dir else "")
            return ({"tool": "list_directory", "path": requested or "."},
                    "list_directory: unknown path: " + (requested or ".") + hint)
        entries = list_directory(target[0],
                                 max_entries=args.get("max_entries", 15))
        trace = {"tool": "list_directory", "path": requested or "."}
        return trace, "list_directory\n" + "\n".join(entries)
    if name == "find_path":
        pattern = args.get("glob") or ""
        offset = _clamp_int(args.get("offset"), 0)
        got, total, more = find_paths(
            pattern,
            project_dir=project_dir,
            corpus=config.corpus_files(),
            offset=offset,
        )
        trace = {"tool": "find_path", "pattern": pattern or "*",
                 "total": total, "shown": len(got)}
        label = pattern or "*"
        if total == 0:
            return (trace,
                    f"find_path: 0 matches for {label} — try \"*.qmd\", "
                    "\"**/*.py\", a course key (e.g. \"01\") or a substring")
        head = f"find_path: {total} match(es) for {label}"
        if more:
            head += f" (showing {offset + 1}-{offset + len(got)})"
        lines = [head] + got
        if more:
            lines.append(f"… more matches available (use offset={offset + len(got)})")
        return trace, "\n".join(lines)
    if name == "diagnostics":
        requested = (args.get("path") or "").strip()
        if requested:
            resolved = _resolve_tool_paths(requested, project_dir)
            if not resolved:
                return ({"tool": "diagnostics", "path": requested, "errors": 0},
                        f"diagnostics: unknown path: {requested}")
            target = resolved[0]
            if not target.lower().endswith(".py"):
                return ({"tool": "diagnostics", "path": requested, "errors": 0},
                        f"diagnostics: {requested} is not a Python file "
                        "(only .py files are checked — course .qmd are excluded)")
            errs = py_syntax_errors(target)
            trace = {"tool": "diagnostics", "path": requested, "errors": len(errs)}
            return (trace,
                    f"diagnostics: {len(errs)} error(s) in {requested}\n"
                    + "\n".join(errs))
        if not project_dir:
            return ({"tool": "diagnostics", "errors": 0},
                    "diagnostics: no open project (session cwd) — nothing to check")
        files = find_py_files(project_dir)
        if not files:
            return ({"tool": "diagnostics", "files": 0, "errors": 0},
                    "diagnostics: 0 Python file(s) in the project — all good")
        got_errs: dict[str, list[str]] = {}
        proot = str(Path(project_dir).resolve())
        for f in files:
            errs = py_syntax_errors(f)
            if errs:
                got_errs[os.path.relpath(f, proot)] = errs
        total_errors = sum(len(v) for v in got_errs.values())
        trace = {"tool": "diagnostics", "files": len(files),
                 "files_with_errors": len(got_errs), "errors": total_errors}
        head = (f"diagnostics: {len(files)} Python file(s) checked, "
                f"{len(got_errs)} with error(s), {total_errors} error(s) total, "
                "0 warning(s) — syntax check only (static subset)")
        if not got_errs:
            return trace, head + " — all good"
        lines = [head]
        for rel, errs in sorted(got_errs.items()):
            lines.append(f"{rel} ({len(errs)} error(s))")
            lines += ["  " + e for e in errs[:3]]
            if len(errs) > 3:
                lines.append(f"  … +{len(errs) - 3} more")
        return trace, "\n".join(lines)
    return {"tool": "other", "name": name}, f"unknown tool: {name}"


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
        # Endpoint/clef de secours quand `server.ensure` a basculé le modèle sur
        # le fallback distant (routeur localhost injoignable).
        api_key = (config.fallback_api_key()
                   if config.is_fallback_active(self.state["model"]) else None)
        yield from stream_complete(
            messages,
            prof.get("alias", self.state["model"]),
            config.model_base_url(self.state["model"]),
            self.state.get("max_tokens") or CALL_MAX_TOKENS,
            sampling=prof["sampling"],
            reasoning_format="none",
            tools=tools,
            api_key=api_key,
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
            # Profil en mode brut : pas de système, les consignes vivent
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
            # Profil en mode brut : alternance stricte user/assistant (sinon
            # user consécutifs → HTTP 500) ; tout le contexte du tour
            # (consignes, QUOTE Ask, PYTHON-RUN) est fusionné en un seul
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
        # visible vide (profil en mode brut « pense sans répondre » :
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
            # Profil en mode brut : ré-injection au format natif [THINK]…[/THINK]
            # (rejouer la trace complète). Effet de bord connu : le modèle
            # « rejoue » souvent le think de façon dégradée à T2+ (ouvre [THINK]
            # sans le fermer) — la réponse est alors récupérée par la procédure
            # close-and-continue de l'étape 5, et la trace reste enregistrée. On
            # garde le format natif pour que le modèle raisonne à chaque tour.
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
        (``BrutStreamSplitter`` [THINK]…[/THINK] pour un profil en mode brut,
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
        # Le modèle produit des liens markdown natifs (`[texte](url)`) grâce à
        # la section « Références » injectée dans le prompt système par
        # ``config.build_system()``. On passe le contenu tel quel — pas de
        # réécriture post-hoc (qui causait des liens imbriqués dans l'historique).
        # Fallback optionnel : si le modèle produit encore ``fichier:ligne``, le
        # ``rewrite_content()`` sur les résultats d'outils (ci-dessous) le
        # convertit en lien de page.
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
                        if kind == "reasoning":
                            reasoning_parts.append(text)
                            if text:
                                yield (kind, text)
                        else:
                            content_parts.append(text)
                            if text:
                                yield (kind, text)
                if fr:
                    finish = fr
                if us:
                    usage = us
                if tool_calls:
                    round_tool_calls = tool_calls
            for kind, text in splitter.finish():
                if kind == "reasoning":
                    reasoning_parts.append(text)
                    if text:
                        yield (kind, text)
                else:
                    content_parts.append(text)
                    if text:
                        yield (kind, text)
            if not round_tool_calls:
                break
            # Le modèle veut lire le corpus : on exécute réellement l'outil et on
            # lui renvoie le résultat (alternance assistant(tool_calls)/tool
            # strictement respectée — requise par un profil en mode brut, sans
            # danger ailleurs).
            for tc in round_tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == "read_lines":
                    title = "Read lines from the material"
                    path = args.get("path")
                elif name == "grep_files":
                    title = "Search the material (grep)"
                    path = None
                elif name == "list_directory":
                    title = "List directory"
                    path = args.get("path")
                elif name == "find_path":
                    title = "Find files by name pattern"
                    path = args.get("glob")
                elif name == "diagnostics":
                    title = "Check Python files for syntax errors"
                    path = args.get("path")
                else:
                    title = name
                    path = None
                trace, result = _exec_tool(name, args,
                                           self.state.get("cwd") or None)
                yield ("tool_start", {"tool_call_id": tc.get("id"), "tool": name,
                                       "title": title, "path": path, "args": args})
                # Affichage : le résultat de lecture (lignes ``fichier:ligne``)
                # est réécrit en liens cliquables pour l'étudiant ; le message
                # role:"tool" apposé aux messages reste, lui, brut (le modèle doit
                # voir les lignes telles quelles).
                display = rewrite_content(result) if result else result
                yield ("tool_progress", {"tool_call_id": tc.get("id"),
                                          "tool": name, "result": display})
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
