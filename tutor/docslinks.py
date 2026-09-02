"""Liens cliquables vers le book public : réécriture ``fichier:ligne`` → lien markdown.

Le tuteur (4 modèles, harnais ACP) cite le corpus avec ``nom.qmd:ligne`` (même
affichage, en gras, dans ``tools.format_quote``). Cette carte transforme ces
mentions en liens cliquables vers le book rendu, servi en local par
``tutor.docs`` (``run.py serve-docs``) : ``[nom.qmd:ligne](BASE/chemin.html#ancre)``.

La réécriture est **déterministe** et appliquée côté moteur (engine) sur le
contenu visible du tour (``kind == "content"``) — les modèles n'ont pas à
changer d'output : ça marche en outils natifs **et** en mode Ask. Le
raisonnement n'est jamais réécrit.

Carte ligne → section lue depuis ``corpus/sections.json`` (générée par
``tools/build_docs_map.py`` à partir des ``.qmd`` sources et des ids du HTML
rendu) : pour chaque fichier, la liste ordonnée des sections (ligne, slug,
titre). Le slug est l'id réel du HTML rendu (quarto : minuscules, espaces→``-``,
``.`` conservés, doublons ``-1``/``-2`` gérés).

Sans ``sections.json`` (livrable sans book) ou sans base URL, la réécriture
est neutre : le texte passe tel quel.
"""

from __future__ import annotations

import bisect
import json
import os
import re
from typing import Any

from . import config

# Un motif de citation : `nom.qmd:ligne` (l'extension ``.qmd`` fait partie du
# groupe = clé de la carte). Frontière gauche = pas un caractère de « nom de
# fichier » (sinon on réécrirait au milieu d'un identifiant) ; frontière droite
# = pas un chiffre (pour ne pas avaler `1200` en citant :120).
_CITE_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])([A-Za-z0-9_][A-Za-z0-9_.\-]*\.qmd):(\d+)(?![0-9])"
)

# Suffixe de fin de chunk qui peut être un début de citation coupé en deux :
# on retient tout suffixe susceptible de devenir `*.qmd`, `*.qmd:`, `*.qmd:1`…
# pour ne pas casser un lien dont la suite arrive dans le chunk suivant.
_TAIL_RE = re.compile(r"[A-Za-z0-9_.\-]*\.qmd:?\d*$")

# Borne haute d'un suffixe retenu : au-delà, ce n'est pas un nom de fichier
# crédible (et on borne le buffer).
_MAX_TAIL = 80

# Chargé paresseusement (une seule lecture de sections.json par process).
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


def reset_for_tests() -> None:
    """Vide le cache de la carte (utile aux tests)."""
    global _SECTIONS
    _SECTIONS = None


def section_url_for(fname: str, line: int, base_url: str | None = None) -> str | None:
    """URL absolue ``BASE/chemin.html#ancre`` pour ``fname:line``, ou ``None``.

    ``fname`` peut être un chemin (« Courses/01_asynchronous.qmd ») — on ne
    garde que le basename. Fichier connu mais ligne hors de toute section →
    URL de la page sans ancre. Fichier inconnu → ``None`` (pas de lien).
    """
    if base_url is None:
        base_url = config.docs_base_url()
    if "/" in fname:
        fname = os.path.basename(fname)
    entry = sections_table().get(fname)
    if entry is None:
        return None
    url = f"{base_url.rstrip('/')}/{entry['html']}"
    lines = entry["lines"]
    idx = bisect.bisect_right(lines, line) - 1
    if idx >= 0 and entry["sections"][idx].get("slug"):
        url += f"#{entry['sections'][idx]['slug']}"
    return url


def _rewrite(text: str, base_url: str) -> str:
    def _repl(m: re.Match) -> str:
        url = section_url_for(m.group(1), int(m.group(2)), base_url)
        return f"[{m.group(1)}:{m.group(2)}]({url})" if url else m.group(0)

    return _CITE_RE.sub(_repl, text)


def rewrite_content(text: str, base_url: str | None = None) -> str:
    """Réécrit les mentions ``fichier:ligne`` connues d'un texte complet."""
    if base_url is None:
        base_url = config.docs_base_url()
    if not base_url or not text:
        return text
    return _rewrite(text, base_url)


class LinkRewriter:
    """Réécriture **streaming** : une citation peut être coupée entre deux chunks.

    On garde en attente (`pending`) une fenêtre de fin de buffer et on ne
    réécrit que la partie « sûre » (qui ne peut plus être prolongée en citation) :
    chaque ``feed`` découpe au début d'un éventuel suffixe ``*.qmd:…`` ou, à
    défaut, à ``_MAX_TAIL`` caractères de la fin. ``finish()`` vide le buffer
    restant. Sans base URL configurée, laisse passer tel quel.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = base_url if base_url is not None else config.docs_base_url()
        self.pending = ""

    def feed(self, chunk: str) -> list[str]:
        if not self.base_url:
            return [chunk] if chunk else []
        if not chunk:
            return []
        buf = self.pending + chunk
        m = _TAIL_RE.search(buf)
        if m and len(m.group(0)) <= _MAX_TAIL:
            cut = m.start()
        else:
            cut = max(0, len(buf) - _MAX_TAIL)
        safe, self.pending = buf[:cut], buf[cut:]
        if not safe:
            return []
        return [_rewrite(safe, self.base_url)]

    def finish(self) -> list[str]:
        if not self.pending:
            return []
        tail, self.pending = self.pending, ""
        return [_rewrite(tail, self.base_url)] if self.base_url else [tail]
