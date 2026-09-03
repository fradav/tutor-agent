"""Serveur statique local du book public + doc Python (docs cliquables).

Sert ``www`` (``corpus/www/`` — copie des pages HTML rendues du book public) à
la racine ``/`` et, si ``docs.py_dir`` est configuré, le miroir local de la doc
Python officielle sous ``/py/`` — sur ``127.0.0.1:8765`` par défaut. C'est un
simple ``http.server`` stdlib, **rien à voir** avec le routeur llama 8025
(``server.ensure`` gère celui-là ; ne jamais le lancer/arrêter soi-même).

L'engine réécrit les citations ``fichier:ligne`` et ``python:<ref>`` en liens
markdown (``tutor.docslinks``) ; ces liens ne sont cliquables que si ce serveur
tourne — et au bon port. ``ensure()`` est idempotent : port déjà servi par
**notre** serveur (racine correcte) → rien à faire (plusieurs agents peuvent
cohabiter, chaque session ACP peut relancer). Si le port configuré est pris par
un autre processus ou par un ancien serveur dont la racine n'est plus la bonne,
``ensure()`` se relance sur un port libre voisin et mémorise la base réelle
(``effective_base_url()``) que ``tutor.docslinks`` utilise pour réécrire les
liens. Le serveur répond au marqueur ``/__tutor_docs__`` pour qu'``ensure()``
distingue « notre serveur » d'un squatteur de port. En mode STUB (tests
unitaires), aucune socket n'est ouverte, tout est neutre.
"""

from __future__ import annotations

import os
import socket
import threading
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import config


# Base effective : port de repli éventuel (configured port squatté par un autre
# processus) ; ``None`` → base de config. Posée par ``ensure()``, lue par
# ``tutor.docslinks`` (via ``effective_base_url()``) pour que les liens pointent
# vers le serveur réellement lancé.
_effective_base_url: str | None = None

# Marqueur : ``GET /__tutor_docs__`` → 200 "tutor-docs". Sert à vérifier qu'un
# port occupé est bien un de nos serveurs docs (et pas, ex., Betterbird).
_MARKER = "/__tutor_docs__"
_MARKER_BODY = b"tutor-docs\n"


def _make_handler(www: str, py: str):
    """Handler du serveur docs : ``www`` à la racine, ``py`` sous ``/py/``.

    ``py`` vide → pas de route ``/py/`` (le racine www gère les 404). Classe
    construite dynamiquement : ``SimpleHTTPRequestHandler`` attend ``directory``
    en kwarg à l'init, on fournit le racine www ; ``translate_path`` bascule
    vers ``py`` quand l'URI commence par ``/py/``.
    """

    class _DocsHandler(SimpleHTTPRequestHandler):
        py_directory = py

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=www, **kwargs)

        def do_GET(self) -> None:
            if self.path == _MARKER:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(_MARKER_BODY)))
                self.end_headers()
                self.wfile.write(_MARKER_BODY)
                return
            return super().do_GET()

        def translate_path(self, path: str) -> str:
            if self.py_directory and path.startswith("/py/"):
                path = path[len("/py/"):]
                self.directory = self.py_directory
            return super().translate_path(path)

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

    return _DocsHandler


