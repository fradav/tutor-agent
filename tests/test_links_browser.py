"""Tests — liens matières renvoyés par un tour tuteur, cliquables et visibles.

Simule une session étudiant **sans LLM réel** : le backend est mocké
(``TutorEngine.complete_model_stream``) pour produire un contenu avec des
liens markdown natifs (le modèle les produit directement grâce à la section
« Références » du prompt système).

Un vrai serveur docs (book du jumeau ``www/`` + miroir Python temporaire) est
lancé sur un port éphémère — exactement ce que voit l'étudiant.

On vérifie que TOUS les liens du contenu renvoyé sont effectivement cliquables
et visibles dans un navigateur : HTTP 200 sur chacun et ancre présente dans le
HTML servi.
"""

from __future__ import annotations

import re
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from tutor import config, docs, engine
from tutor.engine import TutorEngine

# Réponse mockée du modèle : liens markdown natifs avec la base URL du book.
# Le modèle produit ces liens grâce à la section « Références » injectée dans
# le prompt système par ``config.build_system()``.
_SAMPLE_NATIVE_LINKS = (
    "Pour l'asynchrone, lis le cours : "
    "[Programmation asynchrone](http://127.0.0.1:8765/Courses/03_Asynchronous.html) "
    "et le TP : "
    "[TP Asynchrone](http://127.0.0.1:8765/Courses/Applications/03_0_Asynchronous.html). "
    "La doc Python : "
    "[python:asyncio](https://docs.python.org/3/library/asyncio.html)."
)

# Réponse avec des liens imbriqués (bug signalé) : le modèle aurait dû ne pas
# le faire grâce aux instructions, mais on vérifie que le moteur ne casse pas
# ce cas de figure.
_SAMPLE_NESTED_BUG = (
    "Lis [Cours]([03_Asynchronous.qmd](http://127.0.0.1:8765/Courses/03_Asynchronous.html)) "
    "pour comprendre."
)


def _fake_complete(*chunks: str):
    """Générateur de ``complete_model_stream`` mocké : rend les chunks donnés
    en contenu visible, puis ``finish="stop"`` + usage (aucun tool_call)."""

    def _gen(self, messages):
        for c in chunks:
            yield (None, c, None, None, None)
        yield (None, None, "stop", {"total_tokens": 10}, None)

    return _gen


