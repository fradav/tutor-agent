"""Backend llama-server — mode ROUTEUR (§4/§5, décision encadrant).

Un seul llama-server **routeur** sur le port dédié de ``config.json`` (section
``server``), qui sert les 4 profils locaux sans kill/relance au switch du
``.tutor-model`` : la ligne de commande porte ``--models-preset`` (fichier INI
généré depuis ``config.json``) et ``--models-max 1`` (un seul modèle chargé à la
fois, déchargé/rechargé à la demande selon le champ ``model`` du body).

Le preset INI reproduit la règle du harnais par profil :
  [*]  jinja = true ; reasoning-preserve = true           (global)
  [qwen3.5-4B]  model = <Qwen3.5-…Q8_K_XL.gguf> +
                chat-template-file = qwen3.5-chat-template.jinja (EXTERNE)
  [ornith-1.5-9B]  model = <Ornith-…Q4_K_M.gguf>          (EMBARQUÉ)
  [ministral-3-8B-Reasoning]
                model = <Ministral-…Q4_K_M.gguf>          (EMBARQUÉ)
  [gemma-4-E4B]  model = <gemma-4-E4B_q4_0-it.gguf>       (EMBARQUÉ)
Chaque section : ``c = <max_tokens>`` (32768), ``n-gpu-layers = 99``,
``load-on-startup = true`` pour le modèle par défaut seulement.

Le routing se fait sur le champ ``model`` du body (l'**alias** du profil) :
``llm.py`` envoie déjà ``body["model"]=alias`` et le moteur passe ``prof["alias"]``
→ aucun changement moteur/llm nécessaire.

Profils distants (``config.is_remote``) : aucune gestion de llama-server local —
le serveur distant existe déjà ; ``ensure`` ne fait qu'en vérifier la santé. Si
TOUS les profils sont distants, aucun routeur n'est démarré.

Fichiers runtime dans ``Tutor-agent/servers/`` :
  server.pid          pid du llama-server routeur (démarré par ce module)
  server.log          stdout/stderr du serveur (debug)
  current_model       dernier alias demandé (géré ou adopté)
  models-router.ini   preset généré (traçabilité)

``ensure(model)`` est la fonction d'entrée : pour un profil local et un routeur
déjà up, elle **n'adopte que si l'alias est servi par le preset** et ne redémarre
jamais le serveur (PAS de kill au switch) ; s'il n'y a pas de routeur, elle le
démarre une fois.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config


class ServerError(RuntimeError):
    """Erreur attendue de gestion du serveur (message prêt à afficher)."""


# Deux ``ensure`` concurrents (p. ex. deux ``session/new`` simultanés) ne doivent
# pas se marcher dessus : le verrou sérialise la décision (démarrage ou adoption)
# et l'éventuel flux d'attente. ``ensure`` tourne dans un ``to_thread``
# (protocol.new_session) → un ``threading.Lock`` convient (pas de deadlock avec
# la boucle asyncio).
_ensure_lock = threading.Lock()


def servers_dir() -> Path:
    return config.BASE_DIR / "servers"


def _pidfile() -> Path:
    return servers_dir() / "server.pid"


def _logfile() -> Path:
    return servers_dir() / "server.log"


def _current_file() -> Path:
    return servers_dir() / "current_model"


def _preset_file() -> Path:
    return servers_dir() / "models-router.ini"


def _alias(model: str) -> str:
    return config.profile(model).get("alias", model)


# -- preset routeur (INI généré depuis config.json) ---------------------------

def local_profiles() -> list[str]:
    """Clefs profil servies par le routeur local (``endpoint`` vide)."""
    return [m for m in config.profiles() if not config.is_remote(m)]


def _preset_aliases() -> list[str]:
    """Les alias présents dans le preset routeur (source de vérité du routing)."""
    return [_alias(m) for m in local_profiles()]


def render_preset() -> str:
    """Texte INI du preset routeur — une section par profil local.

    Le nom de section = **alias** (c'est ce que llama.cpp fait correspondre au
    champ ``model`` du body). ``chat-template-file`` n'est défini que pour
    qwen3.5-4B (template externe) ; ornith-1.5-9B / ministral-3-8B-Reasoning /
    gemma-4-E4B gardent le template embarqué du GGUF.
    ``load-on-startup`` ne précharge que le modèle par défaut de config.json.
    """
    default = config.default_model()
    lines = ["version = 1", ""]
    lines += ["[*]", "jinja = true", "reasoning-preserve = true", ""]
    for model in local_profiles():
        prof = config.profile(model)
        alias = _alias(model)
        lines += [f"[{alias}]"]
        lines += [f"model = {config.model_path(model)}"]
        lines += [f"c = {config.max_tokens()}"]
        lines += ["n-gpu-layers = 99"]
        if prof.get("template") == "external":
            lines += [f"chat-template-file = {config.external_template()}"]
        lines += [f"load-on-startup = {'true' if model == default else 'false'}"]
        lines += [""]
    return "\n".join(lines).strip() + "\n"


def write_preset() -> Path:
    """Écrit le preset généré dans ``servers/models-router.ini`` et le retourne."""
    servers_dir().mkdir(parents=True, exist_ok=True)
    path = _preset_file()
    path.write_text(render_preset(), encoding="utf-8")
    return path


def _router_cmd() -> list[str]:
    """Ligne de commande du llama-server ROUTEUR (UNE instance pour les 4 modèles).

    Pas de ``--model`` ni de flags par modèle ici : tout est décrit dans le preset
    INI (``--models-preset``). ``--models-max 1`` garde un seul modèle chargé, le
    switch se joue via le champ ``model`` du body (routing à la demande).
    """
    cfg = config.load_config()
    return [
        config.llama_bin(),
        "--host", cfg["server"]["host"],
        "--port", str(cfg["server"]["port"]),
        "--models-preset", str(write_preset()),
        "--models-max", "1",
    ]


# -- état / sondes ------------------------------------------------------------

def is_managed() -> bool:
    """Un pidfile existe et pointe vers un processus vivant (notre routeur)."""
    try:
        pid = int(_pidfile().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # vivant, mais lancé par un autre utilisateur


def current_model() -> str | None:
    """Dernier alias demandé (d'après current_model), si connu."""
    try:
        value = _current_file().read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def health_ok(base_url: str | None = None, timeout: float = 2.0) -> bool:
    """Le health check de llama.cpp répond 200 (modèle chargé et prêt).

    ``base_url`` par défaut = serveur local de config.json ; pour un profil
    distant, passer ``config.model_base_url(model)``. Le routeur répond 503
    pendant un chargement de modèle → ici ``False``.
    """
    base = base_url or config.base_url()
    try:
        with urllib.request.urlopen(base + "/health", timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False  # 503 → en cours de chargement → pas prêt
    except Exception:
        return False


def _port_busy(timeout: float = 1.0) -> bool:
    """Quelque chose répond déjà sur le port (même 503 : un serveur est là)."""
    try:
        with urllib.request.urlopen(config.base_url() + "/health", timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _server_aliases(base_url: str | None = None, timeout: float = 4.0) -> list[str]:
    """Alias listés par llama.cpp (via /v1/models). Vide si illisible.

    Attention : en mode routeur, /v1/models liste aussi le cache HF — filtrer sur
    ``_preset_aliases()`` avant d'interpréter (cf. ``status``).
    """
    base = base_url or config.base_url()
    try:
        req = urllib.request.Request(base + "/v1/models")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        return [e.get("id") for e in data.get("data", []) if isinstance(e, dict) and e.get("id")]
    except Exception:
        return []


def _mark_alias(model: str) -> None:
    servers_dir().mkdir(parents=True, exist_ok=True)
    _current_file().write_text(_alias(model), encoding="utf-8")


def _tail(path: str, n: int = 40) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return "(log illisible)"
    return "".join(lines[-n:]).rstrip()


# -- cycle de vie -------------------------------------------------------------

def _wait_port_free(wait_up_to: float = 15.0) -> None:
    """Attend que le port ne réponde plus (après ``stop``, avant ``start``).

    Sans cette attente, ``start`` pourrait rebondir sur l'ancien processus en
    train de libérer la socket (le health check répondrait encore « ok »).
    """
    deadline = time.time() + wait_up_to
    while time.time() < deadline:
        if not _port_busy(timeout=0.5):
            return
        time.sleep(0.5)


def _adopt_or_refresh(model: str, wait_up_to: float) -> dict:
    """Routeur géré déjà up : adopte si les alias du preset sont servis.

    Si le routeur tourne mais ne sert **aucun** alias du preset actuel, c'est un
    preset obsolète (config.json modifié après le démarrage — renommage d'alias,
    changement de template). llama.cpp ne lit le preset INI qu'au démarrage → on
    régénère le preset et on redémarre **une fois**. Cas normal (alias servis) :
    adoption sans kill (PAS de redémarrage au switch de modèle).
    """
    alias = _alias(model)
    if set(_server_aliases()) & set(_preset_aliases()):
        _mark_alias(model)
        return {
            "model": model, "alias": alias, "mode": "local",
            "status": "ok", "detail": "routeur actif (alias servi à la demande)",
        }
    stop()
    _wait_port_free()
    started = start(wait_up_to=wait_up_to)
    _mark_alias(model)
    return {
        "model": model, "alias": alias, "mode": "local",
        **started,
        "status": "ok",
        "detail": "preset obsolète régénéré, routeur redémarré (alias: "
                  + ", ".join(_preset_aliases()) + ")",
    }


def ensure(model: str, wait_up_to: float = 180.0) -> dict:
    """S'assure qu'un backend répond pour ``model`` ; renvoie un dict d'état.

    - Profil **distant** : aucun llama-server local ; on ne fait que pinger
      l'endpoint (``config.model_base_url``).
    - Profil **local** (mode routeur) :
        * routeur déjà up (géré ou étranger compatible) → adopté, **aucun kill**
          tant que les alias du preset sont servis ; un routeur géré au preset
          obsolète est régénéré + redémarré une fois (``_adopt_or_refresh``) ;
        * notre routeur tourne mais un modèle est en cours de chargement (503) →
          on attend, sans redémarrer ;
        * serveur NON géré qui ne sert aucun alias du preset → erreur explicite ;
        * pas de serveur → démarre le routeur une fois et attend la disponibilité.
    Lève ``ServerError`` sur tout cas bloquant (log en cas d'échec).
    """
    alias = _alias(model)
    if config.is_remote(model):
        url = config.model_base_url(model)
        return {
            "model": model, "alias": alias, "mode": "remote",
            "status": "ok" if health_ok(url) else "unreachable",
            "detail": f"endpoint distant {url} — aucun serveur local géré",
        }

    with _ensure_lock:
        if health_ok():
            if is_managed():
                return _adopt_or_refresh(model, wait_up_to)
            # Un serveur répond mais n'est pas géré par nous (pidfile perdu /
            # lancé hors de ce module) : on ne l'adopte que s'il sert au moins un
            # alias du preset (routeur compatible).
            if set(_server_aliases()) & set(_preset_aliases()):
                _mark_alias(model)
                return {
                    "model": model, "alias": alias, "mode": "local",
                    "status": "ok", "detail": "routeur étranger compatible (adopté)",
                }
            raise ServerError(
                f"un serveur NON géré répond sur {config.base_url()} mais ne sert "
                f"aucun alias du preset ({_preset_aliases()}). "
                "Arrêtez-le manuellement, puis relancez `start <model>`.")
        if is_managed():
            # Notre routeur tourne mais charge un modèle (503) : attendre, sans
            # redémarrer (PAS de kill au switch).
            deadline = time.time() + wait_up_to
            while time.time() < deadline:
                if health_ok():
                    _mark_alias(model)
                    return {
                        "model": model, "alias": alias, "mode": "local",
                        "status": "ok", "detail": "routeur prêt après chargement",
                    }
                time.sleep(1)
            raise ServerError(
                f"le routeur géré ne répond pas après {wait_up_to:.0f}s — voir {_logfile()}")
        if _port_busy():
            raise ServerError(
                f"un serveur NON géré répond déjà sur {config.base_url()} mais pas "
                f"en routeur compatible. Arrêtez-le manuellement, puis relancez "
                "`start <model>`.")
        started = start(wait_up_to=wait_up_to)
        _mark_alias(model)
        return {
            "model": model, "alias": alias, "mode": "local",
            **started,
        }


def start(wait_up_to: float = 180.0) -> dict:
    """Démarre le routeur llama.cpp (preset généré) et attend sa santé."""
    if not local_profiles():
        raise ServerError("tous les profils sont distants — aucun llama-server local à démarrer.")
    servers_dir().mkdir(parents=True, exist_ok=True)
    logfile = str(_logfile())
    preset = write_preset()
    log = open(logfile, "w", encoding="utf-8")
    proc = subprocess.Popen(
        _router_cmd(),
        stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _pidfile().write_text(str(proc.pid), encoding="utf-8")
    deadline = time.time() + wait_up_to
    try:
        while time.time() < deadline:
            if health_ok():
                return {
                    "status": "ok", "pid": proc.pid, "logfile": logfile,
                    "detail": f"routeur démarré pid={proc.pid} (preset {preset.name})",
                }
            if proc.poll() is not None:
                raise ServerError(
                    f"le routeur a quitté immédiatement (pid {proc.pid}) — voir {logfile}\n"
                    + _tail(logfile))
            time.sleep(2)
        raise ServerError(f"timeout après {wait_up_to:.0f}s pour le routeur — voir {logfile}")
    except BaseException:
        for f in (_pidfile(), _current_file()):
            try:
                f.unlink()
            except OSError:
                pass
        raise


def stop() -> str:
    """Arrête proprement le llama-server routeur géré (kill du groupe)."""
    try:
        pid = int(_pidfile().read_text(encoding="utf-8").strip())
    except OSError:
        if health_ok():
            return ("aucun pid enregistré, mais un serveur répond sur le port — "
                    "arrêt manuel nécessaire (lancé hors de ce module).")
        return "aucun serveur géré (pas de pid, port libre)."
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    for f in (_pidfile(), _current_file()):
        try:
            f.unlink()
        except OSError:
            pass
    return f"routeur pid {pid} arrêté."


def status() -> dict:
    """État du port + modèle servi + provenance (géré/adopté/non géré).

    ``served_aliases`` = intersection de /v1/models avec les alias du preset (le
    routeur liste aussi le cache HF en /v1/models) ; si /v1/models est muet, on
    renvoie le preset complet (ce que le routeur PEUT servir).
    """
    ok = health_ok()
    served: list[str] = []
    if ok:
        preset = set(_preset_aliases())
        loaded = [a for a in _server_aliases() if a in preset]
        served = loaded or _preset_aliases()
    return {
        "port": config.load_config()["server"]["port"],
        "healthy": ok,
        "served_aliases": served,
        "current_model": current_model(),
        "managed": is_managed(),
    }
