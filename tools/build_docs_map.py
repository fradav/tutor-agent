"""Génère ``corpus/sections.json`` : carte ligne→section pour la doc cliquable.

Lit les sources ``.qmd`` du book public (server-side book) et les ids du HTML
déjà rendu (``docs/``), et produit la table que ``tutor.docslinks`` utilise pour
transformer ``fichier:ligne`` en ``BASE/chemin.html#ancre``.

Pour chaque fichier source :
1. sections ``^#{1,4} `` avec leur numéro de ligne (hors blocs de code) ;
2. ids réels des titres dans le HTML rendu (``<h1..h4 id="…">``, sans
   ``toc-title`` qui est la TOC, pas une ancre) — ordre = ordre des sections ;
3. si les deux listes ont la même longueur, on les zippe (l'id du HTML est la
   vraie ancre, y compris les doublons ``-1``/``-2`` de quarto) ; sinon repli sur
   un slugify approximatif plus un avertissement.

Usage :
    python3 tools/build_docs_map.py [--book /chemin/book] [--out corpus/sections.json]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BOOK = HERE.parent.parent / "Cours-programmation-MIASHS-2026"
DEFAULT_OUT = HERE.parent / "corpus" / "sections.json"

HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
# Les titres ancrés de quarto portent l'id dans data-anchor-id sur le <hX> ; un
# titre de page h1 (class="title") n'a ni l'un ni l'autre. Une section sertie
# dans <section id="…"> a aussi un <h2> avec data-anchor-id — on s'en tient à
# cette source unique (un h avec les deux resterait compté une fois).
ID_RE = re.compile(r"<h([1-6])[^>]*?\bdata-anchor-id=\"([^\"]+)\"")
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
        headings = _fence_aware_headings(src.read_text(encoding="utf-8"))
        ids = _html_ids(html_path.read_text(encoding="utf-8"))
        # Les ancres ne portent QUE les sections (niveau >= 2) : le `#` de
        # titre de page n'a pas d'id dans le HTML. On appaire donc les sections
        # h2..h4 avec les ids (ordre = ordre du fichier).
        sections_h = [h for h in headings if h[2] != "#"]
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
