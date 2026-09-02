"""Validation §7 — session/cancel → stop_reason="cancelled".

Mode STUB (TUTOR_STUB=1) + TutorAgent in-process avec un faux conn qui enregistre
les session_update (contrat protocol.py). Lance prompt() dans une tâche asyncio,
cancel() pendant l'émission, vérifie stop_reason et l'absence d'exception.
"""
from __future__ import annotations
import asyncio
import os
import sys

os.environ["TUTOR_STUB"] = "1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import acp  # noqa: E402
from acp.schema import AgentThoughtChunk  # noqa: E402
from protocol import TutorAgent  # noqa: E402


class _FakeConn:
    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.thoughts: list[str] = []
        self.updates = 0

    async def session_update(self, session_id: str, update, **kwargs) -> None:
        self.updates += 1
        text = getattr(getattr(update, "content", None), "text", None)
        if isinstance(text, str) and text:
            (self.thoughts if isinstance(update, AgentThoughtChunk) else self.chunks).append(text)


async def main() -> int:
    agent = TutorAgent()
    conn = _FakeConn()
    agent.on_connect(conn)
    sid = (await agent.new_session(cwd=".")).session_id
    print("sid:", sid)
    prompt_task = asyncio.create_task(
        agent.prompt(sid, [acp.text_block("Une réponse très longue que je vais annuler.")])
    )
    # Le stub émet d'abord le raisonnement, puis 3 blocs contenu (cadence 0.05 s).
    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline and len(conn.chunks) < 1:
        await asyncio.sleep(0.01)
    await agent.cancel(sid)
    resp = await prompt_task
    print("stop_reason:", resp.stop_reason)
    print(f"updates={conn.updates} chunks={len(conn.chunks)} thoughts={len(conn.thoughts)}")
    ok = resp.stop_reason == "cancelled"
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
