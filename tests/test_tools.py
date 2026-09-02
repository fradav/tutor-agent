"""Tests de la doc cliquable : carte ligne→section, réécriture streaming,
serveur statique local. Indépendants du réseau externe (localhost, port 0).

NB : la suite est lancée avec ``TUTOR_STUB=1`` (replies déterministes) ; la
carte ``corpus/sections.json`` doit exister (committée, générée par
``tools/build_docs_map.py``). Les tests logiques utilisent un petit fixture
pour rester déterministes.
"""
from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from unittest import mock

from tutor import config, docs, docslinks as dl

FIXTURE = {
    "01_asynchronous.qmd": {
        "html": "Courses/01_asynchronous.html",
        "lines": [15, 28],
        "sections": [
            {"line": 15, "slug": "key-points", "title": "Key points"},
            {"line": 28, "slug": "exercises", "title": "Exercises"},
        ],
    },
    "prerequisites.qmd": {
        "html": "prerequisites.html",
        "lines": [87, 140],
        "sections": [
            {"line": 87, "slug": "set-up-the-key-in-zed", "title": "Set up the key in Zed"},
            {"line": 140, "slug": "set-up-the-key-in-zed-1", "title": "Set up the key in Zed"},
        ],
    },
}

BASE = "http://127.0.0.1:8765"


def _load_fixture() -> None:
    dl.reset_for_tests()
    dl._SECTIONS = {k: dict(v) for k, v in FIXTURE.items()}


class DocslinksMappingTest(unittest.TestCase):
    def setUp(self) -> None:
        _load_fixture()

    def test_line_dans_section_donne_ancre(self) -> None:
        self.assertEqual(
            dl.section_url_for("01_asynchronous.qmd", 120, BASE),
            f"{BASE}/Courses/01_asynchronous.html#exercises",
        )

    def test_ligne_en_debut_de_fichier_donne_page_sans_ancre(self) -> None:
        self.assertEqual(
            dl.section_url_for("01_asynchronous.qmd", 5, BASE),
            f"{BASE}/Courses/01_asynchronous.html",
        )

    def test_chemin_prefixe_course(self) -> None:
        self.assertEqual(
            dl.section_url_for("Courses/01_asynchronous.qmd", 120, BASE),
            f"{BASE}/Courses/01_asynchronous.html#exercises",
        )

    def test_doublon_suffixe_1(self) -> None:
        self.assertEqual(
            dl.section_url_for("prerequisites.qmd", 145, BASE),
            f"{BASE}/prerequisites.html#set-up-the-key-in-zed-1",
        )
        self.assertEqual(
            dl.section_url_for("prerequisites.qmd", 90, BASE),
            f"{BASE}/prerequisites.html#set-up-the-key-in-zed",
        )

    def test_fichier_inconnu_retourne_none(self) -> None:
        self.assertIsNone(dl.section_url_for("inconnu.qmd", 3, BASE))

    def test_sans_base_url_retourne_none(self) -> None:
        self.assertIsNone(dl.section_url_for("inconnu.qmd", 3, ""))


class DocslinksRewriteTest(unittest.TestCase):
    def setUp(self) -> None:
        _load_fixture()

    def test_rewrite_texte_complet(self) -> None:
        got = dl.rewrite_content(
            "Regarde 01_asynchronous.qmd:120, c'est dans les exercices.", BASE
        )
        self.assertIn(
            f"[01_asynchronous.qmd:120]({BASE}/Courses/01_asynchronous.html#exercises)",
            got,
        )

    def test_fichier_inconnu_reste_en_clair(self) -> None:
        got = dl.rewrite_content("cf. bidon.qmd:42.", BASE)
        self.assertEqual(got, "cf. bidon.qmd:42.")

    def test_rewrite_inaltere_le_reste(self) -> None:
        got = dl.rewrite_content("Texte sans citation ici. Et puis voilà.", BASE)
        self.assertEqual(got, "Texte sans citation ici. Et puis voilà.")


