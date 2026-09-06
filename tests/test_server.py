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

import tempfile
import unittest
from pathlib import Path
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
            # Pas de remote : on force la voie locale même si un `.env-secret`
            # réel est présent sur la machine de dev.
            mock.patch("tutor.config.fallback_endpoint", return_value=None),
            mock.patch("tutor.config.fallback_api_key", return_value=None),
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
        """Tous les alias du preset actuel sont servis (même si le routeur en
        sert aussi d'obsolètes en parallèle) → adoption, aucun restart : le
        `>=` de _adopt_or_refresh exige la présence de TOUS les alias pour
        ne pas tuer le serveur au switch de modèle."""
        served = list(server._preset_aliases()) + ["legacy-q8", "old-gemma"]
        resp, stop, wait_free, start, _mark = self._run(served)
        stop.assert_not_called()
        start.assert_not_called()
        self.assertEqual(resp["status"], "ok")


class EnsureFallbackTest(unittest.TestCase):
    """ensure() : l'endpoint distant (.env-secret : endpoint + clef) est PRIORITAIRE.

    Cas couverts :
      - remote configuré + joignable (local up OU down) → mode "fallback",
        llama-server local tué (stop), `start` jamais appelé ;
      - remote configuré mais injoignable → démarrage local (mode "local") ;
      - aucun remote → démarrage local (mode "local").
    """

    FB = "http://192.168.1.50:8080"

    def _run(self, fb_ok: bool, local_up: bool = False, remote_set: bool = True):
        with (
            mock.patch("tutor.config.fallback_endpoint",
                       return_value=self.FB if remote_set else None),
            mock.patch("tutor.config.fallback_api_key",
                       return_value="sk-test" if remote_set else None),
            mock.patch("tutor.server.health_ok",
                       side_effect=lambda base=None, timeout=2.0, api_key=None:
                           (fb_ok if base == self.FB else local_up)),
            mock.patch("tutor.server.is_managed", return_value=False),
            mock.patch("tutor.server._port_busy", return_value=False),
            mock.patch("tutor.server.stop") as stop,
            mock.patch("tutor.server._wait_port_free") as wait_free,
            mock.patch("tutor.server.start",
                       return_value={"status": "ok", "pid": 1, "logfile": "x",
                                     "detail": "routeur démarré"}) as start,
            mock.patch("tutor.server._mark_alias") as mark,
            mock.patch("tutor.config.set_fallback_active") as set_fb,
        ):
            resp = server.ensure("ornith-1.5-9B", wait_up_to=5.0)
        return resp, start, mark, set_fb, stop, wait_free

    def test_remote_priority_even_when_local_up(self) -> None:
        """Endpoint + clef configurés et joignables → remote prioritaire : le
        llama-server local est tué même s'il répond déjà (mode "fallback")."""
        resp, start, mark, set_fb, stop, wait_free = self._run(
            fb_ok=True, local_up=True)
        stop.assert_called_once()
        wait_free.assert_called_once()
        start.assert_not_called()
        mark.assert_called_once_with("ornith-1.5-9B")
        self.assertEqual(resp["mode"], "fallback")
        self.assertEqual(resp["status"], "ok")
        self.assertIn("arrêté", resp["detail"])
        self.assertIn(self.FB, resp["detail"])
        # reset (False) en début d'ensure puis engagement (True) : dernier état.
        self.assertEqual(set_fb.call_args, mock.call("ornith-1.5-9B", True))

    def test_remote_priority_when_local_absent(self) -> None:
        """Pas de serveur local mais remote joignable → toujours mode "fallback"."""
        resp, start, mark, set_fb, stop, wait_free = self._run(
            fb_ok=True, local_up=False)
        stop.assert_called_once()  # kill idempotent même si rien ne tournait
        wait_free.assert_called_once()
        start.assert_not_called()
        self.assertEqual(resp["mode"], "fallback")
        self.assertEqual(set_fb.call_args, mock.call("ornith-1.5-9B", True))

    def test_remote_unreachable_falls_back_to_local(self) -> None:
        """Remote configuré (endpoint + clef) mais injoignable → démarrage local."""
        resp, start, mark, set_fb, stop, wait_free = self._run(
            fb_ok=False, local_up=False)
        stop.assert_not_called()
        wait_free.assert_not_called()
        start.assert_called_once_with(wait_up_to=5.0)
        self.assertEqual(resp["mode"], "local")
        self.assertEqual(set_fb.call_args, mock.call("ornith-1.5-9B", False))

    def test_no_remote_starts_local(self) -> None:
        """Pas de remote (ni `.env-secret`, ni `config.json → fallback`) → local."""
        resp, start, _mark, set_fb, stop, wait_free = self._run(
            fb_ok=False, local_up=False, remote_set=False)
        stop.assert_not_called()
        wait_free.assert_not_called()
        start.assert_called_once_with(wait_up_to=5.0)
        self.assertEqual(resp["mode"], "local")
        self.assertEqual(set_fb.call_args, mock.call("ornith-1.5-9B", False))


