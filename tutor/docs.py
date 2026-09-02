"""Serveur statique local du book public (doc cliquable).

Sert ``corpus/www/`` — copie des pages HTML rendues du book public — sur
``127.0.0.1:8765`` par défaut. C'est un simple ``http.server`` stdlib, **rien à
voir** avec le routeur llama 8025 (``server.ensure`` gère celui-là ; ne jamais
le lancer/arrêter soi-même).

L'engine réécrit les citations ``fichier:ligne`` en liens
``BASE/chemin.html#ancre`` (``tutor.docslinks``) ; ces liens ne sont cliquables
que si ce serveur tourne. ``ensure()`` est idempotent : port déjà servi → rien
à faire (plusieurs agents peuvent cohabiter, chaque session ACP peut relancer).
En mode STUB (tests unitaires), aucune socket n'est ouverte, tout est neutre.
"""

from __future__ import annotations

import functools
import os
import socket
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from . import config


class _QuietHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler sans log sur stderr (bruit en session)."""

    def log_message(self, fmt: str, *args: object) -> None:
        pass


def _probe(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def serve(host: str = "127.0.0.1", port: int = 8765,
          directory: str | None = None) -> ThreadingHTTPServer:
    """Monte (et renvoie) le serveur HTTP ; à faire tourner en thread."""
    handler = functools.partial(_QuietHandler, directory=directory)
    return ThreadingHTTPServer((host, port), handler)


def ensure(host: str | None = None, port: int | None = None,
           directory: str | None = None) -> dict[str, str]:
    """S'assure que les docs sont servies ; renvoie un statut descriptif.

    - port déjà servi → ``ok`` (idempotent, ne relance rien) ;
    - ``corpus/www/`` absent (livrable sans book) → ``absent``, rien ne tourne ;
    - sinon → lance le serveur en thread et renvoie ``ok``.
    """
    if config.STUB:
        return {"status": "ok", "detail": "docs (mode STUB) : non servies"}
    host = host or "127.0.0.1"
    port = int(port or config.docs_port())
    directory = directory or config.www_dir()
    if _probe(host, port):
        return {"status": "ok", "detail": f"docs déjà servies sur http://{host}:{port}"}
    if not os.path.isdir(directory):
        return {"status": "absent",
                "detail": f"pas de www ({directory}) — doc non servie"}
    server = serve(host, port, directory)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return {"status": "ok",
            "detail": f"docs servies sur http://{host}:{port} (dossier {directory})"}
