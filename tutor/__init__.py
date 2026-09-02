"""Moteur tuteur socratique — portage du harnais « Ask » pour l'agent ACP.

Modules :
- ``tools``  : outils lecture seule (grep/read/list) + exécution Python.
- ``llm``    : appel backend ``/v1/chat/completions`` (portage de harness.complete).
- ``config`` : profils modèles (config.json), chemins, corpus, prompt système.
- ``engine`` : tour de dialogue type « run_turn » du harnais.
"""
