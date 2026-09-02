"""Validation §5 — course de switch : deux ``ensure`` concurrents (modèles
différents) ne doivent pas se tuer mutuellement le serveur (bug n°1 du handoff).

Reproduit le symptôme rapporté : deux ``session/new`` simultanés (cas Zed pendant
un switch) → avant le verrou ``_ensure_lock``, chacun pouvait redémarrer le
serveur pendant que l'autre l'adoptait → serveur down au premier prompt
(Connection refused). Avec le verrou, les deux ``ensure`` sont sérialisés et le
serveur finit sain sur l'un des deux alias.

Usage: python3 .e2e/switch_race.py [model_a] [model_b]
"""
from __future__ import annotations
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tutor import server  # noqa: E402

MODEL_A = "ministral-3-8B-Reasoning"
MODEL_B = "ornith-1.5-9B"


def main() -> int:
    print(f"[switch_race] {MODEL_A} ↔ {MODEL_B} (2 threads simultanés)")
    errors: list[tuple[str, str]] = []  # (modèle, exception)
    lock_out = threading.Lock()

    def run_ensure(model: str) -> None:
        try:
            st = server.ensure(model, wait_up_to=180.0)
            with lock_out:
                print(f"  ensure({model}) → {st.get('status')} ({st.get('detail')})")
        except Exception as exc:  # noqa: BLE001 — on recueille tout pour l'assertion
            with lock_out:
                errors.append((model, repr(exc)))

    # Barrière maison : on libère les deux threads au même moment.
    gate = threading.Event()
    threads = []
    for model in (MODEL_A, MODEL_B):
        t = threading.Thread(
            target=lambda m=model: (gate.wait(), run_ensure(m)), daemon=True
        )
        t.start()
        threads.append(t)
    gate.set()
    for t in threads:
        t.join(timeout=240.0)

    if errors:
        print("RACE FAIL — erreurs :")
        for model, exc in errors:
            print(f"  {model}: {exc}")
        return 1

    st = server.status()
    ok = bool(st["healthy"]) and bool(st["served_aliases"])
    print("status final:", st)
    if not ok:
        print("RACE FAIL — serveur non sain après la course")
        return 1
    print("RACE PASS — serveur sain, alias servi:", st["served_aliases"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
