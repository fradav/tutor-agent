"""Tests — liens matières renvoyés par un tour tuteur, cliquables et servis.

Simule une session étudiant **sans LLM réel** : le mode STUB est désactivé
(``config.STUB`` patché à False) et le backend est mocké
(``TutorEngine.complete_model_stream``) pour produire un contenu qui cite le
corpus — un chapitre (``03_Asynchronous.qmd:63``), un TP
(``Applications/03_0_Asynchronous.qmd:51``) et la doc Python (``python:asyncio``).

Le vrai pipeline de réécriture (``tutor.docslinks``) transforme ces citations en
liens markdown vers un **vrai** serveur docs (book du jumeau ``www/`` + miroir
Python temporaire) lancé sur un port éphémère — exactement ce que voit l'étudiant.

On vérifie que TOUS les liens du contenu renvoyé sont effectivement cliquables
et visibles dans un navigateur : HTTP 200 sur chacun et ancre présente dans le
HTML servi. Un second test découpe une citation entre deux chunks pour exercer
la recomposition streaming du ``LinkRewriter``.
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

# Échantillon de réponse tuteur : citations chapitre + TP + doc Python.
_SAMPLE = (
    "Pour l'asynchrone, lisez d'abord le chapitre (cf. 03_Asynchronous.qmd:63), "
    "puis faites le TP Applications/03_0_Asynchronous.qmd:51. "
    "La boucle d'événements est documentée dans python:asyncio."
)

# Réponse produite réellement par qwen3.5-4B (session Playground-table) : les
# références sont dans des backticks, en liste, avec un label gras — c'est le
# cas où l'utilisateur signalait « les liens ne sont pas là ». La réécriture doit
# retirer les backticks et lier chaque ``*.qmd:ligne`` (le label ``**…**`` n'est
# pas un lien à toucher) ; ``python:asyncio`` est ajouté pour couvrir aussi la
# doc Python dans ce format réel.
_REAL_QWEN35 = (
    "## Références cours\n\n"
    "- **`asyncio.gather`** : `03_0_Asynchronous.qmd:830`\n"
    "- **Exemple de code** : `03_0_Asynchronous.qmd:848`\n"
    "- **Exemple avancé** : `03_0_Asynchronous.qmd:1007`\n\n"
    "**Question** : Comment appeler `asyncio.gather()` avec deux coroutines "
    "pour les exécuter en parallèle ?\n\n"
    "Doc Python : `python:asyncio`\n"
)

# URL attendue pour les lignes 830/848/1007 du TP signalé par l'utilisateur.
_REAL_APP_URL = "Courses/Applications/03_0_Asynchronous.html#asynchronous-widgets"


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

    # Les ancres visées par les citations du test, telles que résolues par
    # ``docslinks.section_url_for`` sur la carte committée (sections.json).
    COURSE_URL = "Courses/03_Asynchronous.html#io-bound-vs.-cpu-bound"
    APP_URL = "Courses/Applications/03_0_Asynchronous.html#asyncio-queues"
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

    def test_liens_cours_tp_python_cliquables(self) -> None:
        turn = self._run_turn_with((_SAMPLE,))
        content = turn["content"]
        # Les trois liens attendus ont été produits par la réécriture.
        for expected in (self.COURSE_URL, self.APP_URL, self.PY_URL):
            self.assertIn(
                f"{self.base}/{expected}", content,
                f"lien manquant dans le contenu : {expected}",
            )
        links = self._links_in(content)
        self.assertGreaterEqual(len(links), 3)
        for url in links:
            body = self._assert_clickable(url)
            self._assert_anchor_present(url, body)

    def test_citation_decoupee_entre_chunks_recomposee(self) -> None:
        """Une citation coupée entre deux chunks (streaming réel) donne un lien
        complet et cliquable — exercice du buffer du ``LinkRewriter``."""
        chunks = (
            "Lis d'abord le chapitre 03_Asynchro",
            "nous.qmd:63 puis le TP Applications/03_0_Asynchro",
            "nous.qmd:51. La boucle : python:asynci",
            "o.",
        )
        turn = self._run_turn_with(chunks)
        content = turn["content"]
        for expected in (self.COURSE_URL, self.APP_URL, self.PY_URL):
            self.assertIn(f"{self.base}/{expected}", content,
                          f"lien manquant (chunké) : {expected}")
        for url in self._links_in(content):
            self._assert_clickable(url)
        # Une recomposition ratée laisserait les fragments ``03_Asynchro`` /
        # ``nous.qmd:63`` en texte brut (aucun lien) → l'assertion de présence
        # ci-dessus aurait échoué : le streaming a donc bien recomposé la citation.

    def test_reponse_reelle_qwen35_backticks_liste(self) -> None:
        """Le format réel de qwen3.5-4B (réf. dans des backticks, en liste, avec
        un label gras) donne des liens cliquables — la sortie signalée
        « les liens ne sont pas là ». AUCUNE citation ``*.qmd:ligne`` ne doit
        subsister en texte brut, et Chaque lien doit répondre HTTP 200 avec une
        ancre présente (cliquable et visible dans un navigateur)."""
        turn = self._run_turn_with((_REAL_QWEN35,))
        content = turn["content"]
        # Aucune des citations backtickées du transcript réel ne reste en l'état
        # (c'était précisément le symptôme « les liens ne sont pas là »).
        for raw in ("`03_0_Asynchronous.qmd:830`", "`03_0_Asynchronous.qmd:848`",
                    "`03_0_Asynchronous.qmd:1007`", "`python:asyncio`"):
            self.assertNotIn(raw, content,
                             f"citation non réécrite dans le contenu : {raw}")
        # Les trois références du TP pointent vers la même ancre réelle ; la doc
        # Python est servie par le miroir local temporaire.
        for expected in (self.PY_URL,):
            self.assertIn(f"{self.base}/{expected}", content,
                          f"lien manquant (format réel) : {expected}")
        app_url = f"{self.base}/{_REAL_APP_URL}"
        self.assertGreaterEqual(content.count(app_url), 3,
                                "les 3 références du TP doivent être liées")
        links = self._links_in(content)
        self.assertGreaterEqual(len(links), 4)
        for url in links:
            body = self._assert_clickable(url)
            self._assert_anchor_present(url, body)


if __name__ == "__main__":
    unittest.main()
