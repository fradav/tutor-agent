"""Tests §4 — backend serveur (server.py), partie « pure » seulement.

Vérifie la génération du preset ROUTEUR (INI) et la ligne de commande llama-server,
sans jamais lancer de processus ni toucher au port :
  - [*] : jinja = true ; reasoning-preserve = true (global) ;
  - qwen3.5-4B : template EXTERNE (chat-template-file qwen3.5-chat-template.jinja),
        load-on-startup = true (modèle par défaut) ;
  - ornith-1.5-9B / gemma-4-E4B : template EMBARQUÉ
        (pas de chat-template-file) ;
  - tous : c = <contexte 32768>, n-gpu-layers = 99, load-on-startup ne précharge
    que le modèle par défaut (qwen3.5-4B) ;
  - cmd routeur : --models-preset <ini> --models-max 1, pas de --model.
"""
from __future__ import annotations

import unittest
from unittest import mock

from tutor import config, server


def _section(text: str, name: str) -> str:
    """Rend le bloc INI d'une section ``[name]`` (jusqu'à la section suivante)."""
    lines = text.splitlines()
    out: list[str] = []
    seen = False
    for line in lines:
        if line.startswith("["):
            if seen:
                break
            seen = line == f"[{name}]"
            continue
        if seen and line.strip():
            out.append(line)
    return "\n".join(out)


class PresetRouterTest(unittest.TestCase):
    def test_preset_has_version_and_global_flags(self) -> None:
        text = server.render_preset()
        self.assertTrue(text.startswith("version = 1"))
        self.assertIn("[*]", text)
        self.assertIn("jinja = true", text)
        self.assertIn("reasoning-preserve = true", text)

    def test_qwen354b_section_external_template(self) -> None:
        section = _section(server.render_preset(), "qwen3.5-4B")
        self.assertIn(f"chat-template-file = {config.external_template()}", section)
        self.assertIn("load-on-startup = true", section)  # modèle par défaut

    def test_ornith_15_9b_embedded_template(self) -> None:
        section = _section(server.render_preset(), "ornith-1.5-9B")
        self.assertNotIn("chat-template-file", section)
        self.assertIn("load-on-startup = false", section)

    def test_gemma_4_e4b_embedded_template(self) -> None:
        section = _section(server.render_preset(), "gemma-4-E4B")
        self.assertNotIn("chat-template-file", section)
        self.assertIn("load-on-startup = false", section)  # défaut = qwen3.5-4B

    def test_common_section_fields_all_models(self) -> None:
        for model in ("qwen3.5-4B", "ornith-1.5-9B", "gemma-4-E4B"):
            with self.subTest(model=model):
                section = _section(server.render_preset(), config.profile(model)["alias"])
                self.assertIn(f"model = {config.model_path(model)}", section)
                self.assertIn(f"c = {config.max_tokens()}", section)
                self.assertIn("n-gpu-layers = 99", section)

    def test_router_cmd_flags(self) -> None:
        cmd = server._router_cmd()
        self.assertEqual(cmd[0], config.llama_bin())
        self.assertIn("--models-preset", cmd)
        self.assertIn("--models-max", cmd)
        self.assertEqual(cmd[cmd.index("--models-max") + 1], "1")
        # Plus de --model mono : tout est décrit dans le preset.
        self.assertNotIn("--model", cmd)
        self.assertNotIn("--alias", cmd)
        self.assertNotIn("--chat-template-file", cmd)


