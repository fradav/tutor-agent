"""Liens cliquables vers le book public + doc Python : réécriture des citations.

Le tuteur (3 modèles, harnais ACP) cite le corpus avec ``nom.qmd:ligne`` (même
affichage, en gras, dans ``tools.format_quote``) et la doc Python officielle
avec ``python:<ref>`` (module, éventuellement ``module#ancre``). Ce module
transforme ces mentions en liens cliquables :

- ``nom.qmd:ligne`` → ``[nom.qmd:ligne](BASE/chemin.html#ancre)`` — book
  servi en local par ``tutor.docs`` (cartes ``corpus/sections.json``) ; un
  préfixe de dossier est supporté et conservé dans le libellé
  (``Applications/03_0_Asynchronous.qmd:63`` →
  ``[Applications/03_0_Asynchronous.qmd:63](BASE/Courses/Applications/03_0_Asynchronous.html#…))`` ;
- ``nom.qmd`` **seul** (label nu — cellule de tableau, prose sans ligne) →
  ``[nom.qmd](BASE/chemin.html)`` — lien de la page entière : tout fichier
  connu du corpus reste cliquable même quand le modèle ne reproduit pas
  l'adjacence ``nom:ligne`` (vue synthèse) ;
- ``python:<mod>`` → ``[python:<mod>](BASE/py/library/<mod>.html)`` (ou
  ``https://docs.python.org/3/library/<mod>.html`` sans miroir local) — la
  base est ``docs.effective_base_url()`` (port réel du serveur, repli inclus).
- des backticks `` ` `` qui encadrent directement une mention sont retirés :
  sinon le lien se rendrait en span de code (non cliquable).

La base du book est ``tutor.docs.effective_base_url()`` : le port configuré,
ou le port de repli quand ``ensure()`` a dû se relancer ailleurs (port squatté
par un autre processus).

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
du book est neutre : le texte passe tel quel. Les citations ``python:`` ne
dépendent que de la base doc Python (en ligne par défaut).
"""

from __future__ import annotations

import bisect
import json
import os
import re
from typing import Any

from . import config, docs

# Un motif de citation : `nom.qmd:ligne` (l'extension ``.qmd`` fait partie du
# groupe = clé de la carte), éventuellement préfixé d'un chemin de dossier
# (`Applications/03_0_Asynchronous.qmd:63`) — le préfixe est capturé (groupe
# ``path``) pour que ``Applications/`` fasse partie du libellé du lien (sinon il
# reste hors du lien et se rend en texte/code non cliquable). Frontière gauche =
# pas un caractère de « nom de fichier » ni un ``/`` (on ne réécrit pas au
# milieu d'un chemin ou d'un identifiant) ; frontière droite = pas un chiffre
# (pour ne pas avaler `1200` en citant :120). Un wrapper de backticks `` ` ``
# directement autour de la mention (groupe 1, facultatif) est avalé puis retiré
# dans ``_rewrite`` : sinon le lien se rendrait en span de code non cliquable
# dans la réponse du modèle.
_CITE_RE = re.compile(
    r"(?P<ouvr>`{0,2})"
    r"(?<![A-Za-z0-9_.\-/])"
    r"(?P<path>(?:[A-Za-z0-9_][A-Za-z0-9_.\-]*/)*)"
    r"(?P<fname>[A-Za-z0-9_][A-Za-z0-9_.\-]*\.qmd):(?P<line>\d+)(?![0-9])"
    r"(?P=ouvr)"
)

# Une citation de doc Python : `python:<ref>` où `<ref>` = module
# (`asyncio`, `queue`) ou module#ancre (`queue#SimpleQueue`). Frontière =
# pas un caractère de nom — ne pas avaler `python:` suivi d'un mot courant.
# Un point n'est autorisé qu'ENTRE deux portions d'identifiant
# (`asyncio.base_events`) : un `.` final de phrase (`python:asyncio.`) ne doit
# pas finir dans le lien (sinon URL `asyncio..html` → 404 → lien mort).
# Même wrapper de backticks facultatif que ``_CITE_RE`` (retiré à la réécriture).
_PY_REF_RE = re.compile(
    r"(?P<ouvr>`{0,2})"
    r"(?<![A-Za-z0-9_])python:([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*(?:#[A-Za-z0-9_.-]+)?)(?![A-Za-z0-9#_])(?P=ouvr)"
)

# Suffixe de fin de chunk qui peut être un début de citation coupé en deux :
# on retient tout suffixe susceptible de devenir `*.qmd`, `*.qmd:`, `*.qmd:1`…
# ou `python:`, `python:asynci`, `python:queue#…` pour ne pas casser un lien
# dont la suite arrive dans le chunk suivant. Dernière branche : toute fin de
# mot/caractère de nom (`[A-Za-z0-9_.\-]+`) est retenue pour qu'un nom de
# fichier nu (`01_asynchron` + `ous.qmd`) coupé entre deux chunks soit recomposé
# avant réécriture. Le backtick (`` ` ``) autorisé en tête des deux premières
# branches permet de recomposer aussi un nom coupé à l'intérieur de backticks.
# Le ``/`` et le ``.`` des classes couvrent aussi un préfixe de chemin
# (`Applications/03_0_Asynchronous`) coupé entre deux chunks : le chemin entier
# est retenu en attente pour être recomposé avant réécriture. Un backtick de
# FERMETURE (`` `{0,2} `` en fin des deux premières branches) est avalé aussi :
# une citation backtickée en toute fin de contenu (`Voir ``python:asyncio```,
# ```03_0_Asynchronous.qmd:1007``` en fin de réponse) doit rester entière dans le
# buffer, sinon ``python:``/``.qmd:`` serait écrit et ``asyncio` ``/``1007` ``
# finirait en attente — le lien ne serait jamais re-réécrit (symptôme
# « les liens ne sont pas là »).
_TAIL_RE = re.compile(
    r"(?:`{0,2}[A-Za-z0-9_.\-/]*\.qmd:?\d*`{0,2}"  # recoupe ``01_asynchron`` → `01_asynchronous.qmd:12`
    r"|`{0,2}python:[A-Za-z0-9_.\-]*(?:#[A-Za-z0-9_.\-]*)?`{0,2}"
    r"|[A-Za-z0-9_.\-/`]+)$"
)

