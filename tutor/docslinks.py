"""Fallback de liens : conversion ``fichier:ligne`` → lien de page.

Le modèle produit des liens markdown natifs `[texte](url)` grâce au prompt
système (section « Références »). Ce module est un **filet de sécurité** :
si le modèle oublie et produit encore `fichier:ligne`, on le convertit en
lien de page cliquable.

Pas de réécriture streaming (plus nécessaire : le modèle connaît la base URL).
Pas de résolution d'ancre (le modèle ne les connaît pas).

Citations ``python:<ref>`` : réécrites en liens vers la doc Python (en ligne
par défaut, miroir local si configuré).
"""

from __future__ import annotations

import bisect
import json
import os
import re
from typing import Any

from . import config, docs

# Motif de citation : ``fichier:ligne`` éventuellement préfixé d'un chemin de
# dossier (``Applications/03_0_Asynchronous.qmd:63``), éventuellement entouré
# de backticks (`` `fichier:ligne` `` → retirés). Frontière gauche = pas un
# caractère de « nom de fichier » ni un ``/`` ; frontière droite = pas un
# chiffre (pour ne pas avaler ``1200`` en citant ``:120``).
_CITE_RE = re.compile(
    r"(?P<ouvr>`{0,2})"
    r"(?<![A-Za-z0-9_.\-/])"
    r"(?P<path>(?:[A-Za-z0-9_][A-Za-z0-9_.\-]*/)*)"
    r"(?P<fname>[A-Za-z0-9_][A-Za-z0-9_.\-]*\.qmd):(?P<line>\d+)(?![0-9])"
    r"(?P=ouvr)"
)

# Motif des fichiers du corpus cités **sans** ligne (label nu). Seuls les noms
# connus sont reconnus. Un préfixe de dossier est capturé pour le libellé.
_FILENAMES_RE: tuple[tuple[str, ...], re.Pattern | None] = ((), None)

# Citation de doc Python : ``python:<ref>`` où ``<ref>`` = module ou
# module#ancre. Wrapper de backticks facultatif (retiré à la réécriture).
_PY_REF_RE = re.compile(
    r"(?P<ouvr>`{0,2})"
    r"(?<![A-Za-z0-9_])python:([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*(?:#[A-Za-z0-9_.-]+)?)(?![A-Za-z0-9#_])(?P=ouvr)"
)

# Chargé paresseusement.
_SECTIONS: dict[str, Any] | None = None


def _load_sections() -> dict[str, Any]:
    """Carte nom de fichier → {html, lines:[…], sections:[{line, slug, title}]}."""
    path = config.sections_json()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for fname, entry in data.items():
        sections = entry.get("sections") or []
        out[fname] = {
            "html": entry.get("html", f"{fname}.html"),
            "lines": [int(s["line"]) for s in sections],
            "sections": sections,
        }
    return out


def sections_table() -> dict[str, Any]:
    global _SECTIONS
    if _SECTIONS is None:
        _SECTIONS = _load_sections()
    return _SECTIONS


def _basenames() -> tuple[str, ...]:
    """Noms de fichiers connus du corpus (basenames, du plus long au plus court)."""
    return tuple(sorted({os.path.basename(f) for f in sections_table()},
                        key=len, reverse=True))


def _filename_re() -> re.Pattern | None:
    """Motif des fichiers du corpus cités sans ligne (label nu).

    Wrapper de backticks facultatif (groupe ``ouvr``) : retiré à la réécriture
    pour que le lien se rende cliquable, pas en span de code.
    """
    global _FILENAMES_RE
    names = _basenames()
    if names == _FILENAMES_RE[0]:
        return _FILENAMES_RE[1]
    pattern = None
    if names:
        pattern = re.compile(
            r"(?P<ouvr>`{0,2})(?<![A-Za-z0-9_.\-/])"
            r"(?P<path>(?:[A-Za-z0-9_][A-Za-z0-9_.\-]*/)*)"
            r"(?P<fname>" + "|".join(re.escape(n) for n in names) + r")"
            r"(?![A-Za-z0-9_\-:])(?P=ouvr)"
        )
    _FILENAMES_RE = (names, pattern)
    return pattern


def reset_for_tests() -> None:
    """Vide les caches (carte et regex des noms — utile aux tests)."""
    global _SECTIONS, _FILENAMES_RE
    _SECTIONS = None
    _FILENAMES_RE = ((), None)


def section_url_for(fname: str, line: int, base_url: str | None = None) -> str | None:
    """URL absolue ``BASE/chemin.html`` (sans ancre) pour ``fname:line``.

    ``fname`` peut être un chemin — on ne garde que le basename. Fichier inconnu
    → ``None`` (pas de lien). Pas de résolution d'ancre : le modèle ne la connaît
    pas, un lien de page suffit.
    """
    if base_url is None:
        base_url = docs.effective_base_url()
    if "/" in fname:
        fname = os.path.basename(fname)
    entry = sections_table().get(fname)
    if entry is None:
        return None
    return f"{base_url.rstrip('/')}/{entry['html']}"


def _python_base_url() -> str:
    """Base des citations ``python:<ref>``."""
    if config.py_dir():
        return f"{docs.effective_base_url().rstrip('/')}/py"
    return config.python_doc_base_url()


def _python_url(ref: str) -> str:
    """URL de la doc Python pour une citation ``python:<ref>``."""
    mod, _, anchor = ref.partition("#")
    url = f"{_python_base_url().rstrip('/')}/library/{mod}.html"
    return f"{url}#{anchor}" if anchor else url


def _rewrite(text: str, base_url: str) -> str:
    def _repl(m: re.Match) -> str:
        # groupe ouvrant/fermant de backticks (groupe 1) : retiré pour que le
        # lien se rende cliquable, pas en span de code.
        fname = m.group("path") + m.group("fname")
        url = section_url_for(fname, int(m.group("line")), base_url)
        return f"[{fname}:{m.group('line')}]({url})" if url else m.group(0)

    text = _CITE_RE.sub(_repl, text)

    # label nu (fichier connu sans ligne) → lien de page
    fname_re = _filename_re()
    if fname_re:
        def _fname_repl(m: re.Match) -> str:
            fname = m.group("path") + m.group("fname")
            url = section_url_for(fname, 0, base_url)
            return f"[{fname}]({url})" if url else m.group(0)
        text = fname_re.sub(_fname_repl, text)

    def _pyrepl(m: re.Match) -> str:
        return f"[python:{m.group(2)}]({_python_url(m.group(2))})"

    return _PY_REF_RE.sub(_pyrepl, text)


def rewrite_content(text: str, base_url: str | None = None) -> str:
    """Convertit les mentions ``fichier:ligne`` connues en liens de page
    (fallback) et ``python:<ref>`` en liens vers la doc Python."""
    if base_url is None:
        base_url = docs.effective_base_url()
    if not base_url or not text:
        return text
    return _rewrite(text, base_url)
