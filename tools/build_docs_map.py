"""Génère ``sections.json`` du dépôt jumeau : carte ligne→section pour la doc
cliquable.

Lit les sources ``.qmd`` du book public (server-side book) et les ids du HTML
déjà rendu (``docs/``), et produit la table que ``tutor.docslinks`` utilise pour
transformer ``fichier:ligne`` en ``BASE/chemin.html#ancre``.

Pour chaque fichier source :
1. sections ``^#{1,4} `` avec leur numéro de ligne (hors blocs de code) ;
2. le titre de page est identifié et écarté — il ne porte jamais d'ancre : il
   vient du ``title:`` du frontmatter, ou bien, sans frontmatter, quarto replie
   la première section source dans le header (``<h1 class="title">``, avec le
   préfixe de numérotation "2 "/"Appendix B — " à retirer pour l'identifier) ;
3. ids réels des titres dans le HTML rendu (``<h2..h4 data-anchor-id="…">``) —
   ordre = ordre des sections ;
4. si les deux listes ont la même longueur, on les zippe (l'id du HTML est la
   vraie ancre, y compris les doublons ``-1``/``-2`` de quarto) ; sinon repli sur
   un slugify approximatif plus un avertissement.

Les sections de contenu doivent être ``##`` ou plus : un ``#`` de corps est
interprété par quarto comme titre de page (replié dans le header, sans ancre) et
s'exclut donc de la carte. C'est une exigence du book, rappelée dans les README
jumeau et du runner.

Usage :
    python3 tools/build_docs_map.py [--book /chemin/book] [--out sections.json]
"""
from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BOOK = HERE.parent.parent / "Cours-programmation-MIASHS-2026"
DEFAULT_OUT = Path(
    "/Users/fradav/Documents/Dev/Teaching/MIASHS-Configuration-Tutorat/sections.json")

HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
# Les sections ancrées de quarto (niveau >= 2) portent leur id dans
# data-anchor-id sur le <hX>. Le titre de page h1 (class="title") n'a ni id ni
# data-anchor-id — on le repère pour l'écarter, pas pour en faire une section.
# Une section sertie dans <section id="…"> a aussi un <h2> avec data-anchor-id
# : on s'en tient à cette source unique (un h avec les deux resterait compté une
# fois).
ID_RE = re.compile(r"<h([2-6])[^>]*?\bdata-anchor-id=\"([^\"]+)\"")
TITLE_RE = re.compile(r"<h1[^>]*class=\"[^\"]*\btitle\b[^\"]*\"[^>]*>(.*?)</h1>", re.S)
FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(\n|$)", re.S)
TOC_TITLE = "toc-title"


def _fence_aware_headings(text: str) -> list[tuple[int, str, str]]:
    """(ligne, titre, niveau) des titres markdown, en ignorant les blocs ``` ."""
    items: list[tuple[int, str, str]] = []
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
            items.append((i, m.group(2).strip(), m.group(1)))
    return items


def _html_ids(html: str) -> list[str]:
    ids = [m.group(2) for m in ID_RE.finditer(html) if m.group(2) != TOC_TITLE]
    return ids


