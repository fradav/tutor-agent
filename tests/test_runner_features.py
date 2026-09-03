"""Tests of the refactored runner features (Python ACP, generic + twin split).

Covers the parts that the pinned FR tests do not exercise:
- AGENTS convention: ``<cwd>/AGENTS.<model>.md`` over ``<cwd>/AGENTS.md``
  (``config.build_system`` + ``engine.initial_state``);
- read-only access to the **open project** (session cwd): project-relative
  paths, refusal of absolute / ``..``, ``list_directory`` tool;
- ``config.local.json`` merge: ``course_dir`` / ``py_dir`` /
  ``python_doc_base_url``;
- ``python:<ref>`` citation rewriting (full + streaming);
- two-root doc server (www at ``/``, Python doc at ``/py/``).

The suite still runs under ``TUTOR_STUB=1``.
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from tutor import config, docs, docslinks as dl
from tutor import engine


def _write_tree(base: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


class BuildSystemAgentsTest(unittest.TestCase):
    """AGENTS.<model>.md takes precedence over AGENTS.md; none → empty."""

    MD = "# AGENTS\n\nTeach me socratically."

    def test_agents_md_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_tree(Path(tmp), {"AGENTS.md": self.MD})
            self.assertEqual(config.build_system("qwen3.5-4B", tmp), self.MD.strip())

    def test_model_variant_wins_over_plain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_tree(Path(tmp), {
                "AGENTS.md": "plain",
                "AGENTS.qwen3.5-4B.md": "variant",
            })
            self.assertEqual(config.build_system("qwen3.5-4B", tmp), "variant")

    def test_other_model_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_tree(Path(tmp), {
                "AGENTS.md": "plain",
                "AGENTS.ornith-1.5-9B.md": "variant",
            })
            self.assertEqual(config.build_system("qwen3.5-4B", tmp), "plain")

    def test_none_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(config.build_system("qwen3.5-4B", tmp), "")

    def test_empty_cwd_returns_empty(self) -> None:
        self.assertEqual(config.build_system("qwen3.5-4B", ""), "")

    def test_initial_state_loads_agents_from_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            variant = "AGENTS.qwen3.5-4B.md"
            _write_tree(Path(tmp), {variant: "variant-socratic"})
            state = engine.initial_state(
                model="qwen3.5-4B",
                session_id="t-qwen",
                label="qwen",
                cwd=str(tmp),
            )
            messages = state["messages"]
            self.assertTrue(messages)
            self.assertEqual(messages[0]["role"], "system")
            self.assertEqual(messages[0]["content"], "variant-socratic")


class ProjectToolPathsTest(unittest.TestCase):
    """Tool access to the open project (cwd), read-only, bounded."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        _write_tree(self.root, {
            "notes.txt": "alpha\nbeta\ngamma\n",
            "src/main.py": "def main():\n    return 42\n",
            "src/util.py": "VALEUR = 1\n",
            ".venv/foo.py": "x = 1\n",
        })

    def test_read_lines_project_file(self) -> None:
        trace, result = engine._exec_tool(
            "read_lines", {"path": "notes.txt", "start": 1, "num": 2},
            project_dir=str(self.root),
        )
        self.assertIn("notes.txt:1: alpha", result)
        self.assertIn("notes.txt:2: beta", result)
        self.assertEqual(trace["lines"], "1-2")

    def test_grep_project_file(self) -> None:
        trace, result = engine._exec_tool(
            "grep_files", {"pattern": "beta", "paths": ["notes.txt"]},
            project_dir=str(self.root),
        )
        self.assertIn("1 match", result)
        self.assertIn("notes.txt:2: beta", result)
        self.assertEqual(trace["matches"], 1)

    def test_absolute_path_refused(self) -> None:
        trace, result = engine._exec_tool(
            "read_lines", {"path": "/etc/hostname", "start": 1, "num": 1},
            project_dir=str(self.root),
        )
        self.assertIn("unknown path", result)

    def test_dotdot_climb_refused(self) -> None:
        trace, result = engine._exec_tool(
            "read_lines", {"path": "../etc/passwd", "start": 1, "num": 1},
            project_dir=str(self.root),
        )
        self.assertIn("unknown path", result)

    def test_subdirectory_glob(self) -> None:
        resolved = engine._resolve_tool_paths("src/*.py", str(self.root))
        self.assertEqual(len(resolved), 2)
        for p in resolved:
            self.assertTrue(p.startswith(str(self.root.resolve())))

    def test_list_directory_root(self) -> None:
        trace, result = engine._exec_tool(
            "list_directory", {}, project_dir=str(self.root),
        )
        self.assertIn("notes.txt", result)
        self.assertIn("src/", result)
        self.assertEqual(trace["tool"], "list_directory")

    def test_list_directory_subdir(self) -> None:
        trace, result = engine._exec_tool(
            "list_directory", {"path": "src"}, project_dir=str(self.root),
        )
        self.assertIn("main.py", result)
        self.assertIn("util.py", result)

    def test_list_directory_unknown(self) -> None:
        trace, result = engine._exec_tool(
            "list_directory", {"path": "nope"}, project_dir=str(self.root),
        )
        self.assertIn("unknown path", result)

    def test_find_path_project_glob(self) -> None:
        trace, result = engine._exec_tool(
            "find_path", {"glob": "**/*.py"}, project_dir=str(self.root),
        )
        self.assertIn("src/main.py", result)
        self.assertIn("src/util.py", result)
        self.assertEqual(trace["total"], 2)

    def test_find_path_noise_dir_excluded(self) -> None:
        trace, result = engine._exec_tool(
            "find_path", {"glob": "**/*.py"}, project_dir=str(self.root),
        )
        self.assertNotIn(".venv/foo.py", result)
        self.assertNotIn("foo.py", result)

    def test_find_path_absolute_refused(self) -> None:
        trace, result = engine._exec_tool(
            "find_path", {"glob": "/etc/passwd"}, project_dir=str(self.root),
        )
        self.assertIn("0 matches", result)
        self.assertEqual(trace["total"], 0)

    def test_find_path_dotdot_climb_refused(self) -> None:
        trace, result = engine._exec_tool(
            "find_path", {"glob": "../*.py"}, project_dir=str(self.root),
        )
        self.assertIn("0 matches", result)
        self.assertEqual(trace["total"], 0)

    def test_find_path_empty_pattern_yields_star(self) -> None:
        trace, result = engine._exec_tool(
            "find_path", {}, project_dir=str(self.root),
        )
        self.assertEqual(trace["pattern"], "*")
        self.assertIn("notes.txt", result)

    def test_diagnostics_single_good_file(self) -> None:
        trace, result = engine._exec_tool(
            "diagnostics", {"path": "src/main.py"}, project_dir=str(self.root),
        )
        self.assertIn("0 error(s)", result)
        self.assertEqual(trace["errors"], 0)

    def test_diagnostics_single_bad_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_tree(root, {"src/broken.py": "def oops(:\n"})
            trace, result = engine._exec_tool(
                "diagnostics", {"path": "src/broken.py"}, project_dir=str(root),
            )
            self.assertIn("1 error(s)", result)
            self.assertIn("broken.py:1:", result)
            self.assertEqual(trace["errors"], 1)

    def test_diagnostics_whole_project_all_good(self) -> None:
        trace, result = engine._exec_tool(
            "diagnostics", {}, project_dir=str(self.root),
        )
        self.assertIn("2 Python file(s)", result)
        self.assertIn("all good", result)
        self.assertEqual(trace["errors"], 0)

    def test_diagnostics_whole_project_mixed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_tree(root, {
                "a.py": "x = 1\n",
                "b.py": "def boom(:\n",
            })
            trace, result = engine._exec_tool(
                "diagnostics", {}, project_dir=str(root),
            )
            self.assertIn("2 Python file(s)", result)
            self.assertIn("1 with error(s)", result)
            self.assertIn("1 error(s) total", result)
            self.assertIn("b.py (1 error(s))", result)
            self.assertEqual(trace["errors"], 1)

    def test_diagnostics_qmd_path_rejected(self) -> None:
        _write_tree(self.root, {"cours.qmd": "# titre\n"})
        trace, result = engine._exec_tool(
            "diagnostics", {"path": "cours.qmd"}, project_dir=str(self.root),
        )
        self.assertIn("not a Python file", result)
        self.assertEqual(trace["errors"], 0)


