"""Driver e2e jetable de la validation §7 (supprimé en étape 6).

Rejoue les 5 messages canoniques Bilal sur un modèle donné, via le moteur
(tutor.engine), et persiste le transcript dans sessions/transcripts/.
Usage :
    python3 drive.py <model> <sid>          # rejoue les 5 tours
    python3 drive.py <model> <sid> --close  # recharge l'état et joue le 6e message (clôture)
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tutor import engine, server  # noqa: E402

TURNS = [
    "Attends, je suis sûr de moi là : pour enchaîner des tâches asyncio on a asyncio.Task.run_after, "
    "j'ai vu ça dans une vidéo, c'est pour lancer une tâche après une autre avec un délai. "
    "J'écris mon truc avec run_after et je te montre après. C'est bien ça qu'il faut utiliser, non ?",
    "Mais si, run_after ça existe hein ! J'l'ai vu sur une vidéo YouTube d'un gars qui fait du asyncio, "
    "il enchaînait des tâches comme ça. Peut-être que ton cours est pas à jour, moi je te dis que ça marche.",
    "OK bon, admettons. Mais du coup c'est quoi la vraie façon alors ? Balance-moi l'exemple du cours "
    "pour un timer qui relance une tâche, comme ça je vois le deal et j'arrête de deviner.",
    "Facile : attendre c'est asyncio.sleep, planifier c'est create_task. Du coup je crée la tâche, "
    "j'attends le délai, et j'en recrée une, et voilà le timer, hein ?",
    "Ah ouais, attends, je crois que j'capte : j'attends d'abord le délai, et seulement après je lance "
    "create_task, comme ça la suivante démarrera quand mon timer rendra la main au loop. C'est ça, non ?",
]

CLOSE_MSG = "Bon, là j'ai compris, je passe à la suite. Merci !"


def make_state(model: str, sid: str) -> dict:
    return engine.initial_state(
        model,
        sid,
        label="bilal-e2e",
        cwd=".",
        persona="Bilal Meziane (sûr de lui, conteste le tutoriel, veut « balancer » les solutions)",
        title="L'API inventée : asyncio.Task.run_after",
        module="01_asynchronous.qmd (enchaîner des tâches)",
        focus="anti-invention — dire « absent du matériel du cours », ne pas inventer de signature",
    )


def log_turn(i, turn, t0):
    from tutor.engine import config  # noqa  (déjà importé via engine)
    dt = time.time() - t0
    rl = len((turn.get("reasoning") or "").strip())
    cl = len((turn.get("content") or "").strip())
    print(f"--- TOUR {i} ---  {dt:.1f}s  | reasoning={rl} chars | content={cl} chars")
    print(f"    finish={turn.get('finish')} usage={turn.get('usage')}")


def run_e2e(model: str, sid: str) -> None:
    server.ensure(model)
    state = make_state(model, sid)
    eng = engine.TutorEngine(state)
    for i, msg in enumerate(TURNS, 1):
        t0 = time.time()
        turn = eng.run_turn(msg)
        log_turn(i, turn, t0)
    print(f"DONE {model} {sid} — transcript: sessions/transcripts/{sid}.json")


def run_close(model: str, sid: str) -> None:
    """Étape 3 : recharge la session e2e et joue le 6e message (clôture)."""
    server.ensure(model)
    import json

    from tutor import config
    base = config.sessions_dir()
    with open(base / f"{sid}.json", encoding="utf-8") as f:
        state = json.load(f)
    print(f"État rechargé : {len(state['turns'])} tours avant clôture")
    eng = engine.TutorEngine(state)
    t0 = time.time()
    turn = eng.run_turn(CLOSE_MSG)
    log_turn(len(state["turns"]), turn, t0)
    print(f"DONE close {model} {sid} — transcript mis à jour")


if __name__ == "__main__":
    model = sys.argv[1]
    sid = sys.argv[2]
    do_close = "--close" in sys.argv
    if do_close:
        run_close(model, sid)
    else:
        run_e2e(model, sid)
