"""Assainissement des sources ``.qmd`` du corpus : vidage en place des cellules
de code marquées ``tags: [solution]``.

Les sujets de TP du book 2025 (``Courses/Applications/*.qmd``) embarquent leurs
réponses dans des cellules quarto fencées (`` ```{python} `` … `` ``` ``) marquées
``#| tags: [solution]``. Sans nettoyage, le modèle les lit
(``grep_files``/``read_lines``) et les pages locales servies
(``www/Courses/Applications/*.html``) les affichent. Ce module vide le corps de
ces cellules **en conservant chaque ligne** (les lignes deviennent vides, les
fins de ligne restent) : les numéros de ligne ``fichier:ligne`` et l'alignement
des ancres restent exacts.

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


def strip_solution_cells(text: str) -> str:
    """Retourne ``text`` sans le contenu des cellules ``tags: [solution]``.

    Chaque ligne d'une cellule de solution est remplacée par une ligne vide
    (les fins de ligne sont conservées : la numérotation source reste valide,
    et les titres — donc les ancres — ne bougent pas).
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
    out: list[str] = []
    for i, line in enumerate(lines):
        if blanked[i]:
            # Conserver la structure de lignes (fins de ligne incluses), vider
            # le contenu.
            out.append("\n" if line.endswith("\n") else "")
        else:
            out.append(line)
    return "".join(out)


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
