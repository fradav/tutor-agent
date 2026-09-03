"""Génère ``sections.json`` du dépôt jumeau : carte ligne→section pour la doc
cliquable.

Lit les sources ``.qmd`` du book public (server-side book) et les ids du HTML
déjà rendu (``docs/``), et produit la table que ``tutor.docslinks`` utilise pour
transformer ``fichier:ligne`` en ``BASE/chemin.html#ancre``.

Fiabilité : seules les ancres réelles du HTML rendu (``data-anchor-id`` des
``<h2..h6>``) sont retenues. Les titres sources sont alignés sur le HTML dans
l'ordre, par texte normalisé ; un titre sans correspondance exacte est écarté
(pas de slugify approximatif → jamais de lien mort).

Cas du book dérivé de slides (book 2025), gérés ici :
- les attributs slides ``{...}`` (``{.smaller}``, ``{auto-animate=…}``…) sont
  retirés du texte comparé ;
- les commentaires HTML ``<!-- … -->`` sont retirés avant l'extraction (ils
  peuvent contenir des blocs ``` factices et des titres jamais rendus), en
  conservant les numéros de ligne sources ;
- les titres ``#`` (niveau 1) sont exclus : titres de page ou découpes de
  chapitre slide, jamais ancrés ;
- un doublon suffixé ``(2)/(3)`` — que quarto ne rend qu'une fois — ne matche
  plus aucune ancre et tombe donc hors carte.

Sections : ``##`` et plus, hors blocs de code.

TP (annexe B) : les sujets ``Courses/Applications/*.qmd`` ne sont pas rendus en
HTML par le book — la carte est donc complétée par une page locale ancrée
créée sous ``--www/Courses/Applications/<stem>.html`` (titres en ``<h2..h6>
id="ancre" data-anchor-id="ancre"``, reste du fichier échappé en ``<pre>``),
pour que les citations ``fichier:ligne`` du tuteur restent cliquables. Le book
2025 sert de source de vérité pour les sujets ; les solutions ne sont jamais
indexées ni servies.

Usage :
    python3 tools/build_docs_map.py [--book /chemin/book] [--out sections.json]
                                    [--www /chemin/www]
"""
from __future__ import annotations

import argparse
import html as H
import json
import re
import sys
import unicodedata as U
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Exécuté en script autonome (``python3 tools/build_docs_map.py``) : le paquet
# ``tutor`` n'est pas sur sys.path. Bootstrap pour réutiliser le sanitiser du
# dépôt (source de vérité unique avec ``tutor/tools.py``).
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))
from tutor.sanitize import strip_solution_cells  # noqa: E402
DEFAULT_BOOK = Path(
    "/Users/fradav/Documents/Dev/Python/Cours-programmation-MIASHS-2025")
DEFAULT_OUT = Path(
    "/Users/fradav/Documents/Dev/Teaching/MIASHS-Configuration-Tutorat/sections.json")
# Racine servie par tutor.docs : les pages locales des TP y sont écrites
# (``www/Courses/Applications/<stem>.html``), les chapitres restent lus depuis
# ``book/docs/`` (HTML réellement rendu).
DEFAULT_WWW = Path(
    "/Users/fradav/Documents/Dev/Teaching/MIASHS-Configuration-Tutorat/www")

# Commentaires HTML : retirés avant l'extraction des titres (ils faussaient le
# comptage de fences et avalaient des titres jamais rendus).
COMM_RE = re.compile(r"<!--.*?-->", re.S)
# Titres de contenu = niveau >= 2 : les # sont titre de page / chapitre slide.
HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$")
# Ancres réelles : <h2..h6 data-anchor-id="…">, avec le texte rendu.
ID_RE = re.compile(
    r"<h([2-6])[^>]*?\bdata-anchor-id=\"([^\"]+)\"[^>]*>(.*?)</h\1>", re.S)
TOC_TITLE = "toc-title"


def _strip_comments(text: str) -> str:
    """Retire les commentaires HTML en conservant les numéros de ligne."""
    return COMM_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def _fence_aware_headings(text: str) -> list[tuple[int, str]]:
    """(ligne, titre brut) des titres markdown (h2..h6) hors blocs ``` ."""
    items: list[tuple[int, str]] = []
    in_fence = False
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEADING_RE.match(raw)
        if m:
            items.append((i, m.group(2).strip()))
    return items


