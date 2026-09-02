"""Validation §7 — retry « contenu vide » (engine.run_turn_stream ~L337).

Sans serveur : on override complete_model_stream pour forcer un premier appel
backend à finir vide (finish=stop, contenu "") puis un second retournant du
contenu réel. Attendus : 2 appels backend, contenu final non vide, finish=stop.
"""
from __future__ import annotations
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tutor import engine  # noqa: E402

TURN = (
    "Attends, je suis sûr de moi là : pour enchaîner des tâches asyncio on a "
    "asyncio.Task.run_after, j'ai vu ça dans une vidéo…"
)


def main() -> int:
    state = engine.initial_state(
        "qwen3.5-4B", "switch-check-retry",
        label="bilal-e2e", cwd=".",
        persona="Bilal Meziane (sûr de lui, conteste le tutoriel)",
        title="L'API inventée : asyncio.Task.run_after",
        module="01_asynchronous.qmd (enchaîner des tâches)",
        focus="anti-invention",
    )
    eng = engine.TutorEngine(state)

    fake_text = "La bonne API asyncio pour enchaîner est asyncio.sleep puis create_task — pas Task.run_after."
    fake_chunks = [fake_text[i:i + 8] for i in range(0, len(fake_text), 8)]
    calls = {"n": 0}

    def fake_complete(messages):
        # 5-tuple (dr, dc, fr, us, tool_calls) — signature stream_complete ;
        # tool_calls=None partout (aucun outil demandé par le modèle)
        calls["n"] += 1
        if calls["n"] == 1:                       # 1er appel : contenu vide, finish=stop
            yield (None, "", "stop", {}, None)
            return
        for i, ch in enumerate(fake_chunks):      # 2e appel : contenu réel
            fr = "stop" if i == len(fake_chunks) - 1 else None
            yield (None, ch, fr, {}, None)

    eng.complete_model_stream = fake_complete     # type: ignore[assignment]
    turn = eng.run_turn(TURN)
    cl = len((turn.get("content") or "").strip())
    print(f"appels backend = {calls['n']} (attendu 2)")
    print(f"reasoning={len((turn.get('reasoning') or '').strip())} chars "
          f"| content={cl} chars | finish={turn.get('finish')}")
    ok = calls["n"] == 2 and bool(turn.get("content")) and turn.get("finish") == "stop"
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
