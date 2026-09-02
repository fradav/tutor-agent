"""Tests — llm.py : injection du header d'authentification pour le fallback distant.

`stream_complete`/`complete` ne touchent jamais au réseau ici : `urlopen` est
mocké (requête capturée, réponse SSE minimale). On vérifie que :
  - sans api_key → aucun header `Authorization` (comportement local inchangé) ;
  - avec api_key → header `Authorization: Bearer <clef>`.
"""
from __future__ import annotations

import io
import unittest
from unittest import mock

from tutor import llm


class _FakeResp:
    """Mini-réponse SSE : context manager + itérable de lignes bytes."""

    def __init__(self, lines: list[bytes]) -> None:
        self._buf = io.BytesIO(b"\n".join(lines) + b"\n")

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        line = self._buf.readline()
        if not line:
            raise StopIteration
        return line


def _capture_urlopen(captured: dict):
    """Fake urlopen : capture le `Request`, rend une réponse SSE minimale."""
    done = False

    def fake_urlopen(req, timeout=600):
        nonlocal done
        captured["req"] = req
        if not done:
            done = True
            return _FakeResp(
                [b'data: {"choices":[{"delta":{"content":"salut"}}]}',
                 b"data: [DONE]"]
            )
        return _FakeResp([])

    return fake_urlopen


class StreamCompleteAuthTest(unittest.TestCase):
    def _run(self, api_key: str | None):
        captured: dict = {}
        with mock.patch(
            "tutor.llm.urllib.request.urlopen",
            side_effect=_capture_urlopen(captured),
        ):
            list(llm.stream_complete(
                [{"role": "user", "content": "x"}],
                "qwen3.5-4B", "http://192.168.1.50:8080", 100,
                api_key=api_key,
            ))
        return captured["req"]

    def test_no_auth_header_without_key(self) -> None:
        req = self._run(None)
        self.assertIsNone(req.get_header("Authorization"))

    def test_bearer_header_with_key(self) -> None:
        req = self._run("sk-clef-tutos")
        self.assertEqual(req.get_header("Authorization"), "Bearer sk-clef-tutos")

    def test_complete_passes_api_key_to_stream(self) -> None:
        captured: dict = {}
        with mock.patch(
            "tutor.llm.urllib.request.urlopen",
            side_effect=_capture_urlopen(captured),
        ):
            llm.complete(
                [{"role": "user", "content": "x"}],
                "qwen3.5-4B", "http://192.168.1.50:8080", 100,
                api_key="sk-complete",
            )
        self.assertEqual(
            captured["req"].get_header("Authorization"), "Bearer sk-complete")


if __name__ == "__main__":
    unittest.main()