def _norm(s: str | None, html_side: bool = False) -> str:
    """Texte comparable d'un titre, source ou rendu.

    * entités HTML dépliées, nbsp → espace ;
    * attributs slides ``{...}`` retirés ;
    * emphases markdown (``**``, backticks…) retirées ;
    * côté HTML : la numérotation quarto en tête ("2 ", "3.1 ") est retirée ;
    * doublons suffixés ``(N)`` retirés (quarto ne les rend pas en ancre) ;
    * accents normalisés (NFKD), ponctuation → espace, casse réduite.
    """
    if not s:
        return ""
    t = H.unescape(s).replace("\xa0", " ")
    t = re.sub(r"<[^>]+>", "", t)              # balises (surtout côté HTML)
    t = re.sub(r"\{[^}]*\}", "", t)            # attributs slides {...}
    t = re.sub(r"[`*_~]", "", t)               # emphases markdown
    t = re.sub(r"^[0-9]+(\.[0-9]+)*\s*", "", t)  # la numérotation quarto
    t = re.sub(r"\s*\(\d+\)\s*$", "", t)       # doublons suffixés (2)
    t = U.normalize("NFKD", t)
    t = "".join(c for c in t if not U.combining(c))
    t = re.sub(r"[^a-z0-9.\- ]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def _html_anchors(html_text: str) -> list[tuple[str, str]]:
    """[(ancre réelle, texte rendu normalisé)] des titres rendus, dans l'ordre."""
    return [(m.group(2), _norm(m.group(3), html_side=True))
            for m in ID_RE.finditer(html_text) if m.group(2) != TOC_TITLE]


def _slugify(title: str) -> str:
    """Slug quarto-like d'un titre source (auto_identifiers pandoc).

    Uniquement pour les pages locales des TP : id stable et unique, sans
    dépendre d'un rendu quarto (les sujets ne sont pas compilés.). Pas de
    slugify approximatif hors de ce cas — les chapitres gardent les ancres
    réelles du HTML rendu.
    """
    t = H.unescape(title)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\{[^}]*\}", "", t)          # attributs slides {...}
    t = re.sub(r"[`*_~]", "", t)               # emphases markdown
    t = re.sub(r"\s*\(\d+\)\s*$", "", t)    # doublons suffixés (2)
    t = U.normalize("NFKD", t)
    t = "".join(c for c in t if not U.combining(c))
    t = re.sub(r"[^a-z0-9 ._\-]", "", t.lower())
    t = re.sub(r"[ ._]+", "-", t).strip("-")
    return t or "section"


def _inline(s: str) -> str:
    """Échappe un titre pour l'affichage : HTML échappé + emphases minimales."""
    s = H.escape(s, quote=False)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def synthesize_tp_html(src_text: str, stem: str) -> str:
    """Page locale d'un sujet TP : sections ``##…`` ancrées + reste en ``<pre>``.

    Les titres (h2..h6, hors fences) deviennent des éléments ancrés
    (``id``/``data-anchor-id`` : slug quarto-like) que ``_build_sections``
    aligne ensuite sur les titres sources ; tout le reste du fichier est
    échappé HTML dans un ``<pre>`` par section — les citations
    ``fichier:ligne`` du tuteur ouvrent cette page à la bonne ancre.
    """
    body: list[str] = []
    pre: list[str] = []
    seen: dict[str, int] = {}
    in_fence = False

    def flush_pre() -> None:
        if pre:
            body.append("<pre>" + "".join(pre) + "</pre>\n")
            pre.clear()

    for line in _strip_comments(src_text).splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            pre.append(H.escape(line, quote=False) + "\n")
            continue
        m = HEADING_RE.match(line)
        if not in_fence and m:
            level = len(m.group(1))
            title = m.group(2).strip()
            slug = _slugify(title)
            seen[slug] = seen.get(slug, 0) + 1      # doublons pandoc : -1, -2…
            if seen[slug] > 1:
                slug = f"{slug}-{seen[slug] - 1}"
            flush_pre()
            body.append(
                f"<h{level} id=\"{slug}\" data-anchor-id=\"{slug}\">"
                f"{_inline(title)}</h{level}>\n")
        else:
            pre.append(H.escape(line, quote=False) + "\n")
    flush_pre()
    return (f"<!DOCTYPE html>\n<html lang=\"en\"><head>\n"
            f"<meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>Application — {H.escape(stem)}</title>\n"
            f"</head><body>\n" + "".join(body) + "</body></html>\n")


