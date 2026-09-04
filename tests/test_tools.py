"""Tests de la doc cliquable : carte ligne→section, réécriture streaming,
serveur statique local. Indépendants du réseau externe (localhost, port 0).

NB : la suite est lancée avec ``TUTOR_STUB=1`` (replies déterministes) ; la
carte ``corpus/sections.json`` doit exister (committée, générée par
``tools/build_docs_map.py``). Les tests logiques utilisent un petit fixture
pour rester déterministes.
"""
from __future__ import annotations

import re
import tempfile
import threading
import unittest
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from unittest import mock

from tutor import config, docs, docslinks as dl
from tutor import tools

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
    "03_0_Asynchronous.qmd": {
        "html": "Courses/Applications/03_0_Asynchronous.html",
        "lines": [63],
        "sections": [
            {"line": 63, "slug": "exemples", "title": "Exemples"},
        ],
    },
}

BASE = "http://127.0.0.1:8765"


def _load_fixture() -> None:
    dl.reset_for_tests()
    dl._SECTIONS = {k: dict(v) for k, v in FIXTURE.items()}


def _write_tree(base: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


class DocslinksMappingTest(unittest.TestCase):
    """Tests de ``section_url_for()`` : retourne un lien de page (sans ancre).

    Le modèle ne connaît pas les ancres HTML ; le fallback ne les résout pas.
    """
    def setUp(self) -> None:
        _load_fixture()

    def test_fichier_connu_donne_lien_page(self) -> None:
        self.assertEqual(
            dl.section_url_for("01_asynchronous.qmd", 120, BASE),
            f"{BASE}/Courses/01_asynchronous.html",
        )

    def test_ligne_en_debut_donne_page_sans_ancre(self) -> None:
        self.assertEqual(
            dl.section_url_for("01_asynchronous.qmd", 5, BASE),
            f"{BASE}/Courses/01_asynchronous.html",
        )

    def test_chemin_prefixe_course(self) -> None:
        self.assertEqual(
            dl.section_url_for("Courses/01_asynchronous.qmd", 120, BASE),
            f"{BASE}/Courses/01_asynchronous.html",
        )

    def test_doublon_suffixe_1(self) -> None:
        # pas d'ancre (fallback page-only)
        self.assertEqual(
            dl.section_url_for("prerequisites.qmd", 145, BASE),
            f"{BASE}/prerequisites.html",
        )

    def test_fichier_inconnu_retourne_none(self) -> None:
        self.assertIsNone(dl.section_url_for("inconnu.qmd", 3, BASE))

    def test_sans_base_url_retourne_none(self) -> None:
        self.assertIsNone(dl.section_url_for("inconnu.qmd", 3, ""))


class DocslinksRewriteTest(unittest.TestCase):
    """Fallback de réécriture : ``fichier:ligne`` → lien de page (sans ancre).

    Le modèle produit des liens natifs ; ce module est le filet de sécurité
    sur les résultats d'outils. Pas de résolution d'ancre (le modèle ne la
    connaît pas).
    """
    def setUp(self) -> None:
        _load_fixture()

    def test_rewrite_texte_complet(self) -> None:
        got = dl.rewrite_content(
            "Regarde 01_asynchronous.qmd:120, c'est dans les exercices.", BASE
        )
        self.assertIn(
            f"[01_asynchronous.qmd:120]({BASE}/Courses/01_asynchronous.html)",
            got,
        )
        # pas d'ancre (fallback page-only)
        self.assertNotIn("#", got.split("01_asynchronous.html")[1])

    def test_fichier_inconnu_reste_en_clair(self) -> None:
        got = dl.rewrite_content("cf. bidon.qmd:42.", BASE)
        self.assertEqual(got, "cf. bidon.qmd:42.")

    def test_fichier_seul_donne_lien_page(self) -> None:
        # label nu → lien de page, sans ancre ; les backticks retirés
        got = dl.rewrite_content(
            "| 3 | `01_asynchronous.qmd` | - Write a program… |", BASE
        )
        self.assertEqual(
            got,
            f"| 3 | [01_asynchronous.qmd]({BASE}/Courses/01_asynchronous.html)"
            " | - Write a program… |",
        )

    def test_fichier_seul_suivi_d_un_point_conserve_le_point(self) -> None:
        got = dl.rewrite_content("cf. 01_asynchronous.qmd.", BASE)
        self.assertEqual(
            got,
            f"cf. [01_asynchronous.qmd]({BASE}/Courses/01_asynchronous.html).",
        )

    def test_fichier_seul_inconnu_reste_en_clair(self) -> None:
        got = dl.rewrite_content("Regarde bidon.qmd dans tes notes.", BASE)
        self.assertEqual(got, "Regarde bidon.qmd dans tes notes.")

    def test_fichier_seul_non_touche_si_suivi_d_un_numero(self) -> None:
        got = dl.rewrite_content("cf. 01_asynchronous.qmd:28.", BASE)
        self.assertIn(
            f"[01_asynchronous.qmd:28]"
            f"({BASE}/Courses/01_asynchronous.html)",
            got,
        )
        self.assertNotIn("01_asynchronous.html)`", got)

    def test_rewrite_inaltere_le_reste(self) -> None:
        got = dl.rewrite_content("Texte sans citation ici. Et puis voilà.", BASE)
        self.assertEqual(got, "Texte sans citation ici. Et puis voilà.")

    def test_citation_backtick_enleve(self) -> None:
        got = dl.rewrite_content("Voir `01_asynchronous.qmd:120` ici.", BASE)
        self.assertEqual(
            got,
            f"Voir [01_asynchronous.qmd:120]"
            f"({BASE}/Courses/01_asynchronous.html) ici.",
        )

    def test_fichier_seul_backtick_enleve(self) -> None:
        got = dl.rewrite_content("cf. `01_asynchronous.qmd` quand tu veux.", BASE)
        self.assertEqual(
            got,
            f"cf. [01_asynchronous.qmd]"
            f"({BASE}/Courses/01_asynchronous.html) quand tu veux.",
        )

    def test_chemin_prefixe_citation(self) -> None:
        got = dl.rewrite_content(
            "Voir Applications/03_0_Asynchronous.qmd:63 ici.", BASE
        )
        self.assertIn(
            f"[Applications/03_0_Asynchronous.qmd:63]"
            f"({BASE}/Courses/Applications/03_0_Asynchronous.html)",
            got,
        )
        self.assertNotIn("Applications/[03_0_Asynchronous", got)

    def test_chemin_prefixe_backtick_enleve(self) -> None:
        got = dl.rewrite_content(
            "cf. `Applications/03_0_Asynchronous.qmd` quand tu veux.", BASE
        )
        self.assertEqual(
            got,
            f"cf. [Applications/03_0_Asynchronous.qmd]"
            f"({BASE}/Courses/Applications/03_0_Asynchronous.html) quand tu veux.",
        )

    def test_python_ref_backtick_enleve(self) -> None:
        got = dl.rewrite_content("Utilise `python:asyncio` ici.", BASE)
        self.assertIn(
            "[python:asyncio](https://docs.python.org/3/library/asyncio.html)",
            got,
        )
        self.assertNotIn("`[python:asyncio]", got)

    def test_span_code_ordinaire_conserve(self) -> None:
        text = "| `print(x)` | `x01_asynchronous.qmd.y` |"
        self.assertEqual(dl.rewrite_content(text, BASE), text)
class DocslinksRewriteNoStreamingTest(unittest.TestCase):
    """Tests de réécriture sur texte complet (plus de streaming depuis la
    refonte : le modèle produit des liens natifs, pas de réécriture engine).

    Ces tests couvrent le fallback ``rewrite_content()`` sur les résultats
    d'outils : conversion ``fichier:ligne`` → lien de page, ``python:<ref>``
    → lien doc Python.
    """
    def setUp(self) -> None:
        _load_fixture()

    def test_rewrite_complete_text(self) -> None:
        """La réécriture sur texte complet fonctionne (utilisé sur les résultats
        d'outils dans engine.py)."""
        text = "Regarde 01_asynchronous.qmd:120 et python:asyncio."
        got = dl.rewrite_content(text, BASE)
        self.assertIn(
            f"[01_asynchronous.qmd:120]({BASE}/Courses/01_asynchronous.html)",
            got,
        )
        self.assertIn(
            "[python:asyncio](https://docs.python.org/3/library/asyncio.html)",
            got,
        )

    def test_no_anchor_in_fallback(self) -> None:
        """Le fallback ne résout pas d'ancre : lien de page uniquement."""
        text = "01_asynchronous.qmd:120"
        got = dl.rewrite_content(text, BASE)
        self.assertIn("01_asynchronous.html)", got)
        # pas d'ancre (le modèle ne la connaît pas)
        self.assertNotIn("#exercises", got)

    def test_python_ref_in_text(self) -> None:
        text = "La doc de python:asyncio est utile."
        got = dl.rewrite_content(text, BASE)
        self.assertIn(
            "[python:asyncio](https://docs.python.org/3/library/asyncio.html)",
            got,
        )
        self.assertNotIn("python:asyncio", got.replace(
            "[python:asyncio](https://docs.python.org/3/library/asyncio.html)", ""
        ))


class RealSectionsJsonTest(unittest.TestCase):
    """Passe-vite : la carte committée couvre bien l'ensemble du corpus."""

    def test_couvre_l_ensemble_du_corpus(self) -> None:
        path = Path(config.sections_json())
        self.assertTrue(path.exists(), "corpus/sections.json manquant — lancer tools/build_docs_map.py")
        corpus_files = set(config.corpus_files().values())
        with path.open(encoding="utf-8") as f:
            import json
            table = json.load(f)
        # Les valeurs ``corpus_files`` portent parfois un sous-chemin
        # (``Applications/<stem>.qmd``) mais la carte est indexée par basename
        # (c'est ainsi que ``docslinks.section_url_for`` la lit).
        for fname in corpus_files:
            key = Path(fname).name
            self.assertIn(key, table, f"{fname} absent de la carte")
            self.assertIn("sections", table[key])


class FindPathUnitTest(unittest.TestCase):
    """find_paths : clé courte / glob / sous-chaîne sur le corpus, glob projet,
    pagination, refus des patterns absolus et montées .. """

    CORPUS = {"01": "01_asynchronous.qmd",
              "02": "02_threads.qmd",
              "03": "03_sockets.qmd"}

    def test_corpus_short_key(self) -> None:
        got, total, more = tools.find_paths("01", corpus=self.CORPUS)
        self.assertEqual(got, ["01_asynchronous.qmd"])
        self.assertEqual(total, 1)
        self.assertFalse(more)

    def test_corpus_glob(self) -> None:
        got, total, more = tools.find_paths("*.qmd", corpus=self.CORPUS)
        self.assertEqual(total, 3)
        self.assertFalse(more)
        self.assertIn("01_asynchronous.qmd", got)

    def test_corpus_substring(self) -> None:
        got, total, _ = tools.find_paths("sockets", corpus=self.CORPUS)
        self.assertEqual(got, ["03_sockets.qmd"])
        self.assertEqual(total, 1)

    def test_pagination(self) -> None:
        got, total, more = tools.find_paths(
            "*.qmd", corpus=self.CORPUS, max_results=2)
        self.assertEqual(len(got), 2)
        self.assertEqual(total, 3)
        self.assertTrue(more)
        got2, _, more2 = tools.find_paths(
            "*.qmd", corpus=self.CORPUS, max_results=2, offset=2)
        self.assertEqual(len(got2), 1)
        self.assertFalse(more2)

    def test_absolute_pattern_refused(self) -> None:
        got, total, more = tools.find_paths("/etc/passwd", corpus=self.CORPUS)
        self.assertEqual((got, total, more), ([], 0, False))

    def test_dotdot_climb_refused(self) -> None:
        got, total, more = tools.find_paths("../secret", corpus=self.CORPUS)
        self.assertEqual((got, total, more), ([], 0, False))

    def test_project_glob_noise_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_tree(base, {
                "src/a.py": "x = 1\n",
                ".venv/x.py": "y = 2\n",
                "notes.txt": "t\n",
            })
            got, total, _ = tools.find_paths(
                "**/*.py", project_dir=str(base))
            self.assertEqual(got, ["src/a.py"])
            self.assertEqual(total, 1)

    def test_empty_pattern_yields_star(self) -> None:
        got, total, _ = tools.find_paths("", corpus=self.CORPUS)
        self.assertEqual(total, 3)


class PySyntaxErrorsUnitTest(unittest.TestCase):
    """py_syntax_errors : fichier valide → [], invalide → fichier:ligne:col: msg,
    binaire/NUL, fichier absent → [] (jamais de traceback)."""

    def test_good_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ok.py"
            p.write_text("def f():\n    return 1\n", encoding="utf-8")
            self.assertEqual(tools.py_syntax_errors(str(p)), [])

    def test_bad_file_reports_line_column_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.py"
            p.write_text("def oops(:\n", encoding="utf-8")
            errs = tools.py_syntax_errors(str(p))
            self.assertEqual(len(errs), 1)
            self.assertTrue(errs[0].startswith("bad.py:1:"), errs[0])
            self.assertIn(":", errs[0].split("bad.py:", 1)[1])

    def test_nul_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "nul.py"
            p.write_bytes(b"x = 1\x00print(2)\n")
            errs = tools.py_syntax_errors(str(p))
            self.assertEqual(len(errs), 1)
            self.assertIn("NUL bytes", errs[0])

    def test_missing_file_empty(self) -> None:
        self.assertEqual(
            tools.py_syntax_errors("/nonexistent/never.py"), [])


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

    def test_marker_identifie_nos_serveurs(self) -> None:
        tmp = self._tmp_www()
        server = docs.serve("127.0.0.1", 0, str(tmp))
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            with mock.patch.object(config, "STUB", False):
                self.assertTrue(docs._serves_marker("127.0.0.1", port))
                self.assertEqual(
                    docs._get_status("127.0.0.1", port, "/__tutor_docs__"), 200)
        finally:
            server.shutdown()
            server.server_close()

    def test_ensure_bascule_sur_autre_port_si_port_squatte(self) -> None:
        tmp = self._tmp_www()
        # Un « squatteur » étranger tient un port, sans notre marqueur.
        squatter = ThreadingHTTPServer(("127.0.0.1", 0), SimpleHTTPRequestHandler)
        squat = squatter.server_address[1]
        threading.Thread(target=squatter.serve_forever, daemon=True).start()
        started: list[ThreadingHTTPServer] = []
        orig_bind = docs._bind_server

        def _spy(host: str, port: int, www: str, py: str):
            server, used = orig_bind(host, port, www, py)
            self.assertNotEqual(used, squat)
            started.append(server)
            return server, used

        try:
            with mock.patch.object(config, "STUB", False), \
                    mock.patch.object(docs, "_bind_server", side_effect=_spy):
                res = docs.ensure("127.0.0.1", squat, str(tmp))
            self.assertEqual(res["status"], "ok")
            self.assertIn("un autre processus", res["detail"])
            m = re.search(r"http://127\.0\.0\.1:(\d+)", res["detail"])
            self.assertIsNotNone(m)
            want = f"http://127.0.0.1:{m.group(1)}"
            self.assertEqual(docs.effective_base_url(), want)
            with urllib.request.urlopen(f"{want}/index.html") as r:
                self.assertEqual(r.read().decode(), "<h1>ok</h1>")
            # Une seconde session réadopte le port déjà servi (aucun nouveau
            # serveur) : effective base inchangée, pas de bind supplémentaire.
            with mock.patch.object(config, "STUB", False), \
                    mock.patch.object(docs, "_bind_server", side_effect=_spy):
                res2 = docs.ensure("127.0.0.1", squat, str(tmp))
            self.assertEqual(docs.effective_base_url(), want)
            self.assertIn("un autre processus", res2["detail"])
            self.assertEqual(len(started), 1)
        finally:
            docs._set_effective_base()
            squatter.shutdown()
            squatter.server_close()
            for s in started:
                s.shutdown()
                s.server_close()

    def test_ensure_reste_idempotent_si_notre_serveur_a_bonne_racine(self) -> None:
        # Même port, notre serveur, bonne racine → pas de relance, base = config.
        tmp = self._tmp_www()
        server = docs.serve("127.0.0.1", 0, str(tmp))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(config, "STUB", False):
                res = docs.ensure("127.0.0.1", port, str(tmp))
            self.assertEqual(res["status"], "ok")
            self.assertIn("déjà servies", res["detail"])
            self.assertEqual(docs.effective_base_url(), config.docs_base_url())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
