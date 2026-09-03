"""Tests du sanitiser de cellules solution et de son branchement sur les outils.

Couvre ``tutor.sanitize.strip_solution_cells`` (vidage en place, numéros de
ligne préservés, échafaudages ``eval: false`` intacts) et le branchement réel
dans ``tutor.tools`` : ``read_lines``/``grep_files`` ne renvoient jamais le
contenu d'une cellule ``tags: [solution]`` d'une source du corpus, tout en
laissant les fichiers du projet de l'élève intacts.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tutor import config, tools
from tutor.sanitize import (
    count_solution_cells,
    count_solution_divs,
    count_solution_headings,
    strip_solution_cells,
)

FENCE = "```"


def _tp_doc() -> str:
    """Petit sujet de TP synthétique : 1 cellule solution + 1 échafaudage."""
    return (
        "# Application 02_0\n"
        "\n"
        "## Exercice produit scalaire\n"
        "\n"
        f"{FENCE}{{python}}\n"
        "#| tags: [solution]\n"
        "import numpy as np\n"
        "np.dot(a, b)\n"
        f"{FENCE}\n"
        "\n"
        "## Exercice manuel\n"
        "\n"
        f"{FENCE}{{python}}\n"
        "#| eval: false\n"
        "# à compléter avec les …\n"
        "def produit(a, b):\n"
        "    ...\n"
        f"{FENCE}\n"
        "\n"
        "### Fin\n"
    )


class StripSolutionCellsTest(unittest.TestCase):
    def test_vide_le_corps_mais_preserve_les_lignes(self) -> None:
        src = _tp_doc()
        out = strip_solution_cells(src)
        # même nombre de lignes (numéros fichier:ligne valides)…
        self.assertEqual(out.count("\n"), src.count("\n"))
        # … mais plus aucun contenu de la cellule solution
        self.assertNotIn("np.dot", out)
        self.assertNotIn("import numpy", out)
        self.assertNotIn("tags: [solution]", out)
        # les lignes de la cellule deviennent vides (préservant la hauteur)
        i_fence = src.splitlines().index(f"{FENCE}{{python}}")
        for off in range(i_fence, i_fence + 5):
            self.assertEqual(out.splitlines()[off].strip(), "",
                             f"ligne {off} non vidée")

    def test_echafaudage_eval_false_intact(self) -> None:
        out = strip_solution_cells(_tp_doc())
        self.assertIn("def produit(a, b):", out)
        self.assertIn("#| eval: false", out)

    def test_titres_et_ancres_conserves(self) -> None:
        out = strip_solution_cells(_tp_doc())
        self.assertIn("## Exercice produit scalaire", out)
        self.assertIn("## Exercice manuel", out)
        self.assertIn("### Fin", out)
        self.assertEqual(out.splitlines().index("## Exercice manuel"),
                         _tp_doc().splitlines().index("## Exercice manuel"))

    def test_eval_false_et_solution_dans_la_meme_cellule(self) -> None:
        src = ("```{python}\n#| eval: false\n#| tags: [solution]\nprint(1)\n"
               "```\n")
        self.assertEqual(strip_solution_cells(src).strip(), "")

    def test_marqueur_typoe_bang(self) -> None:
        # ``#! tags: [solution]`` (typo observée dans 04_0) doit aussi être vidé.
        src = ("```{python}\n#! tags: [solution]\nfind_start_chunk(...)\n"
               "```\n")
        self.assertEqual(strip_solution_cells(src).strip(), "")

    def test_tags_liste_yaml_multi_ligne(self) -> None:
        src = ("```{python}\n#| tags:\n#|   - solution\nprint(1)\n```\n")
        self.assertEqual(strip_solution_cells(src).strip(), "")

    def test_cellule_non_solution_intacte(self) -> None:
        src = ("```{python}\n#| echo: false\nprint(42)\n```\n")
        self.assertEqual(strip_solution_cells(src), src)

    def test_document_sans_fence_intact(self) -> None:
        src = "# Titre\n\ntexte sans cellule.\n"
        self.assertEqual(strip_solution_cells(src), src)

    def test_count_solution_cells(self) -> None:
        self.assertEqual(count_solution_cells(_tp_doc()), 1)
        self.assertEqual(count_solution_cells("pas de cellule\n"), 0)

    def test_div_solution_vide_son_texte(self) -> None:
        src = (
            "## Exercice\n\n"
            ":::solution\n"
            "La réponse est un raisonnement asymptotique.\n"
            ":::\n"
            "\n"
            "Suivant.\n"
        )
        out = strip_solution_cells(src)
        self.assertNotIn("raisonnement", out)
        # contenu de la cellule vidé, structure de lignes conservée
        self.assertEqual(out.count("\n"), src.count("\n"))
        # l'échafaudage et le texte extérieur au div restent
        self.assertIn("## Exercice", out)
        self.assertIn("Suivant.", out)

    def test_div_solution_avec_accolades_et_quatre_colons(self) -> None:
        src = (
            ":::{.solution}\n"
            "As we already lock the increment.\n"
            ":::\n"
            "::::solution\n"
            "Delaying the sum could be interesting.\n"
            "::::\n"
        )
        out = strip_solution_cells(src)
        self.assertNotIn("increment", out)
        self.assertNotIn("interesting", out)

    def test_div_callout_non_solution_intact(self) -> None:
        src = (
            "::: {.callout-important}\n"
            "Never, ever put your API key in the code.\n"
            ":::\n"
        )
        self.assertEqual(strip_solution_cells(src), src)

    def test_div_solution_contenant_une_fence_markdown(self) -> None:
        src = (
            ":::solution\n"
            "```markdown\n"
            "# Logging each answer\n"
            "```\n"
            ":::\n"
        )
        out = strip_solution_cells(src)
        self.assertNotIn("Logging", out)

    def test_titre_solution_vide(self) -> None:
        src = "## Solution for main process function\n\ncorps\n"
        out = strip_solution_cells(src)
        self.assertEqual(out.splitlines()[0].strip(), "")
        self.assertIn("corps", out)

    def test_titre_commencant_par_solutions_pluriel_vide(self) -> None:
        src = "### Solutions possibles\n\ncorps\n"
        out = strip_solution_cells(src)
        self.assertEqual(out.splitlines()[0].strip(), "")

    def test_titre_d_exercice_contenant_le_mot_solution_garde(self) -> None:
        # « 9. Comparing Solutions » / « Pure asyncio solution » sont des titres
        # d'exercices (échafaudages), pas des réponses : ils doivent survivre.
        src = (
            "### **9. Comparing Solutions**\n\nExercice à faire.\n"
            "## Pure `asyncio` solution\n\nÉchafaudage `eval: false`.\n"
        )
        self.assertEqual(strip_solution_cells(src), src)

    def test_compteurs_divs_et_titres(self) -> None:
        src = _tp_doc() + (
            "\n:::solution\n prose \n:::\n"
            "\n## Solution to exercise X\n"
        )
        self.assertEqual(count_solution_divs(src), 1)
        self.assertEqual(count_solution_headings(src), 1)
        self.assertEqual(count_solution_divs("aucun\n"), 0)
        self.assertEqual(count_solution_headings("aucun\n"), 0)


class ToolsSanitizationTest(unittest.TestCase):
    """Le branchement réel : les outils ne renvoient jamais de solution.

    ``_read_lines`` n'assainit que les ``.qmd`` sous ``config.corpus_root()`` ;
    un fichier du même nom hors corpus (projet élève) reste brut.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "Courses" / "Applications"
        self.corpus.mkdir(parents=True)
        (self.corpus / "02_0_Numpy_Workout.qmd").write_text(
            _tp_doc(), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_read_lines_corpus_sans_solution(self) -> None:
        with mock.patch.object(config, "corpus_root",
                               return_value=self.root / "Courses"):
            shown = tools.read_lines(
                str(self.corpus / "02_0_Numpy_Workout.qmd"), 1, 25)
        text = "\n".join(shown)
        self.assertNotIn("np.dot", text)
        self.assertNotIn("import numpy", text)
        # les lignes sont bien « présentes » mais vides
        self.assertIn("02_0_Numpy_Workout.qmd:5: ", shown[4])

    def test_grep_files_corpus_sans_solution(self) -> None:
        with mock.patch.object(config, "corpus_root",
                               return_value=self.root / "Courses"):
            total, shown = tools.grep_files(
                [str(self.corpus / "02_0_Numpy_Workout.qmd")], "np\\.dot|numpy")
        self.assertEqual((total, shown), (0, []))

    def test_fichier_hors_corpus_non_assaini(self) -> None:
        # même nom, mais sous la racine projet → brut (jamais de sanitisation
        # du travail de l'élève)
        proj = self.root / "projet"
        proj.mkdir()
        (proj / "entrainement.qmd").write_text(
            "cellule bidon\n```{python}\n#| tags: [solution]\nprint('x')\n```\n",
            encoding="utf-8")
        with mock.patch.object(config, "corpus_root",
                               return_value=self.root / "Courses"):
            shown = tools.read_lines(str(proj / "entrainement.qmd"), 1, 5)
        self.assertIn("print('x')", "\n".join(shown))


if __name__ == "__main__":
    unittest.main()