class LocalConfigMergeTest(unittest.TestCase):
    """config.local.json deep-merge drives course_dir / py_dir / python base."""

    def _fake_config(self, tmp: Path, py_dir: str = "") -> dict:
        return {
            "paths": {
                "course_dir": str(tmp),
                "corpus_root": "corpus/Courses",
                "gguf_dir": "models",
                "external_template": "models/x.jinja",
                "sessions_dir": "sessions",
            },
            "corpus_files": {"01": "01_a.qmd"},
            "docs": {
                "base_url": "http://127.0.0.1:8765",
                "www_dir": "corpus/www",
                "sections_json": "corpus/sections.json",
                "py_dir": py_dir,
                "python_doc_url": "https://docs.python.org/3/",
            },
        }

    def test_course_dir_reroutes_corpus_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_tree(root, {
                "Courses/01_a.qmd": "# x\n",
                "www/index.html": "<h1>x</h1>",
                "sections.json": "{}",
            })
            fake = self._fake_config(root)
            with mock.patch.object(config, "_CONFIG", fake):
                self.assertEqual(config.course_dir(), str(root.resolve()))
                self.assertEqual(config.corpus_root(), str((root / "Courses").resolve()))
                self.assertEqual(config.www_dir(), str((root / "www").resolve()))
                self.assertEqual(config.sections_json(), root / "sections.json")

    def test_py_dir_local_mirror_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = self._fake_config(Path(tmp), py_dir=str(Path(tmp) / "python-doc"))
            with mock.patch.object(config, "_CONFIG", fake):
                self.assertNotEqual(config.py_dir(), "")
                self.assertEqual(
                    config.python_doc_base_url(),
                    "http://127.0.0.1:8765/py",
                )

    def test_py_dir_empty_falls_back_online(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = self._fake_config(Path(tmp))
            with mock.patch.object(config, "_CONFIG", fake):
                self.assertEqual(config.py_dir(), "")
                self.assertEqual(
                    config.python_doc_base_url(),
                    "https://docs.python.org/3",
                )


class PythonCiteRewriteTest(unittest.TestCase):
    """Citations ``python:<ref>`` → cliquables (en ligne + miroir local)."""

    def test_rewrite_module_online(self) -> None:
        with mock.patch.object(
            config, "python_doc_base_url",
            return_value="https://docs.python.org/3/",
        ):
            got = dl.rewrite_content(
                "Voir python:asyncio pour la boucle.", "http://127.0.0.1:8765"
            )
        self.assertIn(
            "[python:asyncio](https://docs.python.org/3/library/asyncio.html)",
            got,
        )

    def test_rewrite_module_anchor_local_mirror(self) -> None:
        with mock.patch.object(
            config, "python_doc_base_url",
            return_value="http://127.0.0.1:8765/py",
        ):
            got = dl.rewrite_content(
                "Cf. python:queue#SimpleQueue sur la file.", "http://127.0.0.1:8765"
            )
        self.assertIn(
            "[python:queue#SimpleQueue]"
            "(http://127.0.0.1:8765/py/library/queue.html#SimpleQueue)",
            got,
        )

    def test_no_rewrite_when_followed_by_space_word(self) -> None:
        with mock.patch.object(
            config, "python_doc_base_url",
            return_value="https://docs.python.org/3/",
        ):
            got = dl.rewrite_content(
                "python: c'est le langage du cours.", "http://127.0.0.1:8765"
            )
        self.assertNotIn("](http", got)

    def test_streaming_cut_recomposed(self) -> None:
        with mock.patch.object(
            config, "python_doc_base_url",
            return_value="https://docs.python.org/3/",
        ):
            rw = dl.LinkRewriter("http://127.0.0.1:8765")
            out: list[str] = []
            for c in ["Voir python:asynci", "o pour la boucle."]:
                out += rw.feed(c)
            out += rw.finish()
        joined = "".join(out)
        self.assertIn(
            "[python:asyncio](https://docs.python.org/3/library/asyncio.html)",
            joined,
        )
        self.assertIn("pour la boucle.", joined)

    def test_rest_unaltered(self) -> None:
        got = dl.rewrite_content("Aucune citation ici.", "http://127.0.0.1:8765")
        self.assertEqual(got, "Aucune citation ici.")


class TwoRootDocsServerTest(unittest.TestCase):
    """Serves www at / and the local Python doc at /py/."""

    def test_serves_both_roots(self) -> None:
        www = Path(tempfile.mkdtemp(prefix="tutor-www2-"))
        pydoc = Path(tempfile.mkdtemp(prefix="tutor-py-"))
        _write_tree(www, {"index.html": "<h1>book</h1>"})
        _write_tree(pydoc, {"library/queue.html": "<h1>queue</h1>"})
        server = docs.serve("127.0.0.1", 0, str(www), str(pydoc))
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html") as r:
                self.assertEqual(r.read().decode(), "<h1>book</h1>")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/py/library/queue.html"
            ) as r:
                self.assertEqual(r.read().decode(), "<h1>queue</h1>")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