def _norm(s: str | None) -> str:
    """Normalisation pour comparaison de titres (espaces/casse)."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _frontmatter_title(src: str) -> str | None:
    """Valeur du `title:` du bloc YAML de tête, ou None.

    Quarto replie toute section de niveau >= 2 dont le texte est exactement le
    titre de page dans le header (sans ancre). C'est la référence fiable — le
    `<h1 class="title">` rendu porte un préfixe de numérotation ("2 ",
    "Appendix B — ") qui casse la comparaison.
    """
    m = FM_RE.search(src)
    if not m:
        return None
    tm = re.search(r"(?m)^title\s*:\s*(.+?)\s*$", m.group(1))
    if not tm:
        return None
    return _norm(tm.group(1).strip().strip('\"\''))


def _is_page_title(heading: str, rendered: str | None) -> bool:
    """La 1ʳᵉ section source joue-t-elle le rôle de titre de page ?

    Sans `title:` de frontmatter, quarto prend la première section source comme
    titre et la replie dans le header (jamais ancrée). On compare au `<h1
    class="title">` rendu en retirant la numérotation ajoutée ("4 " ou
    "Appendix B — ").
    """
    if not rendered:
        return False
    t = re.sub(r"^appendix [a-z] — ", "", _norm(rendered))
    t = re.sub(r"^\d+ ", "", t)
    return t == _norm(heading)


def _page_title(page: str) -> str | None:
    """Texte du titre de page h1 rendu par quarto, ou None."""
    m = TITLE_RE.search(page)
    if not m:
        return None
    inner = re.sub(r"<[^>]+>", "", m.group(1))
    return _norm(html.unescape(inner))


def _slugify(title: str) -> str:
    """Repli approximatif de l'id quarto (minuscules, espaces→-, . conservés)."""
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9._\-\s\u00e0-\u00ff]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s.strip("-")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", type=Path, default=DEFAULT_BOOK, help="racine du book public (avec docs/ rendu)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="fichier sections.json à écrire")
    args = parser.parse_args()

    book = args.book.resolve()
    docs = book / "docs"
    if not (docs / "index.html").exists():
        print(f"erreur: pas de docs/index.html rendu sous {docs}")
        return 2

    sources = sorted(book.glob("*.qmd")) + sorted((book / "Courses").glob("*.qmd"))
    table: dict[str, dict] = {}
    warnings: list[str] = []
    for src in sources:
        rel = src.relative_to(book)
        stem = src.stem
        html_rel = f"Courses/{stem}.html" if rel.parts[0] == "Courses" else f"{stem}.html"
        html_path = docs / html_rel
        if not html_path.exists():
            warnings.append(f"— pas de HTML rendu pour {rel} ({html_rel})")
            continue
        src_text = src.read_text(encoding="utf-8")
        headings = _fence_aware_headings(src_text)
        html_text = html_path.read_text(encoding="utf-8")
        ids = _html_ids(html_text)
        # Le titre de page ne porte jamais d'ancre : soit il vient du `title:`
        # du frontmatter (fiable), soit — sans frontmatter — quarto replie la
        # première section source dans le header, avec le numéro de chapitre ou
        # le préfixe appendix en moins. On écarte cette section-là aussi.
        page_title = _frontmatter_title(src_text)
        sections_h = [h for h in headings if _norm(h[1]) != _norm(page_title)]
        if page_title is None and headings:
            first = headings[0]
            if _is_page_title(first[1], _page_title(html_text)):
                sections_h = sections_h[1:]
        sections: list[dict] = []
        if ids and len(ids) == len(sections_h):
            for (line, title, _lvl), slug in zip(sections_h, ids):
                sections.append({"line": line, "slug": slug, "title": title})
        else:
            warnings.append(
                f"— {rel}: {len(sections_h)} sections vs {len(ids)} ids HTML "
                f"→ repli slugify (à vérifier)"
            )
            for line, title, _lvl in sections_h:
                slug = _slugify(title)
                if slug:
                    sections.append({"line": line, "slug": slug, "title": title})
        print(f"  {rel}: {len(sections_h)} sections"
              + (f" ({len(ids)} ids)" if ids and len(ids) == len(sections_h) else ""))
        if sections:
            table[stem + ".qmd"] = {
                "html": html_rel,
                "sections": sections,
            }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(table, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    print(f"écrit {args.out.resolve()} ({len(table)} fichiers)")
    total = sum(len(v["sections"]) for v in table.values())
    first = {}
    for fname in ["01_asynchronous.qmd", "00_intro-parallelism.qmd", "projects.qmd",
                  "appendix-llm-docs.qmd"]:
        if fname in table:
            first[fname] = [(s["line"], s["slug"]) for s in table[fname]["sections"][:3]]
    print(f"{total} sections indexées")
    print("extrait:", json.dumps(first, ensure_ascii=False))
    for w in warnings:
        print(w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