def _probe(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def effective_base_url() -> str:
    """Base réelle du serveur docs : port de repli éventuel, sinon config."""
    return _effective_base_url or config.docs_base_url()


def _set_effective_base(base: str | None = None) -> None:
    global _effective_base_url
    _effective_base_url = base


def _serves_marker(host: str, port: int, timeout: float = 0.5) -> bool:
    """Le service sur ``host:port`` répond-il au marqueur de nos serveurs docs ?

    Répond True pour un de nos serveurs (même à racine obsolète), False pour un
    autre processus qui aurait pris le port.
    """
    try:
        with urllib.request.urlopen(
                f"http://{host}:{port}{_MARKER}", timeout=timeout) as r:
            return r.status == 200 and r.read(32).startswith(_MARKER_BODY.strip())
    except OSError:
        return False


def _get_status(host: str, port: int, path: str,
                timeout: float = 0.5) -> int | None:
    """Code HTTP pour ``path`` sur ``host:port`` (``None`` si pas de réponse)."""
    try:
        with urllib.request.urlopen(
                f"http://{host}:{port}{path}", timeout=timeout) as r:
            return r.status
    except OSError:
        return None


def _sample_page(www: str) -> str | None:
    """Premier ``*.html`` relatif sous ``www``, pour vérifier la racine servie."""
    if not www or not os.path.isdir(www):
        return None
    for root, _dirs, files in os.walk(www):
        for name in sorted(files):
            if name.endswith(".html"):
                rel = os.path.relpath(os.path.join(root, name), www)
                return rel.replace(os.sep, "/")
    return None


def serve(host: str = "127.0.0.1", port: int = 8765,
          directory: str | None = None,
          py_directory: str | None = None) -> ThreadingHTTPServer:
    """Monte (et renvoie) le serveur HTTP ; à faire tourner en thread.

    ``directory`` = racine www (signature historiques, tests) ; ``py_directory``
    = doc Python locale servie sous ``/py/`` (None → ``config.py_dir()``).
    """
    www = directory or config.www_dir()
    py = py_directory if py_directory is not None else config.py_dir()
    handler = _make_handler(www, py)
    return ThreadingHTTPServer((host, port), handler)


def _served_desc(www: str, py: str) -> str:
    parts = [f"www → {www}"]
    if py and os.path.isdir(py):
        parts.append(f"/py/ → {py}")
    return ", ".join(parts)


def _bind_server(host: str, port: int, www: str, py: str) -> tuple[ThreadingHTTPServer, int]:
    """Monte le serveur sur ``port`` (0 = éphémère) et le lance en thread.

    Retourne (serveur, port effectif). Lève ``OSError`` si le bind échoue
    (port occupé par un autre processus).
    """
    server = serve(host, port, www, py)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


def _start_on_free_port(host: str, configured: int, www: str, py: str) -> int:
    """Démarre le serveur docs sur un port libre proche de ``configured``
    (puis éphémère) ; renvoie le port effectivement pris.
    """
    for cand in [configured + i for i in range(1, 6)] + [0]:
        try:
            _server, used = _bind_server(host, cand, www, py)
            return used
        except OSError:
            continue
    raise RuntimeError("aucun port libre pour le serveur docs")


def _adopt_or_start_free(host: str, configured: int, www: str, py: str) -> int:
    """Repli du port configuré squatté : réadopte le serveur docs déjà présent
    sur un port voisin (cohabitation de plusieurs sessions ACP), sinon en lance
    un neuf. Renvoie le port de repli à utiliser.
    """
    sample = _sample_page(www)
    for cand in [configured + i for i in range(1, 6)]:
        if (_serves_marker(host, cand)
                and sample
                and _get_status(host, cand, f"/{sample}") == 200):
            return cand
    return _start_on_free_port(host, configured, www, py)


def ensure(host: str | None = None, port: int | None = None,
           directory: str | None = None,
           py_directory: str | None = None) -> dict[str, str]:
    """S'assure que les docs sont servies ; renvoie un statut descriptif.

    - port déjà servi par NOTRE serveur avec la bonne racine → ``ok``
      (idempotent, ne relance rien) ;
    - port configuré pris par un autre processus, ou par un de nos serveurs à
      racine obsolète → serveur relancé sur un port libre voisin, base réelle
      mémorisée (``effective_base_url()``) ;
    - ni www ni doc Python locale disponibles → ``absent``, rien ne tourne ;
    - sinon → lance le serveur en thread sur le port configuré et renvoie ``ok``.
    """
    if config.STUB:
        return {"status": "ok", "detail": "docs (mode STUB) : non servies"}
    host = host or "127.0.0.1"
    configured = int(port or config.docs_port())
    www = directory or config.www_dir()
    py = py_directory if py_directory is not None else config.py_dir()
    if not os.path.isdir(www) and not (py and os.path.isdir(py)):
        _set_effective_base()
        return {"status": "absent",
                "detail": f"pas de www ({www}) ni de doc Python ({py}) — doc non servie"}
    if _probe(host, configured):
        # Port occupé : est-ce bien NOTRE serveur, avec la bonne racine ?
        sample = _sample_page(www)
        marker_ok = _serves_marker(host, configured)
        root_ok = bool(sample) and _get_status(host, configured, f"/{sample}") == 200
        if marker_ok and root_ok:
            _set_effective_base()
            return {"status": "ok",
                    "detail": f"docs déjà servies sur http://{host}:{configured}"}
        cause = ("un ancien serveur docs à la racine obsolète" if marker_ok
                 else "un autre processus (pas le serveur docs)")
        used = _adopt_or_start_free(host, configured, www, py)
        _set_effective_base(f"http://{host}:{used}")
        return {"status": "ok",
                "detail": (f"port {configured} occupé par {cause} → docs servies "
                           f"sur http://{host}:{used} ({_served_desc(www, py)})")}
    _set_effective_base()
    _bind_server(host, configured, www, py)
    return {"status": "ok",
            "detail": f"docs servies sur http://{host}:{configured} ({_served_desc(www, py)})"}
