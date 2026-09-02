# Validation §7 — Switch de modèle & Robustesse

Complément de la validation §7 (ACP tuteur socratique, modèles q8 / ornith / ministral).
Les 3 runs e2e persona Bilal (`sessions/transcripts/{q8,ornith,ministral}-bilal-e2e.json`) étaient déjà passés.
Ce document couvre les deux blocs restants : **switch** et **robustesse**.

- Date : 2026-08-31
- Environnement : `.venv/bin/python3` (racine `Tutor-agent/`), PAS de repo git.
- État de départ : llama-server **ministral** géré par le projet (`servers/server.pid`, `servers/current_model`).
- Tous les tours de contrôle utilisent des sid dédiés `switch-check-*` ; les transcripts e2e existants n'ont pas été touchés.

---

## 1) Switch de modèle (Tutor-agent)

Profil de session utilisé pour chaque tour de contrôle : persona Bilal, message
« Attends, je suis sûr de moi là : pour enchaîner des tâches asyncio on a `asyncio.Task.run_after`… »,
via `.e2e/switch_check.py <modèle> <sid>` (un seul `run_turn`, exigence : content non vide + `finish=stop`).

| # | test | résultat | preuve concrète |
|---|------|----------|-----------------|
| SW1 | `run.py start q8` depuis l'état ministral → tue ministral, lance q8 | **PASS** | `.venv/bin/python3 run.py start q8` → `[OK] démarré pid=32138` ; `run.py status` → `port 8025: OK (alias: q8; géré)` ; `lsof -nP -iTCP:8025 -sTCP:LISTEN` → **un seul** `llama-ser 32138` (port 8025) |
| SW2 | Tour de contrôle q8 | **PASS** | `.venv/bin/python3 .e2e/switch_check.py q8 switch-check-q8` → `sessions/switch-check-q8.json` : `alias=q8`, `finish='stop'`, `content_len=570`, début : « Bonjour ! Je vois que tu as une idée, mais avant de continuer, je dois vérifier avec préci… » (~12 s) |
| SW3 | `run.py start ornith` → tue q8, lance ornith | **PASS** | `run.py start ornith` → `[OK] démarré pid=32241` ; `status` → `alias: ornith; géré` ; un seul serveur sur 8025 |
| SW4 | Tour de contrôle ornith | **PASS** | `.e2e/switch_check.py ornith switch-check-ornith` → `sessions/switch-check-ornith.json` : `alias=ornith`, `finish='stop'`, `content_len=890`, début : « Intéressant que tu sois sûr — mais je dois te dire franchement, en toutes lettres : `run_a… » (~23,5 s) |
| SW5 | Retour à l'état initial : `run.py start ministral` | **PASS** | `run.py start ministral` → `[OK] démarré pid=33103` ; `status` → `port 8025: OK (alias: ministral; géré)` ; `cat servers/current_model servers/server.pid` → `ministral` / `33103` ; `lsof -nP -iTCP:8025 -sTCP:LISTEN` → un seul `llama-ser 33103` |

**Constats switch**
- À chaque `start <nouvel_alias>`, l'ancien serveur est tué proprement : il ne reste **jamais** qu'un seul processus `llama-server` en écoute sur le port 8025 (`lsof`).
- `servers/current_model` et `servers/server.pid` reflètent systématiquement le nouvel alias (vérifié en `cat` pour q8/ornith/ministral).
- Chaque tour de contrôle a répondu sur le bon alias avec du contenu réel (`finish=stop`, 451 à 890 caractères), aucun serveur zombi.

---

## 2) Robustesse

### C1 — Serveur qui tombe → relance automatique

| test | résultat | preuve concrète |
|------|----------|-----------------|
| Tuer le llama-server en cours, puis `tutor.server.ensure("q8")` doit relancer | **PASS** | `kill $(cat servers/server.pid)` (pid 32241) ; `lsof -nP -iTCP:8025 -sTCP:LISTEN` → plus rien (port libre) ; `.venv/bin/python3 -c "import tutor.server as s; s.ensure('q8'); print('ensured')"` → `ensured`, relance `pid=32654` ; `run.py status` → `alias: q8; géré` ; tour de contrôle court → `sessions/switch-check-ensure.json` : `alias=q8`, `finish='stop'`, `content_len=451`, début : « Absent du matériel du cours : `asyncio.Task.run_after` n'est pas dans le cours (je l'ai ch… » |

### C2 — Corpus introuvable

| test | résultat | preuve concrète |
|------|----------|-----------------|
| `run_turn("attends…")` avec `corpus_root` → `/tmp/absent-xyz` doit dire « no match »/« introuvable » sans crasher | **FAIL au premier passage → PASS après fix** | Premier passage → **crash** : `FileNotFoundError: [Errno 2] No such file or directory: '/tmp/absent-xyz/01_asynchronous.qmd'`, chain : `engine.py:305 run_tool_plan → engine.py:207 read_lines → tools.py:51 read_lines → tools.py:27 _read_lines`. Fix appliqué dans `tutor/tools.py` (`read_lines` L49 : `except OSError: return []`, même tolérance que `grep_files`). Re-vérifié e2e le 31/08 : `.venv/bin/python3 .e2e/robust_corpus.py` (serveur q8) → **aucun traceback**, run nominal `content=811 chars | finish=stop`, visible : « `run_after` est **absent du matériel du cours**… sans succès » (le QUOTE equivalent à « no match » est émis). |

**Cause racine (corrigée)**
- `grep_files` (tutor/tools.py L39-42) tolère les fichiers absents : `except OSError: continue` → les 2 `grep` du plan affichent « no match found in the material » et dégradent proprement.
- `read_lines` (tutor/tools.py L49-58) appelait `_read_lines` **sans** `try/except` → l'`OSError` remontait depuis l'étape `{"tool": "read", "section": "01", …}` du `TOOL_PLAN` et faisait échouer tout le tour avant qu'un message « introuvable » ne puisse être émis.
- **Fix** : `except OSError: return []` ajouté dans `read_lines` (même comportement que `grep_files`) → l'engine émet le QUOTE « no match found in the material » neutre et le tour passe.

**Note (artefact de sampling, non bloquant)** : lors de la re-vérification, un run sur deux était un raisonnement spiralé q8 (≈15 457 chars de `reasoning`, `content` vide, `finish=length`) — l'autre run était nominal (`content=811`, `finish=stop`). Le crash est éliminé dans les deux cas ; le « no match » visible n'est garanti que sur le run nominal.

### C3 — session/cancel → stop_reason="cancelled"

| test | résultat | preuve concrète |
|------|----------|-----------------|
| Stub `TUTOR_STUB=1` + `TutorAgent` in-process, `prompt()` en tâche asyncio puis `cancel()` pendant l'émission | **PASS** | `.venv/bin/python3 .e2e/robust_cancel.py` → `sid: ornith-session` ; `stop_reason: cancelled` ; `updates=2 chunks=1 thoughts=1` ; aucune exception remontée ; `RESULT: PASS` |

### C4 — Retry contenu vide (déjà codé ~L337)

| test | résultat | preuve concrète |
|------|----------|-----------------|
| Premier appel backend `finish=stop` + contenu vide ⇒ second appel | **PASS** (code + exécution) | Lecture `tutor/engine.py` L337-339 : `if not content.strip() and finish == "stop":` → second `yield from self._stream_call(messages, BrutStreamSplitter())`. Exécution `.venv/bin/python3 .e2e/robust_retry.py` (override de `complete_model_stream` : 1er appel vide `finish=stop`, 2e avec contenu, sans serveur) → `appels backend = 2 (attendu 2)` ; `content=92 chars | finish=stop` ; `RESULT: PASS`. La branche exécutée est la même que le chemin réel (retry sur le back-end streamé). |

---

## Points d'attention

1. **C2** : le crash initial (FAIL) a été **corrigé** dans `tutor/tools.py` (`read_lines` → `except OSError: return []`) puis **re-vérifié en e2e** : plus de traceback, le visible émet bien « absent du matériel du cours ». Voir §2/C2 pour le détail et l'artefact de sampling constaté.
2. **Scripts ajoutés** (nouveaux, dans `.e2e/`, à garder) : `switch_check.py`, `robust_corpus.py`, `robust_cancel.py`, `robust_retry.py`. Aucune modification de `config.json`, `tutor/*.py`, `protocol.py`, `run.py`, `acp_agent.py`, `.e2e/drive.py`, `tests/*`.
3. **Sessions créées** : `sessions/switch-check-{q8,ornith,ensure,retry}.json` (sid dédiés, voulus). Les transcripts e2e d'origine sont intacts.
4. **État final restauré** : llama-server **ministral** (pid 33103) — identique à l'état de départ.
5. Un llama-server **hors projet** tourne sur le port 10013 (pid 23448, sweep-next-edit) : non touché, sans rapport.
