# Validation §7 — Option B : outils standard (tool_calls ACP réels)

Complément de `RESULTS-switch-robustesse.md`. L'encadrant a décidé de **repasser
au mode outil standard** (au lieu de la pré-exécution de plan + blocs QUOTE de
l'Option A) : les 3 modèles appellent eux-mêmes `grep_files` / `read_lines` via
de vrais tool_calls ACP. Documente la **cause racine de la fuite de reasoning**
et sa correction, puis la grille de re-validation réelle.

- Date : 2026-09-01
- Environnement : `.venv/bin/python` (racine `Tutor-agent/`), llama-server géré
  par le projet, port 8025.
- Outil de validation : `.e2e/switch_check.py <modèle> <sid>` (1 tour réel persona
  Bilal, message `asyncio.Task.run_after`, plan d'outils). Transcripts persistés
  dans `sessions/transcripts/<sid>.json`.

---

## 1) Cause racine de la fuite — marqueur Qwen réel = ` think` (5 lettres)

**Symptôme (avant fix)** : le reasoning fuyait intégralement dans le `content`
visible pour q8 ET ornith → `reasoning` vide, content = raisonnement + réponse
balisés ` think\n…\n response` (le « pense sans répondre » des models est
invisible pour l'étudiant et le `content` devient illisible).

**Fausse piste (handoff précédent)** : le correctif avait câblé les marqueurs
`<thinking>` / `</thinking>` (8 lettres) dans `QwenStreamSplitter`. Ce tag n'est
**jamais** émis par le flux réel. Cause du leurre : le terminal PTY affiche le
caractère `<` (0x3c) comme un espace, donc « `<thinking>` » était lu comme
« thinking » sans vérification octet.

**Vérité, vérifiée par `od -c` / hex sur le flux** (`/tmp/probe-ornith-tools.dc`,
`/tmp/probe-q8-tools.dc` générés par `.e2e/probe_markers.py <m> tools`) :

```
probe q8   : 3c 74 68 69 6e 6b 3e 0a …            →  think (5 lettres)
             3c 2f 74 68 69 6e 6b 3e 0a 0a         →  /think  →  la fermeture
```

Le tag réel est ` think` / ` response` — « think » **5 lettres**, pas 8.
`data.find("<thinking>")` → -1, d'où le splitter qui ne découpait rien et
basculait tout le flux en `content`.

**Fix** (`tutor/engine.py`) : `QwenStreamSplitter.OPEN = "<think>"` /
`CLOSE = "</think>"` écrits via échappements hex `\x3c…\x3e` (immunisés contre
le piège d'affichage PTY). Docstring mise à jour.

**Preuve du fix (splitter seul sur le flux enregistré)** :

| flux | reasoning | content | fuite |
|------|-----------|---------|-------|
| ornith tools | 235 chars | 2 chars (`\n\n` — tool_call, pas de texte) | non |
| q8 tools | 302 chars | 2 chars | non |

---

## 2) Grille de re-validation réelle — PASS 3/3

Chaque tour = `switch_check.py` → `server.ensure(model)` (bascule auto) puis
`TutorEngine.run_turn` réel. Exigences : reasoning **non vide**, content réel,
**zéro artefact** (` think` / ` [THINK]` absents du visible), tool_calls
exécutés, `finish=stop`.

| # | modèle | transcript | reasoning | content | finish | tools réels | anti-invention | fuite |
|---|--------|------------|-----------|---------|--------|-------------|----------------|-------|
| OB1 | ornith | `optb-ornith.json` | 3146 | 906 | stop | grep run_after (0 match), grep create_task (5), grep run_after (0) | ✅ « absent du matériel du cours » | **non** |
| OB2 | q8 | `optb-check.json` | 5218 | 508 | stop | grep run_after (0), grep Task (0), read_file ×2, … | ✅ « absent du matériel du cours » | **non** |
| OB3 | ministral | `ministral-check.json` | 1238 | 688 | stop | grep run_after (0) | ✅ « aucune référence… absent du matériel » | **non** |

`switch_check` a affiché `RESULT: PASS` pour les trois. Les `reasoning`/`content`
ci-dessus sont mesurés sur les transcripts persistés (`len(reasoning)` /
`len(content)`), avec vérification explicite d'absence de marqueurs de
raisonnement dans le content (hex `3c 74 68 69 6e 6b 3e` et `3c 2f 74 68 69 6e 6b 3e`
pour q8/ornith, `[THINK]`/`[/THINK]` pour ministral — opération sur les octets,
pas sur l'affichage PTY).

**Comparaison avec l'état antérieur (fuite)** — transcripts sauvegardés
`.REGRESSION` avant fix :

| transcript | avant fix (REGRESSION) | après fix |
|------------|------------------------|-----------|
| `optb-check` (q8) | reasoning=0, content=3635, **fuite ` think`** | reasoning=5218, content=508, 0 fuite |
| `optb-ornith` | reasoning=0, content=1804, **fuite ` think`** | reasoning=3146, content=906, 0 fuite |

Le reasoning remonte (de 0 → 3146/5218) et le visible est dépouillé des
artefacts : le mode outils standard est fonctionnel sur les 3 profils sans
fuite.

---

## 3) Verdict

- **Option B (outils standard ACP) = validée en réel sur q8, ornith, ministral.** 
  Le nombre d'outils (2) est raisonnable et les modèles appellent correctement
  `grep_files` / `read_lines` (tool_calls rendus proprement au streaming).
- Les 3 modèles répondent en anti-invention (la fausse API `run_after` est
  signalée « absente du matériel », aucune signature inventée), y compris ornith
  et ministral sur template embarqué.
- La fuite historique de reasoning est éliminée (root cause : marqueur
  `<thinking>` à 8 lettres câblé par erreur, réel = ` think` 5 lettres).

## 4) Artefacts utilisés

- Sondes octet-vérité (temporelles) : `/tmp/probe-{ornith,q8}-tools.dc`.
- Transcripts de référence conservés : `optb-check.json`, `optb-ornith.json`,
  `ministral-check.json` + leurs `.REGRESSION` (état anti-fix, pour comparaison).
- `.e2e/probe_markers.py` : sondé la vérité des marqueurs → jetable, supprimée.
