"""Point d'entrée ACP du tuteur socratique.

Branche ``TutorAgent`` (protocol.py) sur stdio grâce à ``acp.run_agent``, qui
gère la boucle JSON-RPC 2.0 line-delimited (messages JSON sur stdout, logs sur
stderr).

Utilisation :
    python3 acp_agent.py

Côté Zed (settings.json) :
    "agent_servers": {"tuteur": {"type": "custom",
                                 "command": ["python3", "/chemin/Tutor-agent/acp_agent.py"]}}
"""

from __future__ import annotations

import asyncio
import logging
import sys

import acp

from protocol import TutorAgent


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(acp.run_agent(TutorAgent()))  # type: ignore[arg-type]  # duck-typé vs acp.Agent (voir protocol.py)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