# Borne haute d'un suffixe retenu : au-delà, ce n'est pas un nom de fichier
# crédible (et on borne le buffer).
_MAX_TAIL = 80

# Chargé paresseusement (une seule lecture de sections.json par process).
_SECTIONS: dict[str, Any] | None = None

# Regex des mentions « nues » d'un fichier connu du corpus (label sans ligne),
# construite paresseusement depuis la carte : `01_asynchronous.qmd` → lien de
# page. Seuls les noms connus sont reconnus (jamais de lien vers un fichier
# inconnu) ; un `nom.qmd` suivi de `:chiffres` est laissé à `_CITE_RE`.
_FILENAMES_RE: tuple[tuple[str, ...], re.Pattern | None] = ((), None)


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
    """Noms de fichiers connus du corpus (basenames, du plus long au plus
    court pour que l'alternance matche le nom complet d'abord)."""
    return tuple(sorted({os.path.basename(f) for f in sections_table()},
                        key=len, reverse=True))


def _filename_re() -> re.Pattern | None:
    """Motif des fichiers du corpus cités **sans** ligne (label nu).

    Facade arrière ``[a-z0-9_.-/]`` (pas au milieu d'un identifiant ni d'un
    chemin) et devant rien qui prolonge un nom (`a-z0-9_-:`) — une mention
    ``nom.qmd:NN`` reste gérée par ``_CITE_RE``. Un ``.`` final (fin de phrase)
    est autorisé. Un préfixe de dossier (`Applications/`) devant le nom est
    capturé (groupe ``path``) et conservé dans le libellé du lien.
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
    """URL absolue ``BASE/chemin.html#ancre`` pour ``fname:line``, ou ``None``.

    ``fname`` peut être un chemin (« Courses/01_asynchronous.qmd ») — on ne
    garde que le basename. Fichier connu mais ligne hors de toute section →
    URL de la page sans ancre. Fichier inconnu → ``None`` (pas de lien).
    """
    if base_url is None:
        base_url = docs.effective_base_url()
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


def _python_base_url() -> str:
    """Base des citations ``python:<ref>`` : miroir local ``/py`` quand
    ``docs.py_dir`` est posé (port réel du serveur docs, y compris repli), sinon
    https://docs.python.org/3/ (en ligne)."""
    if config.py_dir():
        return f"{docs.effective_base_url().rstrip('/')}/py"
    return config.python_doc_base_url()


def _python_url(ref: str) -> str:
    """URL de la doc Python pour une citation ``python:<ref>``.

    Base = ``_python_base_url()`` : miroir local ``BASE/py`` quand
    ``docs.py_dir`` est posé, sinon https://docs.python.org/3/ (en ligne).
    ``python:queue#SimpleQueue`` → ``<base>/library/queue.html#SimpleQueue``.
    """
    mod, _, anchor = ref.partition("#")
    url = f"{_python_base_url().rstrip('/')}/library/{mod}.html"
    return f"{url}#{anchor}" if anchor else url


def _rewrite(text: str, base_url: str) -> str:
    def _repl(m: re.Match) -> str:
        # groupe 1 = wrapper de backticks facultatif (voir ``_CITE_RE``) : on le
        # laisse tomber pour que le lien se rende cliquable, pas en span de code.
        # Le préfixe de dossier (``path``) est dans le libellé, sinon il resterait
        # hors du lien et le casserait.
        fname = m.group("path") + m.group("fname")
        url = section_url_for(fname, int(m.group("line")), base_url)
        return f"[{fname}:{m.group('line')}]({url})" if url else m.group(0)

    text = _CITE_RE.sub(_repl, text)

    # label nu (fichier connu sans ligne) → lien de la page : ligne 0 → jamais
    # d'ancre (page complète). Couvre les synthèses du modèle qui séparent le
    # nom du numéro de ligne (tableaux, `**Fichier :** …`) — le label reste
    # alors cliquable, on perd seulement la précision de l'ancre.
    fname_re = _filename_re()
    if fname_re:
        def _fname_repl(m: re.Match) -> str:
            # groupe 1 = wrapper de backticks facultatif (voir ``_filename_re``) ;
            # le préfixe de dossier (``path``) est dans le libellé du lien.
            fname = m.group("path") + m.group("fname")
            url = section_url_for(fname, 0, base_url)
            return f"[{fname}]({url})" if url else m.group(0)

        text = fname_re.sub(_fname_repl, text)

    def _pyrepl(m: re.Match) -> str:
        # groupe 1 = wrapper de backticks facultatif (voir ``_PY_REF_RE``).
        return f"[python:{m.group(2)}]({_python_url(m.group(2))})"

    return _PY_REF_RE.sub(_pyrepl, text)


def rewrite_content(text: str, base_url: str | None = None) -> str:
    """Réécrit les mentions ``fichier:ligne`` connues et ``python:<ref>`` d'un
    texte complet (base ``base_url`` pour le book, base Python indépendante)."""
    if base_url is None:
        base_url = docs.effective_base_url()
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
        self.base_url = (base_url if base_url is not None
                         else docs.effective_base_url())
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
