"""CLI d'exploitation du tuteur (backend + agent) — §4.

Sous-commandes :
  start <model> [--wait N]   s'assure que le routeur llama.cpp sert <model>
                             (démarre le routeur au besoin, précharge le modèle
                             par défaut ; AUCUN kill au switch — routing par
                             requête ; profils distants → simple ping de
                             l'endpoint, aucun serveur local).
  status                     état du port + alias servis + provenance.
  stop                       arrête proprement le llama-server routeur géré.
  agent                      lance l'agent ACP sur stdio (transport + moteur +
                             backend) — équivalent de ``acp_agent.py``.

Exemples :
  python3 run.py start ornith-1.5-9B
  python3 run.py status
  python3 run.py stop
"""
from __future__ import annotations

import argparse
import sys

from tutor import server


def _cmd_start(args: argparse.Namespace) -> int:
    try:
        result = server.ensure(args.model, wait_up_to=args.wait)
    except server.ServerError as e:
        print(f"erreur: {e}", file=sys.stderr)
        return 1
    tag = "OK" if result["status"] == "ok" else "ATTENTION"
    print(f"[{tag}] {result['detail']}")
    return 0 if result["status"] == "ok" else 2


def _cmd_status(_args: argparse.Namespace) -> int:
    s = server.status()
    if s["healthy"]:
        served = ", ".join(s["served_aliases"]) or "(inconnu)"
        managed = "géré" if s["managed"] else ("adopté" if s["current_model"] else "non géré")
        print(f"port {s['port']}: OK (alias: {served}; {managed})")
    else:
        print(f"port {s['port']}: libre (aucun serveur)")
    return 0


def _cmd_stop(_args: argparse.Namespace) -> int:
    print(server.stop())
    return 0


def _cmd_agent(_args: argparse.Namespace) -> int:
    from acp_agent import main as agent_main

    agent_main()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="run",
        description="CLI d'exploitation du tuteur socratique (llama-server + agent ACP).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="démarre/assure le serveur pour un modèle")
    p_start.add_argument("model", help="qwen3.5-4B | ornith-1.5-9B | ministral-3-8B-Reasoning | gemma-4-E4B")
    p_start.add_argument("--wait", type=float, default=180.0, help="délai d'attente (s)")
    p_start.set_defaults(func=_cmd_start)

    sub.add_parser("status", help="état du port et du modèle servi").set_defaults(func=_cmd_status)
    sub.add_parser("stop", help="arrête le llama-server géré").set_defaults(func=_cmd_stop)
    sub.add_parser("agent", help="lance l'agent ACP sur stdio").set_defaults(func=_cmd_agent)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