class EnsureRefreshTest(unittest.TestCase):
    """ensure() : routeur géré déjà up — adoption OU redémarrage si preset obsolète.

    Cas couvert : après un renommage d'alias (config.json), le routeur encore en
    mémoire avec l'ancien preset ne sert aucun alias actuel → on régénère le
    preset et on redémarre une fois. Si les alias sont servis → simple adoption,
    aucun restart (PAS de kill au switch).
    """

    def _run(self, served_aliases: list[str]):
        with (
            mock.patch("tutor.server.health_ok", return_value=True),
            mock.patch("tutor.server.is_managed", return_value=True),
            mock.patch("tutor.server._server_aliases", return_value=served_aliases),
            mock.patch("tutor.server.stop") as stop,
            mock.patch("tutor.server._wait_port_free") as wait_free,
            mock.patch("tutor.server.start",
                       return_value={"status": "ok", "pid": 4242, "logfile": "x",
                                     "detail": "routeur démarré"}) as start,
            mock.patch("tutor.server._mark_alias") as mark,
        ):
            resp = server.ensure("ornith-1.5-9B", wait_up_to=5.0)
        return resp, stop, wait_free, start, mark

    def test_ensure_refreshes_stale_router(self) -> None:
        """Routeur géré qui ne sert que les ANCIENS alias → restart une fois."""
        resp, stop, wait_free, start, mark = self._run(["ornith", "q8"])
        stop.assert_called_once()
        wait_free.assert_called_once()
        start.assert_called_once_with(wait_up_to=5.0)
        mark.assert_called_once_with("ornith-1.5-9B")
        self.assertEqual(resp["status"], "ok")
        self.assertIn("redémarré", resp["detail"])

    def test_ensure_adopts_fresh_router_no_restart(self) -> None:
        """Routeur géré qui sert les alias ACTUELS → adoption, aucun restart."""
        resp, stop, wait_free, start, mark = self._run(list(server._preset_aliases()))
        stop.assert_not_called()
        wait_free.assert_not_called()
        start.assert_not_called()
        mark.assert_called_once_with("ornith-1.5-9B")
        self.assertEqual(resp["status"], "ok")
        self.assertIn("alias servi", resp["detail"])

    def test_ensure_restarts_on_any_current_alias_served(self) -> None:
        """Dès qu'UN alias actuel est servi, pas de restart (aucun redémarrage au
        switch alors qu'un autre modèle du preset est préchargé)."""
        served = ["qwen3.5-4B"] + ["ornith", "q8"]  # un seul alias courant
        resp, stop, wait_free, start, _mark = self._run(served)
        stop.assert_not_called()
        start.assert_not_called()
        self.assertEqual(resp["status"], "ok")


class EnsureFallbackTest(unittest.TestCase):
    """ensure() : routeur local ABSENT → bascule sur le fallback distant si
    configuré (`fallback.endpoint`) et joignable ; sinon démarrage local.

    Cas couverts :
      - fallback joignable → mode "fallback", `start` jamais appelé, flag engagé ;
      - fallback injoignable → démarrage local (mode "local"), flag non engagé.
    """

    FB = "http://192.168.1.50:8080"

    def _run(self, fb_ok: bool):
        with (
            mock.patch("tutor.config.fallback_endpoint", return_value=self.FB),
            mock.patch("tutor.server.health_ok",
                       side_effect=lambda base=None, timeout=2.0, api_key=None: bool(base) and fb_ok),
            mock.patch("tutor.server.is_managed", return_value=False),
            mock.patch("tutor.server._port_busy", return_value=False),
            mock.patch("tutor.server.start",
                       return_value={"status": "ok", "pid": 1, "logfile": "x",
                                     "detail": "routeur démarré"}) as start,
            mock.patch("tutor.server._mark_alias") as mark,
            mock.patch("tutor.config.set_fallback_active") as set_fb,
        ):
            resp = server.ensure("ornith-1.5-9B", wait_up_to=5.0)
        return resp, start, mark, set_fb

    def test_fallback_engaged_when_local_absent_remote_ok(self) -> None:
        resp, start, mark, set_fb = self._run(fb_ok=True)
        start.assert_not_called()
        mark.assert_called_once_with("ornith-1.5-9B")
        self.assertEqual(resp["mode"], "fallback")
        self.assertEqual(resp["status"], "ok")
        self.assertIn("fallback distant", resp["detail"])
        self.assertIn(self.FB, resp["detail"])
        # reset (False) en début d'ensure puis engagement (True) : dernier état actif.
        self.assertEqual(set_fb.call_args, mock.call("ornith-1.5-9B", True))

    def test_ensure_starts_local_when_fallback_unreachable(self) -> None:
        resp, start, _mark, set_fb = self._run(fb_ok=False)
        start.assert_called_once_with(wait_up_to=5.0)
        self.assertEqual(resp["mode"], "local")
        # reset au début, pas d'engagement.
        self.assertEqual(set_fb.call_args, mock.call("ornith-1.5-9B", False))


if __name__ == "__main__":
    unittest.main()
