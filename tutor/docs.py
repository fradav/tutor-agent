"""Serveur statique local du book public + doc Python (docs cliquables).

Sert ``www`` (``corpus/www/`` — copie des pages HTML rendues du book public) à
la racine ``/`` et, si ``docs.py_dir`` est configuré, le miroir local de la doc
Python officielle sous ``/py/`` — sur ``127.0.0.1:8765`` par défaut. C'est un
simple ``http.server`` stdlib, **rien à voir** avec le routeur llama 8025
(``server.ensure`` gère celui-là ; ne jamais le lancer/arrêter soi-même).

L'engine réécrit les citations ``fichier:ligne`` et ``python:<ref>`` en liens
markdown (``tutor.docslinks``) ; ces liens ne sont cliquables que si ce serveur
tourne. ``ensure()`` est idempotent : port déjà servi → rien à faire (plusieurs
agents peuvent cohabiter, chaque session ACP peut relancer). En mode STUB
(tests unitaires), aucune socket n'est ouverte, tout est neutre.
"""

from __future__ import annotations

import os
import socket
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import config


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


def ensure(host: str | None = None, port: int | None = None,
           directory: str | None = None,
           py_directory: str | None = None) -> dict[str, str]:
    """S'assure que les docs sont servies ; renvoie un statut descriptif.

    - port déjà servi → ``ok`` (idempotent, ne relance rien) ;
    - ni www ni doc Python locale disponibles → ``absent``, rien ne tourne ;
    - sinon → lance le serveur en thread et renvoie ``ok``.
    """
    if config.STUB:
        return {"status": "ok", "detail": "docs (mode STUB) : non servies"}
    host = host or "127.0.0.1"
    port = int(port or config.docs_port())
    www = directory or config.www_dir()
    py = py_directory if py_directory is not None else config.py_dir()
    if _probe(host, port):
        return {"status": "ok", "detail": f"docs déjà servies sur http://{host}:{port}"}
    if not os.path.isdir(www) and not (py and os.path.isdir(py)):
        return {"status": "absent",
                "detail": f"pas de www ({www}) ni de doc Python ({py}) — doc non servie"}
    server = serve(host, port, www, py)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    served = [f"www → {www}"]
    if py and os.path.isdir(py):
        served.append(f"/py/ → {py}")
    return {"status": "ok",
            "detail": f"docs servies sur http://{host}:{port} ({', '.join(served)})"}