class EnvSecretLoaderTest(unittest.TestCase):
    """fallback_endpoint()/fallback_api_key() : le `.env-secret` prime sur
    `config.json → fallback` (lecteur mocké, aucun fichier réel nécessaire)."""

    SECRET = {
        "OPENAI_ENDPOINT": "https://llm-serve.lab.fr/v1",
        "OPENAI_API_KEY": "sk-env-secret",
    }
    CONFIG = {"endpoint": "http://192.168.1.20:8080", "api_key": "cfg-key"}

    def _resolve(self, secret: dict | None, config_values: dict | None) -> tuple:
        with (
            mock.patch("tutor.config._env_secret", return_value=secret or {}),
            mock.patch("tutor.config._CONFIG",
                       {"fallback": config_values or {"endpoint": "", "api_key": ""}}),
        ):
            return config.fallback_endpoint(), config.fallback_api_key()

    def test_env_secret_wins_over_config(self) -> None:
        endpoint, key = self._resolve(self.SECRET, self.CONFIG)
        self.assertEqual(endpoint, self.SECRET["OPENAI_ENDPOINT"])
        self.assertEqual(key, self.SECRET["OPENAI_API_KEY"])

    def test_config_used_without_env_secret(self) -> None:
        endpoint, key = self._resolve(None, self.CONFIG)
        self.assertEqual(endpoint, self.CONFIG["endpoint"])
        self.assertEqual(key, self.CONFIG["api_key"])

    def test_empty_when_neither_configured(self) -> None:
        endpoint, key = self._resolve(None, {"endpoint": "", "api_key": ""})
        self.assertIsNone(endpoint)
        self.assertIsNone(key)

    def test_partial_env_secret_falls_back_per_field(self) -> None:
        # Seul l'endpoint est dans .env-secret → la clef retombe sur config.json.
        endpoint, key = self._resolve(
            {"OPENAI_ENDPOINT": "https://other.host/v1"}, self.CONFIG
        )
        self.assertEqual(endpoint, "https://other.host/v1")
        self.assertEqual(key, self.CONFIG["api_key"])

    def test_read_env_secret_real_parser(self) -> None:
        """Le parser lit un vrai fichier : commentaires, ligne vide, guillemets."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".env-secret"
            path.write_text(
                "# commentaire\n"
                "OPENAI_ENDPOINT=https://llm.lab/v1\n"
                'OPENAI_API_KEY="sk-quoted"\n\n',
                encoding="utf-8",
            )
            with mock.patch("tutor.config.ENV_SECRET_PATH", path):
                self.assertEqual(
                    config._read_env_secret(),
                    {"OPENAI_ENDPOINT": "https://llm.lab/v1",
                     "OPENAI_API_KEY": "sk-quoted"},
                )


if __name__ == "__main__":
    unittest.main()