def _build_sections(src_text: str, html_text: str) -> tuple[list[dict], int, int, int]:
    """(sections, nb ancres HTML, ancres non mappées, titres écartés).

    Alignement ordre + texte : pour chaque titre source, on cherche la prochaine
    ancre HTML non consommée de même texte ; les ancres sautées entre-temps sont
    des orphelins (jamais de cible source), les titres sans ancre sont écartés.
    """
    headings = _fence_aware_headings(_strip_comments(src_text))
    anchors = _html_anchors(html_text)
    sections: list[dict] = []
    used = 0
    skipped = 0
    for line, title in headings:
        sn = _norm(title)
        if not sn:
            continue
        target: str | None = None
        j = used
        while j < len(anchors):
            if anchors[j][1] == sn:
                target = anchors[j][0]
                used = j + 1
                break
            j += 1
        if target is None:
            skipped += 1
            continue
        sections.append({"line": line, "slug": target, "title": title})
    return sections, len(anchors), len(anchors) - used, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", type=Path, default=DEFAULT_BOOK,
                        help="racine du book public (avec docs/ rendu)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="fichier sections.json à écrire")
    parser.add_argument("--www", type=Path, default=DEFAULT_WWW,
                        help="racine servie localement (pages locales des TP)")
    args = parser.parse_args()

    book = args.book.resolve()
    docs = book / "docs"
    if not (docs / "index.html").exists():
        print(f"erreur: pas de docs/index.html rendu sous {docs}")
        return 2

    sources = (sorted(book.glob("*.qmd"))
               + sorted((book / "Courses").glob("*.qmd"))
               + sorted((book / "Courses" / "Applications").glob("*.qmd")))
    table: dict[str, dict] = {}
    total = 0
    total_unused = 0
    for src in sources:
        rel = src.relative_to(book)
        stem = src.stem
        src_text = src.read_text(encoding="utf-8")
        if rel.parts[:2] == ("Courses", "Applications"):
            # Sujets de TP (annexe B) : les cellules ``tags: [solution]``
            # embarquées dans les sources du book sont vidées — elles ne
            # doivent être ni servies (page locale) ni indexées (carte).
            src_text = strip_solution_cells(src_text)
            # Sujets de TP : pas de HTML rendu par le book → page locale ancrée.
            html_rel = f"Courses/Applications/{stem}.html"
            html_text = synthesize_tp_html(src_text, stem)
            html_dest = args.www / html_rel
            html_dest.parent.mkdir(parents=True, exist_ok=True)
            html_dest.write_text(html_text, encoding="utf-8")
            print(f"  {rel}: HTML local généré ({html_dest.relative_to(args.www)})")
        else:
            html_rel = (f"Courses/{stem}.html" if rel.parts[0] == "Courses"
                        else f"{stem}.html")
            html_path = docs / html_rel
            if not html_path.exists():
                print(f"  {rel}: pas de HTML rendu ({html_rel}) — ignoré")
                continue
            html_text = html_path.read_text(encoding="utf-8")
        sections, n_ids, unused, skipped = _build_sections(
            src_text, html_text)
        note = []
        if unused:
            note.append(f"{unused} ancres HTML non mappées")
        if skipped:
            note.append(f"{skipped} titres écartés")
        print(f"  {rel}: {len(sections)} sections · {n_ids} ids HTML"
              + (f" ({', '.join(note)})" if note else ""))
        total += len(sections)
        total_unused += unused
        if sections:
            table[stem + ".qmd"] = {"html": html_rel, "sections": sections}

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(table, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    print(f"écrit {args.out.resolve()} ({len(table)} fichiers)")
    print(f"{total} sections indexées · {total_unused} ancres HTML non mappées")
    sample = {f: [(s["line"], s["slug"]) for s in table[f]["sections"][:3]]
              for f in ["01_Code-Assistant.qmd", "02_Parallel-intro.qmd",
                        "06_Dask-Ray.qmd", "11_1_WebGPU-matmul.qmd"]
              if f in table}
    print("extrait:", json.dumps(sample, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
