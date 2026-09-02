"""Validation §7 — robustesse « corpus introuvable ».

Monkeypatch tutor.config._CONFIG["paths"]["corpus_root"] vers /tmp/absent-xyz
puis run_turn("attends…") avec qwen3.5-4B (serveur qwen3.5-4B attendu déjà démarré par
ailleurs).
But : aucun traceback, le contexte visible dit « no match »/introuvable (pas un crash).
"""
from __future__ import annotations
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tutor import config, engine  # noqa: E402

TURN = (
    "Attends, je suis sûr de moi là : pour enchaîner des tâches asyncio on a "
    "asyncio.Task.run_after, j'ai vu ça dans une vidéo, c'est pour lancer une "
    "tâche après une autre avec un délai. J'écris mon truc avec run_after et je "
    "te montre après. C'est bien ça qu'il faut utiliser, non ?"
)


def main() -> int:
    config._CONFIG["paths"]["corpus_root"] = "/tmp/absent-xyz"
    print("corpus_root patched →", config.corpus_root())
    state = engine.initial_state(
        "qwen3.5-4B", "switch-check-corpus",
        label="bilal-e2e", cwd=".",
        persona="Bilal Meziane (sûr de lui, conteste le tutoriel)",
        title="L'API inventée : asyncio.Task.run_after",
        module="01_asynchronous.qmd (enchaîner des tâches)",
        focus="anti-invention",
    )
    eng = engine.TutorEngine(state)
    turn = eng.run_turn(TURN)
    rl = len((turn.get("reasoning") or "").strip())
    cl = len((turn.get("content") or "").strip())
    print(f"--- TOUR --- | reasoning={rl} chars | content={cl} chars | finish={turn.get('finish')}")
    print("TOOLS:", __import__("json").dumps(turn.get("tools"), ensure_ascii=False))
    print("CONTENT_PREVIEW:", (turn.get("content") or "").strip()[:600].replace("\n", " "))
    print("RESULT:", "PASS (aucun traceback)" if turn.get("content") else "CHECK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