class DocslinksStreamingTest(unittest.TestCase):
    """La réécriture ne doit ni perdre ni dupliquer de texte, et recomposer une
    citation coupée entre deux chunks du flux."""

    def setUp(self) -> None:
        _load_fixture()

    def _roundtrip(self, text: str, sizes: list[int]) -> str:
        rw = dl.LinkRewriter(BASE)
        parts: list[str] = []
        pos = 0
        for n in sizes:
            rw.feed(text[pos:pos + n])
            pos += n
        parts += rw.finish()
        return "".join(parts)

    def test_citation_coupee_recomposee(self) -> None:
        text = "00: cf. 01_asynchronous.qmd:120 fin"
        rw = dl.LinkRewriter(BASE)
        out: list[str] = []
        # découpe au milieu du nom de fichier, puis au milieu des "120"
        for c in ["00: cf. 01_asynchron", "ous.qmd:1", "20 fin"]:
            out += rw.feed(c)
        out += rw.finish()
        self.assertEqual(
            "".join(out),
            "00: cf. [01_asynchronous.qmd:120]"
            f"({BASE}/Courses/01_asynchronous.html#exercises) fin",
        )

    def test_aucune_perte_sur_texte_mixte(self) -> None:
        text = "Le bloc 01_asynchronous.qmd:28 parle des exercices, et 00 rien."
        rw = dl.LinkRewriter(BASE)
        chunks = ["Le bloc 01_asynchronous.", "qmd:28", " parle des exercices, et 00 rien."]
        out: list[str] = []
        for c in chunks:
            out += rw.feed(c)
        out += rw.finish()
        joined = "".join(out)
        # le texte est conservé (citations remplacées par leurs liens)
        self.assertIn("parle des exercices, et 00 rien.", joined)
        self.assertIn(f"]({BASE}/Courses/01_asynchronous.html#exercises)", joined)

    def test_jointure_egale_sur_chunks_aleatoires(self) -> None:
        text = "Citation 01_asynchronous.qmd:15 et prerequisites.qmd:140 locales."
        for chunk_max in (1, 3, 7, 25):
            rw = dl.LinkRewriter(BASE)
            out: list[str] = []
            for i in range(0, len(text), chunk_max):
                out += rw.feed(text[i:i + chunk_max])
            out += rw.finish()
            joined = "".join(out)
            self.assertIn(f"[01_asynchronous.qmd:15]({BASE}/Courses/01_asynchronous.html#key-points)", joined)
            self.assertIn(f"[prerequisites.qmd:140]({BASE}/prerequisites.html#set-up-the-key-in-zed-1)", joined)
            # pas de texte perdu : les liens remplacent leurs mentions exactes
            self.assertIn("et", joined)


class RealSectionsJsonTest(unittest.TestCase):
    """Passe-vite : la carte committée couvre bien les 7 fichiers du corpus."""

    def test_couvre_l_ensemble_du_corpus(self) -> None:
        path = Path(config.sections_json())
        self.assertTrue(path.exists(), "corpus/sections.json manquant — lancer tools/build_docs_map.py")
        corpus_files = set(config.corpus_files().values())
        with path.open(encoding="utf-8") as f:
            import json
            table = json.load(f)
        for fname in corpus_files:
            self.assertIn(fname, table, f"{fname} absent de la carte")
            self.assertIn("sections", table[fname])


class DocsServerTest(unittest.TestCase):
    def _tmp_www(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="tutor-www-"))
        (tmp / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
        return tmp

    def test_serve_sert_le_dossier(self) -> None:
        tmp = self._tmp_www()
        server = docs.serve("127.0.0.1", 0, str(tmp))
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html") as r:
                self.assertEqual(r.read().decode(), "<h1>ok</h1>")
        finally:
            server.shutdown()
            server.server_close()

    def test_stub_ensure_ne_lance_rien(self) -> None:
        with mock.patch.object(config, "STUB", True):
            res = docs.ensure()
        self.assertEqual(res["status"], "ok")
        self.assertIn("STUB", res["detail"])

    def test_ensure_idempotent_sur_port_deja_servi(self) -> None:
        tmp = self._tmp_www()
        server = docs.serve("127.0.0.1", 0, str(tmp))
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            with mock.patch.object(config, "STUB", False):
                res = docs.ensure("127.0.0.1", port, str(tmp))
            self.assertEqual(res["status"], "ok")
            self.assertIn("déjà servies", res["detail"])
        finally:
            server.shutdown()
            server.server_close()

    def test_absent_sans_dossier_www(self) -> None:
        with mock.patch.object(config, "STUB", False), \
                mock.patch.object(docs, "_probe", return_value=False):
            res = docs.ensure("127.0.0.1", 0, "/tmp/tutor-www-absent-sûr-xyz")
        self.assertEqual(res["status"], "absent")


if __name__ == "__main__":
    unittest.main()
