"""Outils du harnais « Ask » — portage fidèle de ``harness.py`` (L14-154).

Lecture seule sur le corpus (``grep_files``, ``read_lines``, ``list_directory``)
avec troncature (MAX_SHOWN / MAX_LINE_CHARS), plus l'exécution réelle du code
de l'étudiant (``run_python``) et le formatage des blocs neutres QUOTE /
PYTHON-RUN. Jamais de syntaxe d'outil : les petits modèles la reflètent.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

MAX_SHOWN = 8          # nombre max de lignes montrées par grep
MAX_LINE_CHARS = 180   # troncature par ligne
PYTHON_TIMEOUT = 15    # secondes avant kill d'un script étudiant


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