class StudentLinksClickableTest(unittest.TestCase):
    """Un tour tuteur (backend mocké) renvoie des liens cliquables vers le book
    (chapitres + TP) et la doc Python, servis par un vrai serveur docs."""

    # URLs attendues pour les liens natifs du test.
    COURSE_URL = "Courses/03_Asynchronous.html"
    APP_URL = "Courses/Applications/03_0_Asynchronous.html"
    PY_URL = "py/library/asyncio.html"

    def setUp(self) -> None:
        www = config.www_dir()
        self.assertTrue(Path(www).is_dir(),
                        f"www du jumeau absent : {www} — impossible de tester les liens")
        # Miroir Python temporaire (le jumeau n'en embarque pas) : servi sous
        # ``/py/``, c'est la cible des citations ``python:asyncio`` hors-ligne.
        self._py_tmp = tempfile.TemporaryDirectory(prefix="tutor-pydoc-")
        py = Path(self._py_tmp.name)
        (py / "library").mkdir(parents=True)
        (py / "library" / "asyncio.html").write_text(
            "<html><body><h1 id=\"asyncio\">asyncio</h1></body></html>",
            encoding="utf-8",
        )
        self.server = docs.serve("127.0.0.1", 0, www, str(py))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"
        docs._set_effective_base(self.base)

    def tearDown(self) -> None:
        docs._set_effective_base()
        self.server.shutdown()
        self.server.server_close()
        self._py_tmp.cleanup()

    def _run_turn_with(self, chunks: tuple[str, ...]) -> dict:
        """Un tour complet avec backend mocké (STUB désactivé) — sortie non
        streamée ``turn["content"]`` passée par la réécriture des citations."""
        state = engine.initial_state(
            model="qwen3.5-4B", session_id="t-liens", label="liens", cwd="")
        turn: dict = {}
        with tempfile.TemporaryDirectory(prefix="tutor-sessions-") as sess, \
                mock.patch.object(config, "STUB", False), \
                mock.patch.object(config, "py_dir",
                                  return_value=str(Path(self._py_tmp.name))), \
                mock.patch.object(config, "sessions_dir",
                                  return_value=Path(sess)), \
                mock.patch.object(TutorEngine, "complete_model_stream",
                                  new=_fake_complete(*chunks)):
            turn = TutorEngine(state).run_turn(
                "Que dois-je lire pour comprendre l'asynchrone ?")
        return turn

    # -- helpers -----------------------------------------------------------

    def _links_in(self, content: str) -> list[str]:
        """Tous les liens markdown du contenu, résolus contre la base du test."""
        hrefs = re.findall(r"\[[^\]]*\]\(([^)]+)\)", content)
        return [h if h.startswith(("http://", "https://"))
                else f"{self.base}/{h.lstrip('/')}" for h in hrefs]

    def _assert_clickable(self, url: str) -> bytes:
        """Le lien répond HTTP 200 (cliquable / visible dans un navigateur)."""
        with urllib.request.urlopen(url, timeout=10) as r:
            self.assertEqual(r.status, 200, f"lien mort : {url}")
            return r.read()

    def _assert_anchor_present(self, url: str, body: bytes) -> None:
        """L'ancre du fragment existe dans le HTML servi (navigation utile)."""
        path, _, fragment = url.partition("#")
        if fragment:
            self.assertIn(
                f'id="{fragment}"', body.decode("utf-8", errors="replace"),
                f"ancre manquante sur {path}#{fragment}",
            )

    # -- tests -------------------------------------------------------------

    def test_liens_natifs_cours_tp_python_cliquables(self) -> None:
        """Le modèle produit des liens markdown natifs vers le book (chapitres +
        TP) et la doc Python — tous cliquables et visibles."""
        # Adapter le contenu pour utiliser le port dynamique du test
        content = _SAMPLE_NATIVE_LINKS.replace(
            "http://127.0.0.1:8765", self.base
        )
        turn = self._run_turn_with((content,))
        result = turn["content"]
        # Les liens vers le book (cours + TP) ont été produits par le modèle.
        self.assertIn(f"{self.base}/{self.COURSE_URL}", result)
        self.assertIn(f"{self.base}/{self.APP_URL}", result)
        # La doc Python pointe vers l'URL en ligne (le modèle utilise ce défaut)
        self.assertIn("https://docs.python.org/3/library/asyncio.html", result)
        links = self._links_in(result)
        self.assertGreaterEqual(len(links), 3)
        for url in links:
            body = self._assert_clickable(url)
            self._assert_anchor_present(url, body)

    def test_liens_natifs_sont_passes_tel_quels(self) -> None:
        """Les liens natifs du modèle passent à travers l'engine sans être
        réécrits ni cassés — pas de liens imbriqués ajoutés."""
        content = f"[Cours]({self.base}/Courses/03_Asynchronous.html)"
        turn = self._run_turn_with((content,))
        result = turn["content"]
        # Le lien original doit être présent tel quel
        self.assertIn(f"[Cours]({self.base}/Courses/03_Asynchronous.html)", result)
        # Pas de réécriture qui ajouterait des backticks ou des liens imbriqués
        self.assertNotIn("``", result)
        # Un seul lien markdown (pas de double emboîtement)
        hrefs = re.findall(r"\[[^\]]*\]\([^)]+\)", result)
        self.assertEqual(len(hrefs), 1,
                         f"nombre de liens attendu: 1, obtenu: {len(hrefs)}")

    def test_fallback_rewrite_sur_resultat_outil(self) -> None:
        """Le fallback ``rewrite_content()`` sur les résultats d'outils convertit
        ``fichier:ligne`` en lien de page (le modèle ne connaît pas les ancres)."""
        from tutor.docslinks import rewrite_content
        tool_result = "Ligne trouvée dans 03_Asynchronous.qmd:63"
        got = rewrite_content(tool_result, self.base)
        self.assertIn(
            f"[03_Asynchronous.qmd:63]({self.base}/Courses/03_Asynchronous.html)",
            got,
        )
        # Pas d'ancre (fallback page-only)
        self.assertNotIn("#", got.split("03_Asynchronous.html")[1])

    def test_python_ref_rewrite_sur_resultat_outil(self) -> None:
        """Le fallback convertit aussi ``python:<ref>`` en lien doc Python."""
        from tutor.docslinks import rewrite_content
        tool_result = "Module documenté : python:asyncio"
        got = rewrite_content(tool_result, self.base)
        self.assertIn(
            "[python:asyncio](https://docs.python.org/3/library/asyncio.html)",
            got,
        )


if __name__ == "__main__":
    unittest.main()
