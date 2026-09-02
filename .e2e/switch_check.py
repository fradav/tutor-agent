"""Validation §7 — tour de contrôle switch (1 tour persona Bilal) pour un modèle.

Usage: python3 .e2e/switch_check.py <model> <sid>
Tour réel (content non vide requis, finish=stop) via tutor.engine.run_turn.
Même persona / plan d'outils que le drive e2e (drive.py), sid dédié switch-check-*.
"""
from __future__ import annotations
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tutor import engine, server  # noqa: E402

TURN = (
    "Attends, je suis sûr de moi là : pour enchaîner des tâches asyncio on a "
    "asyncio.Task.run_after, j'ai vu ça dans une vidéo, c'est pour lancer une "
    "tâche après une autre avec un délai. J'écris mon truc avec run_after et je "
    "te montre après. C'est bien ça qu'il faut utiliser, non ?"
)


def _chunks(text, n):
    return [text[i:i + n] for i in range(0, len(text), n)]


def main(model: str, sid: str) -> int:
    print(f"[switch_check] model={model} sid={sid}")
    st = server.ensure(model)
    print("ensure:", json_dumps(st))
    state = engine.initial_state(
        model, sid,
        label="bilal-e2e",
        cwd=".",
        persona="Bilal Meziane (sûr de lui, conteste le tutoriel, veut « balancer » les solutions)",
        title="L'API inventée : asyncio.Task.run_after",
        module="01_asynchronous.qmd (enchaîner des tâches)",
        focus="anti-invention — dire « absent du matériel du cours », ne pas inventer de signature",
    )
    eng = engine.TutorEngine(state)
    t0 = time.time()
    turn = eng.run_turn(TURN)
    dt = time.time() - t0
    rl = len((turn.get("reasoning") or "").strip())
    cl = len((turn.get("content") or "").strip())
    print(f"--- TOUR ---  {dt:.1f}s | reasoning={rl} chars | content={cl} chars")
    print(f"finish={turn.get('finish')} usage={turn.get('usage')}")
    print("TOOLS:", json_dumps(turn.get("tools")))
    ok = bool((turn.get("content") or "").strip()) and turn.get("finish") == "stop"
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
