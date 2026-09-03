"""Outils du harnais « Ask » — portage fidèle de ``harness.py`` (L14-154).

Lecture seule sur le corpus (``grep_files``, ``read_lines``, ``list_directory``)
avec troncature (MAX_SHOWN / MAX_LINE_CHARS), plus ``find_path`` (glob sur le
corpus et le projet ouvert) et ``diagnostics`` (contrôle de syntaxe Python
local via ``ast.parse`` — sous-ensemble statique du vrai outil Zed, qui
s'appuie sur les LSP du hôte, indisponibles dans un process ACP externe), plus
l'exécution réelle du code de l'étudiant (``run_python``) et le formatage des
blocs neutres QUOTE / PYTHON-RUN. Jamais de syntaxe d'outil : les petits
modèles la reflètent.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

MAX_SHOWN = 8          # nombre max de lignes montrées par grep
MAX_LINE_CHARS = 180   # troncature par ligne
PYTHON_TIMEOUT = 15    # secondes avant kill d'un script étudiant

# Dossiers jamais explorés (index de dépendances, caches) : trop de bruit dans
# find_path / diagnostics, et un coût inutile (walk récursif).
_NOISE_DIRS = {".git", ".venv", "venv", "env", "__pycache__",
               ".mypy_cache", ".pytest_cache", ".ruff_cache",
               "node_modules", ".direnv", ".cache"}

_FIND_PAGE = 50           # page par défaut de find_path (parité avec Zed Ask)
_FIND_CAP = 500           # borne globale de collecte de find_path (jamais de
                          # walk infini : un `**/*` sur un gros projet s'arrête là)
DIAG_FILES_CAP = 100      # maximum de fichiers .py vérifiés par diagnostics


def _truncate(text: str) -> str:
    if len(text) > MAX_LINE_CHARS:
        return text[: MAX_LINE_CHARS] + "…"
    return text


def _read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [line.rstrip("\n") for line in f]


def grep_files(paths: list[str], pattern: str, max_shown: int = MAX_SHOWN):
    """Grep sur plusieurs fichiers, sorties préfixées `fichier:ligne: texte`.

    Retourne (total_matches, shown_lines).
    """
    rx = re.compile(pattern, re.IGNORECASE)
    matches = []
    for path in paths:
        try:
            lines = _read_lines(path)
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                matches.append(f"{os.path.basename(path)}:{i}: {_truncate(line)}")
    return len(matches), matches[:max_shown]


def read_lines(path: str, start: int, num: int, max_shown: int = MAX_SHOWN + 8):
    """Lecture d'une plage de lignes, préfixées `fichier:ligne: texte`.

    Fichier absent → liste vide (l'engine émet alors le QUOTE « no match »
    neutre), comme `grep_files`. Jamais de traceback sur corpus incomplet.
    """
    try:
        lines = _read_lines(path)
    except OSError:
        return []
    shown = []
    for offset in range(num):
        lineno = start + offset
        if lineno > len(lines):
            break
        shown.append(f"{os.path.basename(path)}:{lineno}: {_truncate(lines[lineno - 1])}")
    return shown[:max_shown]


def list_directory(path: str, max_entries: int = 15) -> list[str]:
    try:
        names = sorted(os.listdir(path))
    except OSError as e:
        return [f"error: {e}"]
    entries = []
    for name in names:
        sub = os.path.join(path, name)
        if os.path.isdir(sub):
            entries.append(f"{name}/")
        else:
            entries.append(f"{name}  [{os.path.splitext(name)[1] or 'file'}]")
    if len(entries) > max_entries:
        entries = entries[:max_entries] + [f"… et {len(entries) - max_entries} entrées"]
    return entries


def _is_abs_or_climb(path: str) -> bool:
    """Vrai pour un chemin absolu ou une montée ``..`` (jamais d'accès hors des
    racines exposées — même garde-fou que ``_resolve_project_paths``)."""
    return os.path.isabs(path) or ".." in path.replace("\\", "/").split("/")


def _iter_glob(root: Path, pattern: str):
    """Chemins **relatifs** (pathlib) correspondant à ``pattern`` sous ``root``.

    Non récursif sauf si le pattern contient ``**`` ; en mode récursif les
    dossiers de bruit (``_NOISE_DIRS``) sont sautés à la descente (aucun index
    de dépendances dans les résultats, walk borné). Ne rend que des **fichiers**
    (les répertoires sont du ressort de ``list_directory``).
    """
    if "**" in pattern:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS]
            base = Path(dirpath)
            for name in filenames:
                rel = base.relative_to(root) / name
                if rel.match(pattern):
                    yield rel
    else:
        for cand in root.glob(pattern):
            try:
                rel = cand.relative_to(root)
            except ValueError:
                continue
            if cand.is_file():
                yield rel


def find_paths(pattern: str,
               project_dir: str | None = None,
               corpus: dict[str, str] | None = None,
               max_results: int = _FIND_PAGE,
               offset: int = 0) -> tuple[list[str], int, bool]:
    """Glob (lecture seule) sur le corpus ET le projet ouvert.

    Retourne ``(résultats paginés, total, encore)``. Les chemins rendus sont
    **réutilisables tels quels** par ``grep_files``/``read_lines`` (noms de
    fichiers du corpus, e.g. ``01_asynchronous.qmd``, ou chemins relatifs au
    projet, e.g. ``src/main.py``) — c'est ce que le modèle re-demande ensuite.

    Résolution, dans l'ordre : clé courte du corpus (``"01"``) → glob sur les
    noms de fichiers du corpus (``*.qmd``, ``0?``) → sous-chaîne ; puis glob
    sur le projet (``**/*.py`` récursif, bruit filtré). Patterns absolus et
    montées ``..`` refusés (``[]``).
    """
    pattern = (pattern or "*").strip()
    if _is_abs_or_climb(pattern):
        return [], 0, False
    files = corpus or {}
    ordered: list[str] = []
    seen: set[str] = set()

    def _push(m: str) -> None:
        if m not in seen:
            seen.add(m)
            ordered.append(m)

    # 1) corpus : clé courte, glob sur les noms, puis sous-chaîne.
    if "*" in pattern or "?" in pattern:
        corpus_hits = sorted(
            f for f in files.values() if Path(f).match(pattern))
    elif pattern in files:
        corpus_hits = [files[pattern]]
    else:
        needle = os.path.basename(pattern).lower().removesuffix(".qmd")
        corpus_hits = [
            f for f in files.values()
            if needle and (needle in f.lower() or f.lower() in needle)]
    for m in corpus_hits:
        _push(m)

    # 2) projet ouvert : glob relatif (récursif si **), bruit filtré.
    if project_dir and not _is_abs_or_climb(pattern):
        root = Path(project_dir).resolve()
        for rel in sorted(
                _iter_glob(root, pattern),
                key=lambda r: os.fspath(r).replace(os.sep, "/").lower()):
            _push(os.fspath(rel).replace(os.sep, "/"))

    total = len(ordered)
    if total > _FIND_CAP:
        ordered = ordered[:_FIND_CAP]
        total = _FIND_CAP
    got = ordered[offset:offset + max_results]
    more = offset + max_results < total
    return got, total, more


def py_syntax_errors(path: str) -> list[str]:
    """Erreurs de syntaxe Python d'un fichier (``ast.parse``, stdlib).

    Retourne ``fichier:ligne:col: message`` — jamais de traceback, lecture seule
    (aucun fichier écrit, contrairement à ``py_compile``). Contrôle **statique**
    seulement : le vrai outil ``diagnostics`` de Zed agrège les LSP du hôte
    (types, linters), indisponibles dans ce process ACP externe — on ne rend que
    les erreurs de syntaxe, et 0 warning.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return []
    if b"\x00" in raw:
        # ast.parse lève selon la version (SyntaxError "None" sur 3.13, ValueError
        # avant) — on détecte les octets nuls explicitement pour un message stable.
        return [f"{os.path.basename(path)}:1:1: file contains NUL bytes"]
    try:
        src = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{os.path.basename(path)}:1:1: cannot decode file as UTF-8"]
    try:
        ast.parse(src, filename=path)
    except SyntaxError as e:
        return [f"{os.path.basename(path)}:{e.lineno}:{e.offset or 1}: {e.msg}"]
    return []


def find_py_files(project_dir: str) -> list[str]:
    """Fichiers ``.py`` du projet ouvert, ordre stable, bruit filtré, borné à
    ``DIAG_FILES_CAP`` (jamais un walk déraisonnable sur ``.venv`` etc.)."""
    root = Path(project_dir).resolve()
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS]
        for name in sorted(filenames):
            if name.endswith(".py"):
                out.append(os.path.join(dirpath, name))
                if len(out) >= DIAG_FILES_CAP:
                    return out
    return out


def format_quote(tool: str, query: str, files_label: str, lines: list[str]) -> str:
    """Bloc QUOTE neutre (jamais de crochets de type [tool result])."""
    if not lines:
        return (f"QUOTE — {tool} {query}: no match found in the material. "
                f"({files_label})")
    return f"QUOTE — {tool} {query} ({files_label})\n" + "\n".join(lines)


def format_python(rel_name: str, result) -> str:
    """Bloc PYTHON-RUN : vraie sortie/erreur d'exécution du code étudiant."""
    code = result.get("exit")
    label = code if code is not None else "timeout"
    head = f"PYTHON-RUN — python3 {rel_name} (exit={label})"
    out = (result.get("stdout") or "").rstrip("\n")
    err = (result.get("stderr") or "").rstrip("\n")
    lines = []
    if out:
        lines.append("standard output:")
        o = out.splitlines()
        lines.extend(o[:14])
        if len(o) > 14:
            lines.append(f"… (sortie tronquée — {len(o)} lignes au total)")
    if err:
        lines.append("standard error:")
        e = err.splitlines()
        lines.extend(e[-14:])
        if len(e) > 14:
            lines.append(f"… (erreur tronquée — {len(e)} lignes au total)")
    if not lines:
        lines = ["(aucune sortie)"]
    return head + "\n" + "\n".join(lines)


def run_python(full_path: str, timeout: int = PYTHON_TIMEOUT, cwd: str | None = None):
    """Exécute réellement le code de l'étudiant. Tue le groupe de processus si
    le script dépasse `timeout` (deadlock, boucle infinie, busy-wait).

    On utilise Popen directement : l'exception TimeoutExpired de Python 3.14
    n'expose plus .pid, il faut donc tracker le pid via l'objet Popen pour
    pouvoir tuer le groupe de processus orphelin après un timeout."""
    if not cwd:
        cwd = os.path.dirname(full_path)
    proc = subprocess.Popen(
        [sys.executable, full_path],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return {
            "exit": proc.returncode,
            "stdout": out or "",
            "stderr": err or "",
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        out, err = proc.communicate()
        return {
            "exit": None,
            "stdout": out or "",
            "stderr": (err or "") + f"\n[TIMEOUT après {timeout}s — programme encore actif, tué]",
            "timed_out": True,
        }
