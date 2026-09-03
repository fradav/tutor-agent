"""Assainissement des sources ``.qmd`` du corpus : vidage en place de tout
contenu de réponse.

Les sujets de TP du book 2025 (``Courses/Applications/*.qmd``) embarquent leurs
réponses sous trois formes ; sans nettoyage le modèle les lit
(``grep_files``/``read_lines``) et les pages locales servies
(``www/Courses/Applications/*.html``) les affichent :

1. cellules de code quarto fencées ( `` ```{python} `` … `` ``` `` ) marquées
   ``#| tags: [solution]`` (variantes ``#!`` typoée et liste YAML multi-lignes) ;
2. blocs de div fenced ``:::solution`` / ``:::{.solution}`` / ``::::solution``
   (prose de réponse entre l'ouvrant et sa ligne de ``:`` de fermeture) ;
3. en-têtes Markdown dont l'étiquette *commence* par « solution » (ex.
   ``## Solution for main process function``) — les titres d'exercices qui ne
   font que contenir le mot (``## 9. Comparing Solutions``) restent visibles.

Ce module vide tout ce contenu **en conservant chaque ligne** (les lignes
deviennent vides, les fins de ligne restent) : les numéros de ligne
``fichier:ligne`` et l'alignement des ancres restent exacts.

Ne touche pas aux échafaudages d'exercice marqués ``#| eval: false`` (avec des
placeholders ``…``) : ce ne sont pas des réponses, et ils doivent rester
visibles pour que l'étudiant puisse les compléter.
"""
from __future__ import annotations

import re

# Ligne d'attributs quarto déclarant la cellule comme solution :
#   #| tags: [solution]
#   #| tags: [solution, foo]
#   #| tags: 'solution'
# … et le marqueur typoé ``#!`` (observé dans 04_0, sinon la cellule fuit).
_ATTRS_SOL_RE = re.compile(r"^\s*#+[|!]?\s*tags\s*:.*\bsolution\b", re.IGNORECASE)
# Forme liste YAML multi-lignes (défensive, non observée dans le corpus) :
#   #| tags:
#   #|   - solution
_YAML_SOL_RE = re.compile(r"^\s*#+[|!]?\s*-\s*solution\s*$", re.IGNORECASE)
# Ouverture d'un div fenced de solution : ``:::solution``, ``:::{.solution}``,
# ``::::solution`` (les ``::: {.callout-*}`` ne doivent pas matcher).
_SOL_DIV_OPEN_RE = re.compile(
    r"^\s*:{3,}\s*\{?\s*\.?\s*solutions?\s*\}?\s*$", re.IGNORECASE)
# Fermeture d'un div fenced : une ligne qui n'est que des ``:``.
_DIV_CLOSE_RE = re.compile(r"^\s*:{3,}\s*$")
# En-tête Markdown dont l'étiquette *commence* par « solution » / « solutions »
# (ex. ``## Solution for main process function`` = étiquette de réponse). Un titre
# d'exercice qui contient juste le mot (ex. ``## 9. Comparing Solutions``,
# ``## Pure `asyncio` solution``) n'est pas une réponse et reste visible.
_HEADING_SOL_RE = re.compile(r"^\s*#{1,6}\s+[*_]*solutions?\b", re.IGNORECASE)


def strip_solution_cells(text: str) -> str:
    """Retourne ``text`` sans le contenu des cellules ``tags: [solution]``,
    des blocs de div ``:::solution`` et des en-têtes « solution ».

    Chaque ligne concernée est remplacée par une ligne vide (les fins de ligne
    sont conservées : la numérotation source reste valide, et les titres — donc
    les ancres — ne bougent pas).
    """
    lines = text.splitlines(keepends=True)
    blanked = [False] * len(lines)
    in_fence = False
    is_solution = False
    fence_start = -1
    for i, line in enumerate(lines):
        if not in_fence:
            if line.lstrip().startswith("```"):
                in_fence = True
                is_solution = False
                fence_start = i
            continue
        # Dans un bloc de code ouvert : fin de cellule, ou tag solution.
        if line.lstrip().startswith("```"):
            if is_solution:
                for j in range(fence_start, i + 1):
                    blanked[j] = True
            in_fence = False
            continue
        if _ATTRS_SOL_RE.search(line) or _YAML_SOL_RE.search(line):
            is_solution = True
    # Blocs de div de solution : on vide de l'ouvrant jusqu'à sa fermeture.
    _blank_solution_divs(lines, blanked)
    # En-têtes « solution » : on vide la ligne de l'en-tête.
    for i, line in enumerate(lines):
        if _HEADING_SOL_RE.match(line):
            blanked[i] = True
    out: list[str] = []
    for i, line in enumerate(lines):
        if blanked[i]:
            # Conserver la structure de lignes (fins de ligne incluses), vider
            # le contenu.
            out.append("\n" if line.endswith("\n") else "")
        else:
            out.append(line)
    return "".join(out)


def _blank_solution_divs(lines: list[str], blanked: list[bool]) -> None:
    """Vide (sur place, dans ``blanked``) les blocs ``:::solution``.

    D'un ouvrant ``:::solution``/``:::{.solution}``/``::::solution`` jusqu'à la
    ligne suivante qui n'est que des ``:`` (fermeture du div). Les divs du
    corpus ne sont pas imbriqués ; la fermeture par première ligne de ``:``
    suffit.
    """
    i = 0
    n = len(lines)
    while i < n:
        if _SOL_DIV_OPEN_RE.match(lines[i]):
            j = i + 1
            while j < n and not _DIV_CLOSE_RE.match(lines[j]):
                j += 1
            end = j if j < n else n - 1
            for k in range(i, end + 1):
                blanked[k] = True
            i = end + 1
        else:
            i += 1


def count_solution_cells(text: str) -> int:
    """Nombre de cellules ``tags: [solution]`` dans ``text`` (pour les tests)."""
    lines = text.splitlines(keepends=True)
    in_fence = False
    count = 0
    seen = False
    for line in lines:
        if not in_fence:
            if line.lstrip().startswith("```"):
                in_fence = True
                seen = False
            continue
        if line.lstrip().startswith("```"):
            if seen:
                count += 1
                seen = False
            in_fence = False
            continue
        if _ATTRS_SOL_RE.search(line) or _YAML_SOL_RE.search(line):
            seen = True
    return count


def count_solution_divs(text: str) -> int:
    """Nombre de blocs ``:::solution`` dans ``text`` (pour les tests)."""
    count = 0
    for line in text.splitlines():
        if _SOL_DIV_OPEN_RE.match(line):
            count += 1
    return count


def count_solution_headings(text: str) -> int:
    """Nombre d'en-têtes « solution » dans ``text`` (pour les tests)."""
    count = 0
    for line in text.splitlines():
        if _HEADING_SOL_RE.match(line):
            count += 1
    return count
