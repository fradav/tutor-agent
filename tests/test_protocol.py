"""Tests du protocole ACP (protocol.py) — sans serveur LLM.

Utilise la paire de transports en mémoire du SDK (``memory_transport_pair``) :
le side-agent tourne via ``acp.run_agent`` (tâche de fond), le side-client via
``acp.connect_to_agent``. Pas de dépendance supplémentaire (unittest standard).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

# Mode STUB : le moteur tuteur ne contacte jamais de llama-server et ne lit pas
# le corpus dans les tests. À poser AVANT l'import de protocol (qui charge
# tutor.config, où STUB est résolu au premier import).
os.environ["TUTOR_STUB"] = "1"

import acp
from acp._transport import memory_transport_pair
from acp.schema import AgentThoughtChunk

from protocol import AGENT_NAME, DEFAULT_MODEL, MODEL_FILE, TutorAgent
from tutor import server as tutor_server


class _CollectingClient:
    """Client de test : accumule les blocs reçus via ``session/update``.

    "Chunks" = blocs de contenu texte ; « thoughts » = blocs de raisonnement
    (AgentThoughtChunk). Les deux sont séparés pour pouvoir asserter chaque canal.
    """

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.thoughts: list[str] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        content = getattr(update, "content", None)
        text = getattr(content, "text", None)
        if isinstance(text, str) and text:
            target = self.thoughts if isinstance(update, AgentThoughtChunk) else self.chunks
            target.append(text)


class AcpProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.agent = TutorAgent()
        self.client = _CollectingClient()
        t_agent, t_client = memory_transport_pair()
        # Duck-typing volontaire : TutorAgent / _CollectingClient n'implémentent qu'un
        # sous-ensemble de l'Protocol/Client (voir protocol.py). Le router du SDK
        # route via getattr, pas via l'héritage — vérifié par les tests ci-dessous.
        self._agent_task = asyncio.create_task(acp.run_agent(self.agent, t_agent))  # type: ignore[arg-type]
        self.conn = acp.connect_to_agent(self.client, t_client)  # type: ignore[arg-type]

    async def asyncTearDown(self) -> None:
        if self._agent_task is not None:
            self._agent_task.cancel()
            try:
                await self._agent_task
            except BaseException:
                pass  # teardown : on ne masque jamais le résultat du test par la clôture du side-agent
        await self.conn.close()

    async def _new_session(self, cwd: str) -> str:
        return (await self.conn.new_session(cwd=cwd)).session_id

    async def test_initialize(self) -> None:
        resp = await self.conn.initialize(acp.PROTOCOL_VERSION)
        self.assertEqual(resp.protocol_version, acp.PROTOCOL_VERSION)
        agent_info = resp.agent_info
        assert agent_info is not None
        self.assertEqual(agent_info.name, AGENT_NAME)
        # Profil Ask : load_session annoncé (reload Zed restaure la conversation),
        # pas de terminal.
        capabilities = resp.agent_capabilities
        assert capabilities is not None
        self.assertTrue(capabilities.load_session)

    async def test_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            sid = await self._new_session(cwd)
        self.assertIsInstance(sid, str)
        self.assertTrue(sid)
        self.assertIn(sid, self.agent._sessions)

    async def test_new_session_model_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            Path(cwd, MODEL_FILE).write_text("ministral-3-8B-Reasoning\n", encoding="utf-8")
            sid = await self._new_session(cwd)
        self.assertTrue(sid.startswith("ministral-3-8B-Reasoning-"))

    async def test_new_session_model_from_file_legacy_key(self) -> None:
        """Fichier périmé (ancienne clef avant le renommage) → normalisée au
        session/new (pas de « profil inconnu » si server.ensure était appelé)."""
        with tempfile.TemporaryDirectory() as cwd:
            Path(cwd, MODEL_FILE).write_text("ministral\n", encoding="utf-8")
            sid = await self._new_session(cwd)
        self.assertTrue(sid.startswith("ministral-3-8B-Reasoning-"))

    async def test_new_session_default_model(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            sid = await self._new_session(cwd)
        self.assertTrue(sid.startswith(f"{DEFAULT_MODEL}-"))

    async def test_new_session_backend_ensure_called(self) -> None:
        """Hors STUB, session/new branche server.ensure et persiste l'état dès la
        création (une session vide doit rester rechargeable par Zed)."""
        with (
            mock.patch("protocol.config.STUB", False),
            mock.patch("protocol.server.ensure", return_value={"detail": "ready (fake)"}) as fake_ensure,
        ):
            with tempfile.TemporaryDirectory() as cwd:
                with tempfile.TemporaryDirectory() as store:
                    with mock.patch("protocol.config.sessions_dir", return_value=Path(store)):
                        sid = await self._new_session(cwd)
                    fake_ensure.assert_called_once_with(DEFAULT_MODEL)
                    self.assertTrue(sid.startswith(f"{DEFAULT_MODEL}-"))
                    # reload Zed : l'état doit exister sur disque dès session/new.
                    self.assertTrue((Path(store) / f"{sid}.json").exists())

    async def test_new_session_backend_error_raises(self) -> None:
        """Backend indisponible au session/new → erreur JSON-RPC interne explicite."""
        with (
            mock.patch("protocol.config.STUB", False),
            mock.patch("protocol.server.ensure", side_effect=tutor_server.ServerError("boom")),
        ):
            with tempfile.TemporaryDirectory() as cwd:
                with self.assertRaises(acp.exceptions.RequestError) as ctx:
                    await self._new_session(cwd)
        self.assertEqual(ctx.exception.code, -32603)  # internal_error
        self.assertEqual(ctx.exception.data.get("reason"), "backend ornith-1.5-9B indisponible")

    async def test_new_session_returns_config_options(self) -> None:
        """session/new expose un sélecteur de modèle ACP (une option ``select``)."""
        with tempfile.TemporaryDirectory() as cwd:
            resp = await self.conn.new_session(cwd=cwd)
        opts = resp.config_options
        self.assertIsNotNone(opts)
        self.assertEqual(len(opts), 1)
        opt = opts[0]
        self.assertEqual(opt.id, "model")
        self.assertEqual(opt.type, "select")
        self.assertEqual(opt.category, "model")
        self.assertEqual(opt.current_value, DEFAULT_MODEL)
        # Une entrée par profil de config.json (qwen3.5-4B, ornith-1.5-9B,
        # ministral-3-8B-Reasoning, gemma-4-E4B).
        self.assertEqual(len(opt.options), 4)
        self.assertIn(DEFAULT_MODEL, {o.value for o in opt.options})

    async def test_load_session_replays_history(self) -> None:
        """session/load restaure une session persistée et rejoue l'historique.

        En STUB le moteur ne persiste pas : on écrit l'état à la main dans un
        dossier temporaire (patch de config.sessions_dir) puis on recharge à froid
        (sessions en mémoire vidées, log client vidé). Le replay émet user /
        thought / message via session/update.
        """
        with tempfile.TemporaryDirectory() as cwd:
            sid = await self._new_session(cwd)
            await self.conn.prompt(sid, [acp.text_block("Premier message")])
            await self.conn.prompt(sid, [acp.text_block("Deuxième message")])
        state = self.agent._sessions[sid]["state"]
        self.assertEqual(len(state["turns"]), 2)
        self.assertEqual(state["model"], DEFAULT_MODEL)

        with tempfile.TemporaryDirectory() as store:
            store_path = Path(store)
            (store_path / "transcripts").mkdir()
            (store_path / f"{sid}.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            # Rechargement à froid : plus de session en mémoire ni d'historique client.
            self.agent._sessions.clear()
            self.client.chunks.clear()
            self.client.thoughts.clear()
            with mock.patch("protocol.config.sessions_dir", return_value=store_path):
                resp = await self.conn.load_session(session_id=sid, cwd=cwd)

        opts = resp.config_options
        self.assertIsNotNone(opts)
        self.assertEqual(opts[0].id, "model")
        self.assertEqual(opts[0].current_value, DEFAULT_MODEL)
        session = self.agent._sessions.get(sid)
        self.assertIsNotNone(session)
        self.assertEqual(session["model"], DEFAULT_MODEL)
        self.assertEqual([t["turn"] for t in session["state"]["turns"]], [1, 2])
        # Le log parallèle des messages étudiants est reconstruit depuis les turns.
        self.assertEqual(
            [m["content"] for m in session["messages"]],
            ["Premier message", "Deuxième message"],
        )
        # Le replay a émis un bloc de raisonnement par tour et le contenu des deux.
        self.assertEqual(len(self.client.thoughts), 2)
        combined = " ".join(self.client.chunks)
        self.assertIn("Premier message", combined)
        self.assertIn("Deuxième message", combined)
        self.assertIn(DEFAULT_MODEL, " ".join(self.client.thoughts))

    async def test_load_session_missing_state_rebuilds_blank(self) -> None:
        """Session jamais persistée → reload Zed réussit avec une session vierge
        reconstruite ; le modèle est relu du préfixe du sessionId (ancienne clef).
        sessions_dir est isolé dans un dossier temporaire : le test ne doit pas
        dépendre d'un état réel resté dans le répertoire runtime."""
        with tempfile.TemporaryDirectory() as store:
            store_path = Path(store)
            with tempfile.TemporaryDirectory() as cwd:
                with mock.patch("protocol.config.sessions_dir", return_value=store_path):
                    resp = await self.conn.load_session(
                        session_id="ministral-Playground-empty-6", cwd=cwd)
        opts = resp.config_options
        self.assertIsNotNone(opts)
        self.assertEqual(opts[0].current_value, "ministral-3-8B-Reasoning")
        session = self.agent._sessions.get("ministral-Playground-empty-6")
        self.assertIsNotNone(session)
        self.assertEqual(session["model"], "ministral-3-8B-Reasoning")
        self.assertEqual(session["state"]["turns"], [])
        self.assertEqual(session["messages"], [])
        # Aucun historique à rejouer : le client n'a rien reçu.
        self.assertEqual(self.client.chunks, [])
        self.assertEqual(self.client.thoughts, [])

    async def test_load_session_missing_state_unknown_prefix_uses_default(self) -> None:
        """Préfixe inconnu (aucun profil ne matche) → défaut config.json.
        sessions_dir isolé (même raison que le test au-dessus)."""
        with tempfile.TemporaryDirectory() as store:
            store_path = Path(store)
            with tempfile.TemporaryDirectory() as cwd:
                with mock.patch("protocol.config.sessions_dir", return_value=store_path):
                    resp = await self.conn.load_session(
                        session_id="bizarre-session-xyz", cwd=cwd)
        opts = resp.config_options
        self.assertIsNotNone(opts)
        self.assertEqual(opts[0].current_value, DEFAULT_MODEL)
        self.assertEqual(
            self.agent._sessions["bizarre-session-xyz"]["state"]["turns"], [])

    async def test_load_session_corrupt_state_raises(self) -> None:
        """État présent mais illisible (JSON invalide) → -32602."""
        with tempfile.TemporaryDirectory() as store:
            (Path(store) / "session-corrompue.json").write_text(
                "{pas du json", encoding="utf-8")
            with mock.patch("protocol.config.sessions_dir", return_value=Path(store)):
                with self.assertRaises(acp.exceptions.RequestError) as ctx:
                    await self.conn.load_session(
                        session_id="session-corrompue", cwd="/tmp")
        self.assertEqual(ctx.exception.code, -32602)

    async def test_load_session_normalizes_legacy_model_key(self) -> None:
        """État persisté avec une ancienne clef (q8) → clef actuelle en mémoire."""
        with tempfile.TemporaryDirectory() as store:
            store_path = Path(store)
            (store_path / "transcripts").mkdir()
            legacy = {"id": "q8-ancienne", "model": "q8", "turns": []}
            (store_path / "q8-ancienne.json").write_text(
                json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
            with mock.patch("protocol.config.sessions_dir", return_value=store_path):
                resp = await self.conn.load_session(session_id="q8-ancienne", cwd="/tmp")
        opts = resp.config_options
        self.assertIsNotNone(opts)
        self.assertEqual(opts[0].current_value, "qwen3.5-4B")
        session = self.agent._sessions.get("q8-ancienne")
        self.assertIsNotNone(session)
        self.assertEqual(session["model"], "qwen3.5-4B")
        self.assertEqual(session["state"]["model"], "qwen3.5-4B")

    async def test_set_config_option_switch_model(self) -> None:
        """session/set_config_option bascule la session vers un autre profil."""
        with tempfile.TemporaryDirectory() as cwd:
            sid = await self._new_session(cwd)
        self.assertEqual(self.agent._sessions[sid]["model"], DEFAULT_MODEL)
        with (
            mock.patch("protocol.config.STUB", False),
            mock.patch("protocol.server.ensure", return_value={"detail": "ready (fake)"}) as fake_ensure,
        ):
            resp = await self.conn.set_config_option(
                config_id="model", session_id=sid, value="ministral-3-8B-Reasoning")
        # Le backend du nouveau modèle est préparé hors STUB.
        fake_ensure.assert_called_once_with("ministral-3-8B-Reasoning")
        session = self.agent._sessions[sid]
        self.assertEqual(session["model"], "ministral-3-8B-Reasoning")
        self.assertEqual(session["state"]["model"], "ministral-3-8B-Reasoning")
        self.assertEqual(session["messages"], [])
        resp_opts = resp.config_options
        self.assertIsNotNone(resp_opts)
        self.assertEqual(resp_opts[0].current_value, "ministral-3-8B-Reasoning")

    async def test_set_config_option_unknown_config_or_model_raises(self) -> None:
        """Config inconnue, profil inconnu ou session inexistante → -32602."""
        with tempfile.TemporaryDirectory() as cwd:
            sid = await self._new_session(cwd)
        with self.assertRaises(acp.exceptions.RequestError) as ctx:
            await self.conn.set_config_option(
                config_id="foo", session_id=sid, value="ornith-1.5-9B")
        self.assertEqual(ctx.exception.code, -32602)
        with self.assertRaises(acp.exceptions.RequestError) as ctx:
            await self.conn.set_config_option(
                config_id="model", session_id=sid, value="nimporte-quoi")
        self.assertEqual(ctx.exception.code, -32602)
        with self.assertRaises(acp.exceptions.RequestError) as ctx:
            await self.conn.set_config_option(
                config_id="model", session_id="session-inconnue", value="ornith-1.5-9B")
        self.assertEqual(ctx.exception.code, -32602)

    async def test_prompt_streams_and_ends(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            sid = await self._new_session(cwd)
        msg = "Bonjour tuteur, je veux enchaîner des tâches asyncio."
        resp = await self.conn.prompt(sid, [acp.text_block(msg)])
        self.assertEqual(resp.stop_reason, "end_turn")
        full = "".join(self.client.chunks)
        self.assertIn("Bonjour tuteur", full)
        self.assertIn(DEFAULT_MODEL, full)  # modèle actif (défaut) annoncé
        # Le raisonnement est streamé sur son propre canal (bloc thought).
        self.assertTrue(self.client.thoughts, "aucun bloc de raisonnement émis")
        self.assertIn(DEFAULT_MODEL, "".join(self.client.thoughts))

    async def test_prompt_backend_down_streams_message(self) -> None:
        """Backend injoignable pendant un tour → message explicite streamé,
        stop_reason=end_turn, pas d'erreur JSON-RPC (au lieu du Connection refused brut)."""
        import urllib.error
        from protocol import BACKEND_DOWN_MESSAGE

        with (
            mock.patch("protocol.config.STUB", False),
            mock.patch("protocol.server.ensure", return_value={"detail": "ready (fake)"}),
            mock.patch(
                "protocol.TutorEngine.run_turn_stream",
                side_effect=urllib.error.URLError(
                    ConnectionRefusedError(61, "Connection refused")),
            ),
        ):
            with tempfile.TemporaryDirectory() as cwd:
                with tempfile.TemporaryDirectory() as store:
                    with mock.patch("protocol.config.sessions_dir", return_value=Path(store)):
                        sid = await self._new_session(cwd)
            msg = "Est-ce que le backend est vivant ?"
            resp = await self.conn.prompt(sid, [acp.text_block(msg)])
        self.assertEqual(resp.stop_reason, "end_turn")
        self.assertIn(BACKEND_DOWN_MESSAGE, "".join(self.client.chunks))
        # Le message étudiant reste dans l'historique de session ; aucun turn persisté.
        session = self.agent._sessions[sid]
        self.assertEqual([m["content"] for m in session["messages"]], [msg])
        self.assertEqual(session["state"]["turns"], [])

    async def test_prompt_unknown_session_errors(self) -> None:
        with self.assertRaises(acp.exceptions.RequestError) as ctx:
            await self.conn.prompt("session-inconnue", [acp.text_block("coucou")])
        self.assertEqual(ctx.exception.code, -32602)

    async def test_cancel_interrupts_stream(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            sid = await self._new_session(cwd)

        async def wait_chunks(min_chunks: int, timeout: float = 2.0) -> None:
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                if len(self.client.chunks) >= min_chunks:
                    return
                await asyncio.sleep(0.01)
            self.fail(f"timeout : {min_chunks} chunks texte attendus")

        prompt_task = asyncio.create_task(
            self.conn.prompt(sid, [acp.text_block("Une réponse très longue que je vais annuler.")])
        )
        # Attend le premier bloc contenu puis annule (le raisonnement STUB est émis d'abord).
        await wait_chunks(1)
        await self.conn.cancel(sid)
        resp = await prompt_task
        self.assertEqual(resp.stop_reason, "cancelled")
        # Le streaming contenu a été coupé avant la fin (le stub émet 3 blocs au plus).
        self.assertLess(len(self.client.chunks), 3)


if __name__ == "__main__":
    unittest.main()
