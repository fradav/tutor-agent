# TODO — Agent ACP tuteur socratique (qwen3.5-4B / ornith-1.5-9B / gemma-4-E4B)

Agent qui se branche sur Zed via le protocole **ACP** (Agent Client Protocol) pour jouer le
tuteur socratique MIASHS, en s'appuyant sur les 3 modèles retenus du plateau Small-Models
(ministral-3-8B-Reasoning, étudiée puis retirée — cf. historique).
L'infra est conçue pour le tutorat : légère, sans le system prompt lourd de zed-agent, avec
un control fin du prompt, du sampling et du template de chat par modèle.

Livrable : un **répertoire autonome** (`Tutor-agent/`), recopiable chez l'élève, embarquant
les modèles, les scripts ACP, les prompts tuteur et le corpus du cours — réutilisable dans
son Zed avec un minimum de configuration.

Convention : chaque tâche est une case `- [ ]` vérifiable. Les chemins cités sont ceux de
l'existant (harness du Playground) à reprendre ou à copier.

---

## 0. Contexte et décisions de cadrage

> Ce qui suit est l'**historique de cadrage** (2026-2027). Ministral a depuis été
> retirée du livrable (hallucination structurelle `run_after`, aucun gain mesuré
> en rejeu — 02/09) : le livrable ne sert plus que qwen3.5-4B, ornith-1.5-9B et
> gemma-4-E4B. Les décisions sur `[THINK]`/alternance stricte restent valables
> comme documentation du cadrage technique.

- [x] **Garder qwen3.5-4B / ornith-1.5-9B / gemma-4-E4B** (parmi le plateau Small-Models) : laisser tomber
      zed-agent comme base (system prompt déjà lourd, infra non adaptée au tutorat).
      *(ministral-3-8B-Reasoning était dans la liste initiale — retirée du livrable, voir historique.)*
- [ ] **Protocole ACP** (JSON-RPC 2.0 sur stdio, complémentaire de MCP) : Zed parle à notre
      agent, l'agent parle aux modèles via llama.cpp ; pas d'intermédiaire « éditeur →
      zed-agent » ni de prompt système d'éditeur imposé.
- [ ] **Reproduire le comportement « Ask »** du harness existant (*harness.py* +
      *demo-ask-harness.py*) : outils lecture seule pré-exécutés par le backend, résultats
      réels injectés en blocs `QUOTE` neutres (jamais de syntaxe d'outil), ancrage
      fichier:ligne, anti-invention, exécution réelle du code étudiant en `PYTHON-RUN`.
- [x] **Piège central documenté dès le départ** *(historique — ministral retirée du livrable)* : ministral-3-8B-Reasoning n'accepte aucun message système,
      exige l'alternance stricte user/assistant (sinon HTTP 500) et raisonne en `[THINK]…[/THINK]`
      dans le `content` (mode BRUT). Toute l'architecture du moteur tuteur doit le supporter.
- [x] **Workflow d'installation `uv`** (clone → `uv sync` → `uv run …`) : environnement géré par
      `uv` (`pyproject.toml` + `uv.lock`, `[tool.uv] package=false`), seule dépendance tierce
      `agent-client-protocol>=0.12` (`requests` retiré à la bascule : jamais importé, tout passe
      par `urllib` stdlib) ; alternative sans uv documentée (venv + pip).

---

## 1. Étude (relire les sources + valider la technique ACP)

### 1.1 Harness existant (la référence comportementale)
- [x] Relire `Small-Models/Playground/experiment/run-session.py` :
      - `MODEL_SAMPLING` (L90-111) — sampling exact par modèle ;
      - `EXTERNAL_TEMPLATE_MODELS` / `EMBEDDED_TEMPLATE_KWARGS` / `BRUT_THINK_MODELS` /
        `EMBED_INSTRUCTIONS_MODELS` / `SYSTEM_THINK_PROMPT_MODELS` (L130-160) ;
      - `_tutor_prompt` + `PREAMBLE` + `build_system` (L215-263) ;
      - `cmd_start` (L275-324) — ligne de commande llama-server exacte par modèle ;
      - `cmd_new` (L383-452) — construction de l'état de session, `no_system_embed` ;
      - `run_turn` (L485-626) — appel LLM, parse BRUT `[THINK]`, ré-injection multi-tours.
- [x] Relire `Small-Models/Playground/experiment/harness.py` : `grep_files`, `read_lines`,
      `list_directory`, `format_quote`, `format_python`, `run_python`, `complete` (à reprendre
      tels quels ou presque).
- [x] Relire `Small-Models/Playground/experiment/sessions.py` : `COURSE_ROOT` et `COURSE_FILES`
      (corpus = `Cours-programmation-MIASHS-2026/Courses/*.qmd`, 7 fichiers 00→06), structure
      des `SESSION_DEFS` (persona, opening, files, tool_plan).
- [x] Relire `MIASHS-2026/seances/demo-s2/demo-ask-harness.py` : variante « 5 tours étudiants »
      avec plan d'outils par tour et retry sur contenu vide (L296-335).
- [x] Relire `MIASHS-2026/seances/demo-s2/AGENTS-ask-good.md` (profil Ask + socratique) et
      `Small-Models/Playground/prompts/tuteur-ministral.md` / `tuteur-q8.md` / `tuteur-ornith.md`
      (les « Réglages spécifiques » à préserver mot pour mot dans le livrable).

### 1.2 Protocole ACP
- [x] Lire la doc du SDK Python `agent-client-protocol`
      (`pip install agent-client-protocol` ; docs https://agentclientprotocol.github.io/python-sdk/).
- [x] Lister et vérifier les méthodes côté agent attendues par Zed : `initialize` (négocie
      `protocolVersion`, `capabilities`), `session/new` (cwd, mcpServers → sessionId),
      `session/prompt`, `session/cancel`, `session/set_model` (**absent du SDK 0.12 — vérifié**, pas
      « unstable », cf. §2), `session/load`. Les notifications agent→client `session/update` (streaming
      `update_agent_message`), `session/request_permission`, `fs/*`, `terminal/*`.
- [x] Implémenter `session/load` (reload Zed) : `initialize` annonce
      `agent_capabilities.load_session=True` ; le handler `load_session` relit
      `sessions/<id>.json` et **rejoue l'historique** au client via `session/update`
      (user → thought → message par tour) pour restaurer la conversation dans Zed.
      `session/new` **persiste l'état dès la création** (une session vide doit rester
      rechargeable) ; l'état absent au load est un **fallback tolérant** (session
      vierge reconstruite, modèle relu du préfixe du sessionId — anciennes clefs
      q8/ornith/ministral normalisées vers les clefs actuelles). Pas de
      `server.ensure` au load (état éventuellement distant, §4). Tests :
      `test_load_session_replays_history`, `test_load_session_missing_state_rebuilds_blank`,
      `test_load_session_corrupt_state_raises`, `test_new_session_backend_ensure_called`.
- [x] Vérifier l'intégration Zed (`settings.json` → `"agent_servers": {"nom": {"type": "custom",
      "command": …, "args": …}}`) : testée en réel le 2026-09-01 (branchement custom
      `uv run --project … python acp_agent.py`, session tuteur fonctionnelle dans Zed) —
      cf. README §3. Le registre ACP est une alternative non testée (hors scope livrable).
- [x] **Décider du mécanisme de switch de modèle** (voir §5) selon l'état réel de
      `session/set_model` dans Zed.

### 1.3 Décision embarquement des .gguf
- [x] **DÉCISION (utilisateur) : ne PAS copier les .gguf dans le livrable.** Les 4 .gguf
      (~5–6 Go chacun) ne sont pas versionnés dans `Tutor-agent/` ; l'élève les obtient via
      `models/fetch_models.py` (téléchargement, reprise `curl -C -`) ou la copie depuis la
      **clé USB** à la construction. `models/` contient `fetch_models.py` + le template
      externe ; sur la machine dev, les `.gguf` sont des **symlinks** vers `Small-Models/gguf`
      (écrasés par le fetch ou la clé chez l'élève — cf. §6).

---

## 2. Infra ACP — squelette stdio

- [x] `protocol.py` + `acp_agent.py` : classe `TutorAgent` (duck-typée, **sans héritage** de
      `acp.Agent` pour ne pas exposer les stubs `...` du Protocol) + entrée `acp.run_agent`.
      Handlers implémentés :
      - `initialize` → renvoie `protocolVersion` (min avec celui du client) + `agent_info`
        (`Implementation`), capabilities par défaut (profil Ask) ;
      - `session/new` → lit le modèle actif dans `<cwd>/.tutor-model` (défaut `ornith-1.5-9B`),
        `session_id = f"{model}-{label}"` (+ `-n` si collision) ;
      - `session/prompt` → **stub** du moteur (§3) : stream par `session/update` (3 blocs
        espacés 0.05 s), rend `stopReason` ; annulation asyncio propre ;
      - `session/cancel` → `task.cancel()` du tour en cours (`StopReason="cancelled"`) ;
      - `session/set_model` → **absent du SDK agent-client-protocol 0.12** (vérifié §1.2/§5) →
        switch via le **sélecteur ACP** (`config_options` sur `session/new` +
        `session/set_config_option`, §5) ; `.tutor-model` ne sert que de défaut au `session/new`.
- [x] **Pas de transport maison** (décision §2 originel) : la boucle line-delimited sur stdio est
      gérée par le SDK (`acp.run_agent`) — messages JSON sur stdout, logs sur stderr. Aucun
      `transport.py` n'existe (le SDK le rend obsolète) ; `protocol.py` porte les handlers.
- [x] Gestion `asyncio` : `run_agent` lit stdio et dispatche les handlers `async` — la boucle
      stdio et un futur appel LLM restent concurrents (les handlers ne bloquent jamais le loop).
- [x] Gestion d'erreur : le router du SDK renvoie `-32601` (méthode inconnue) ; on lève
      `RequestError.invalid_params` → `-32602` (testé), `internal_error` → `-32603`.
- [x] Tests unitaires (`tests/test_protocol.py`, unittest asyncio, transports en mémoire) :
      initialize, new_session (+ modèle fichier/défaut, collision), prompt streamé, session
      inconnue → -32602, cancel → cancelled. **7 tests verts** (`python -m unittest tests.test_protocol -v`).

---

## 3. Moteur tuteur — adaptation du harness « Ask »

- [x] `tutor/tools.py` : porter les outils lecture seule de `harness.py` (`grep_files`,
      `read_lines`, `list_directory`, `find_path`, `diagnostics`) — sujets à la troncature
      (MAX_SHOWN, MAX_LINE_CHARS) et aux bornes (cf. §1.1).
- [x] `tutor/engine.py` : tour de dialogue type « run_turn » :
      1. reçoit le message étudiant ;
      2. exécute un **plan d'outils** (défini par tour ou par persona) sur le corpus → blocs
         `QUOTE` réels (fichier:ligne), via `format_quote` ; « no match » → signal explicite ;
      3. exécute éventuellement le code étudiant (`run_python`) → bloc `PYTHON-RUN` ;
      4. construit les messages pour le modèle (fuse ou pas selon le profil, voir §4) ;
      5. appelle le backend modèle, parse reasonining/content, **retry si contenu vide** ;
      6. réinjecte le raisonnement multi-tours selon le profil ;
      7. retourne texte + trace (outils, matches, usage) pour le transcript.
- [x] `tutor/prompts/` : copier le socle `MIASHS-2026/tuteur-socratique-AGENTS.md` + les 4
      variantes `tuteur-q8.md` / `tuteur-ornith.md` / `tuteur-ministral.md` / `tuteur-gemma4.md`
      + un `PREAMBLE`
      adapté (« harnais Ask », pas de syntaxe d'outil, anti-invention).
- [x] **Décision d'architecture :** garder le mode « pré-exécution de plan + QUOTE » du harness
      (Option A, la plus sûre sur 8–9B locaux, conforme au `demo-ask-harness.py`) **en
      priorité** ; documenter en option B le mode « outils ACP réels appelés par le modèle »
      (`fs/read_text_file`…) à n'activer qu'après validation sur les 4 modèles.
- [x] Transcript : persister chaque tour (JSON, même format que `transcripts/*.json`) pour
      pouvoir comparer avec les synthèses Playground (mkdigest).
- [x] **Brancher le moteur dans `protocol.py`** : `new_session` construit l'état via
      `engine.initial_state` ; `_run_turn` appelle `engine.run_turn` (via `asyncio.to_thread`)
      et streame le contenu en ≥3 blocs espacés (channel `session/update` conservé pour
      `session/cancel`) ; `_stub_reply` §2 supprimé. Mode test STUB via `TUTOR_STUB=1`.
- [x] **Endpoints distants (config)** : `engine.complete_model` résout l'URL du modèle via
      `config.model_base_url(model)` — profil `endpoint` non vide → llama-server distant,
      sinon serveur local de `config.json`. Helpers `model_base_url` / `is_remote` dans
      `tutor/config.py` ; champ `endpoint` documenté dans `config.json` (vide = local).

---

## 4. Backend modèles — 4 profils exacts *(historique : 4 profils dont ministral — livrable final 3)*

- [x] `tutor/llm.py` : réutiliser `complete()` de `harness.py` (endpoint
      `/v1/chat/completions`, champ `model` = alias de llama.cpp, `stream: False`,
      `max_tokens` = contexte `-c 32768`).
- [x] `config.json` : définir les 4 profils avec le **sampling exact** :
      - **qwen3.5-4B** : `temperature=0.6, top_p=0.95, top_k=20, min_p=0.0, presence_penalty=0.0,
        repeat_penalty=1.0` ;
      - **ornith-1.5-9B** : idem qwen3.5-4B (socle Qwen) ;
      - **ministral-3-8B-Reasoning** : `temperature=0.7, top_p=0.95, top_k=40, min_p=0.05,
        presence_penalty=0.0, repeat_penalty=1.1`.
      - **gemma-4-E4B** : socle Qwen (0.6/0.95/20/0/0/1.0 — pas de reco officielle).
- [x] `tutor/server.py` : lancement de `llama-server` (`/opt/homebrew/bin/llama-server` ou
      chemin configurable) avec la ligne par modèle :
      - **qwen3.5-4B** : `--jinja --chat-template-file <qwen3.5-chat-template.jinja> --reasoning-preserve
        -ngl 99 --alias qwen3.5-4B -c 32768` (**template externe**) ;
      - **ornith-1.5-9B** : `--jinja --reasoning-preserve -ngl 99 --alias ornith-1.5-9B -c 32768`
        (**template embarqué**, pas de `--chat-template-file`) ;
      - **ministral-3-8B-Reasoning** : `--jinja --reasoning-preserve -ngl 99 --alias ministral-3-8B-Reasoning -c 32768`
        (**template embarqué**) ;
      - **gemma-4-E4B** : `--jinja --reasoning-preserve -ngl 99 --alias gemma-4-E4B -c 32768`
        (**template embarqué**, preserve thinking interleaved `<|channel|>`, pas de `--chat-template-file`).
      Un seul serveur à la fois (port dédié, redémarrage automatique au changement de modèle),
      ou 3 ports séparés si le switch à chaud est retenu (voir §5).
- [x] **Mode BRUT ministral-3-8B-Reasoning** (capital) : pas de `reasoning_format=deepseek` (avale la réponse
      multi-tours) ; parse manuel `[THINK]…[/THINK]` du `content` (équilibré/fermé, balise non
      fermée → raisonnement non capturé, pas de fuite) ; ré-injection du `content` BRUT
      (`assistant_raw`) au tour suivant (l'endpoint OpenAI-compatible refuse les blocs
      structured de type « thinking »).
- [x] **Absence de système ministral-3-8B-Reasoning** : AUCUN message `role: system` ; les consignes tuteur
      sont embarquées dans le **premier message étudiant** ; **tout le contexte du tour**
      (consignes, message, QUOTE, PYTHON-RUN) est **fusionné en un seul message user**
      (alternance stricte user/assistant, sinon `raise_exception` → 500).
- [x] Gestion du serveur : `health_ok()` (port), pidfile/log pour debug, `stop` propre ;
      `ensure(model)` (démarre/adopte/redémarre au bon alias) + CLI `run.py`
      (`start <model>` / `status` / `stop` / `agent`).
- [x] **Endpoints distants (serveur)** : si `config.is_remote(model)`, `tutor/server.py` (et
      `run.py`) ne **démarre ni ne redémarre ni n'arrête** de llama-server local pour ce
      modèle (le serveur distant existe déjà ; `health_ok` ping l'endpoint). Doc README
      d'installation (config … endpoint distant) : voir §6.

---

## 5. Switch entre les 4 modèles *(historique : 4 modèles dont ministral — livrable final 3)*

- [x] **DÉCISION (utilisateur) : le fallback custom est retenu** (pas d'obstination sur
      `session/set_model`, absent du SDK agent-client-protocol 0.12). Le sélecteur repose sur le
      fichier `.tutor-model` (lu au `session/new`) + redémarrage du backend par `server.ensure`.
- [x] **Sélecteur ACP natif implémenté (switch au niveau de Zed)** : l'agent renvoie
      `config_options` (select `model`, une option par profil, valeur courante) sur `session/new`
      et un handler `session/set_config_option` — le modèle se choisit dans le sélecteur d'agent
      de Zed à la création de session, sans toucher au disque ; `.tutor-model` ne sert plus que
      de **défaut** lu au `session/new`. Couvert par `test_new_session_returns_config_options` /
      `test_set_config_option_switch_model` /
      `test_set_config_option_unknown_config_or_model_raises` (19 tests verts).
- [x] **Sélecteur fichier implémenté et testé** :
      - fichier `.tutor-model` lu au `session/new` — implémenté (`protocol._resolve_model`),
        couvert par `test_new_session_model_from_file` / `test_new_session_default_model` ;
      - bascule de backend au `session/new` via `server.ensure` (redémarre llama.cpp avec le bon
        profil si l'alias servi diffère) — branché en §5, validé en réel (realtest switch
        `switch-check-ornith`, `.e2e/RESULTS-switch-robustesse.md` SW4) et par
        `test_new_session_backend_ensure_called` ;
      - la slash-commande `/modèle …` (switch en **cours** de session) reste une option non
        implémentée : le switch de modèle se fait à la création de session (ou au redémarrage de
        l'agent).
- [x] **DÉCISION (utilisateur) : pas de surcharge locale de config** — une surcharge
      `config.local.json` (merge sur `config.json`) a été envisagée pour changer facilement de
      modèle, puis **abandonnée** : `.tutor-model` suffit et reste le seul mécanisme de switch.
      La surcharge au niveau de Zed est **implémentée** via le sélecteur ACP natif
      (`config_options` / `session/set_config_option`, ci-dessus) ; `.tutor-model` reste le
      défaut au `session/new`.
- [ ] Tester le switch **à chaud** (sans démonter la session, le fil de discussion a-t-il le droit
      de survivre à un changement de template ? — probing à faire, surtout qwen3.5-4B ↔
      ministral-3-8B-Reasoning, templates différents). Le mode routeur (§6) a levé la partie technique : le serveur n'est
      plus redémarré au switch (PID constant, routing par requête), donc la session ACP et le fil
      survivent ; reste la question **pédagogique** — rejouer l'historique d'un modèle sur un
      autre (templates/sampling différents) produit-il une réponse cohérente ? À trancher par un
      test de contrôle.
- [x] Au minimum : choix du modèle à la création de session + au redémarrage de l'agent — fait
      (`.tutor-model` + `server.ensure`) ; rejouer un tour de contrôle après chaque switch → §7.
- [x] **`.tutor-model` périmé → normalisé au `session/new`** : `_resolve_model` traduit les
      anciennes clefs (q8/ornith/ministral) via `_normalize_model` — sinon `config.profile`
      levait une `KeyError` « profil inconnu: ministral » (Internal error) à l'ouverture d'une
      session au-dessus d'un fichier non renommé. Couvert par
      `test_new_session_model_from_file_legacy_key`.

---

## 6. Autonomie du livrable (packaging)

- [x] Structure cible de `Tutor-agent/` :
      ```
      Tutor-agent/
        TODO.md / README.md
        config.json                 # 4 profils (sampling, template, mode), modèle actif
        acp_agent.py                # point d'entrée ACP (stdio)
        protocol.py                 # handlers ACP (pas de transport.py — stdio via le SDK)
        tutor/
          __init__.py
          engine.py  tools.py  llm.py  server.py
          prompts/   # socle + tuteur-q8.md + tuteur-ornith.md + tuteur-ministral.md + tuteur-gemma4.md + PREAMBLE
        models/
          Qwen3.5-4B-UD-Q8_K_XL.gguf
          Ornith-1.5-9B-Q4_K_M.gguf
          Ministral-3-8B-Reasoning-2512-Q4_K_M.gguf
          gemma-4-E4B_q4_0-it.gguf
          qwen3.5-chat-template.jinja
        corpus/Courses/*.qmd        # copie du matériel élève (publique) 00→06
        sessions/                   # transcripts + état (runtime)
      ```
      Note : les .gguf ne sont **pas versionnés** dans le livrable (§1.3) —
      `models/` contient `fetch_models.py` + le template ; sur la machine dev,
      les `.gguf` sont des **symlinks** vers `Small-Models/gguf/` (écrasés par
      `fetch_models.py` ou la copie clé USB chez l'élève).
- [x] **Modèles (pas de copie — décision §1.3)** : `models/` contient
      `fetch_models.py` (script de téléchargement des 4 .gguf → `models/`, reprise
      `curl -C -`, skippe un fichier déjà présent de taille attendue) et
      `qwen3.5-chat-template.jinja` (copie locale de l'artefact llama.cpp, PAS le
      `chat_template.jinja` HF qui diffère). La copie physique des .gguf se fait à
      la construction de la clé USB (`scripts/assemble_usb.py`, hors livrable).
      Le fetch ou la clé **écrasent** les symlinks dev par de vrais fichiers.
- [x] **Copie du corpus** : `corpus/Courses/*.qmd` (7 fichiers, 00→06) copiés
      depuis `Cours-programmation-MIASHS-2026/Courses/` — complet pour l'ancrage.
- [x] **README d'instruction chez l'élève** : `README.md` (dépendances, install,
      branchement Zed `agent_servers`, `.tutor-model`, endpoints distants,
      chemins `config.json` relatifs à `BASE_DIR`, tests).
- [x] **Renommage des alias en noms longs + model cards** : `qwen3.5-4B` / `ornith-1.5-9B` /
      `ministral-3-8B-Reasoning` / `gemma-4-E4B` partout (config.json, INI routeur, tests,
      `.e2e`, doc) — seuls
      les fichiers prompts `tuteur-{q8,ornith,ministral,gemma4}.md` et les IDs de sessions gardent
      leur nom court (champ `prompt` des profils + historique). Les model cards Hugging Face
      sont référencées dans le README §5 (unsloth/Qwen3.5-4B, ornith-ai/Ornith-1.5-9B,
      mistralai/Ministral-3-8B-Reasoning-2512, google/gemma-4-E4B-it-qat-q4_0-gguf).
- [x] **Chemins relatifs ancrés sur `BASE_DIR` (Option A — par défaut)** :
      `config._resolve_path()` résout les chemins non-absolus de `config.json` vs
      `Tutor-agent/` (pas vs `cwd`) → livrable **clé-en-main** : `paths.gguf_dir="models"`,
      `external_template="models/qwen3.5-chat-template.jinja"`, `corpus_root="corpus/Courses"`,
      `sessions_dir="sessions"` ; `server.llama_bin="llama-server"` (résolu via le PATH).
      Aucun réglage de chemins chez l'élève — les valeurs du README §6 sont celles livrées.
- [x] Vérifier qu'aucun secret ne traîne dans le livrable (ni token Mistral, ni
      `.env-secret`) — grep complet hors `.venv` : 0 hit.
- [x] **Workflow d'installation `uv`** : clone → `uv sync` (crée `.venv/`, seule dep
      `agent-client-protocol>=0.12`) → `uv run …` ; `[tool.uv] package=false`. Le
      `requirements.txt` est supprimé (remplacé par `pyproject.toml` + `uv.lock`) ; `requests`
      retiré (jamais importé, tout passe par `urllib`). Alternative sans uv : `python3 -m venv
      .venv && .venv/bin/pip install 'agent-client-protocol>=0.12'`, puis `.venv/bin/python` à
      la place de `uv run python`. La bascule uv ne change aucun chemin du livrable (README §6
      inchangé) — elle ne modifie que les commandes d'install/serveur.

  **DÉCISION PRISE (encadrant) — mode routeur llama.cpp implémenté** :
  « un llama-server mono-modèle géré par `run.py` » est remplacé par le mode
  routeur (`llama-server --models-preset models-router.ini --models-max 1`),
  qui sert les 4 modèles **sans kill/relance** au switch du `.tutor-model` — le
  routing se fait par requête sur le champ `model` du body (l'alias), l'instance
  du serveur n'est jamais redémarrée (`server.ensure` n'adopte que, ne tue
  plus). Config par modèle = fichier INI généré depuis `config.json`
  (`tutor/server.render_preset`, une section par alias) : `chat-template-file`
  pour qwen3.5-4B seul (template externe), template embarqué pour
  ornith-1.5-9B / ministral-3-8B-Reasoning / gemma-4-E4B, `c = max_tokens` (32768),
  `n-gpu-layers = 99`, `load-on-startup` = modèle par défaut seul — cf. doc
  llama.cpp `tools/server/README.md` § Using multiple models. Bascule llama.cpp `brew install llama.cpp` HEAD → **stable 0.3.0**
  (build 10621, c1d0e7a00) — le routeur y est supporté. Double source de vérité
  (config.json: sampling/alias/endpoint + INI: chemin/ctx/template) documentée
  dans README §4/§5. Validation : 10 tests `server.py` (génération preset + cmd
  routeur) + 19 tests protocol (STUB) + réel (`start qwen3.5-4B` → routage
  qwen3.5-4B/ornith-1.5-9B/ministral-3-8B-Reasoning/gemma-4-E4B par requête, PID constant
  aux switchs, `switch_race` PASS,
  `switch_race` PASS,
  `switch_check` tour moteur PASS).

- [x] **Fix routeur « preset obsolète » (01/09)** : le routeur ne relit le preset
      (`models-router.ini`) qu'au démarrage — après le renommage des alias en noms longs,
      l'ancien routeur (démarré avec les alias courts `ornith`/`q8`/`ministral`) répondait
      `400 model '<alias long>' not found` pendant que `server.status()` affichait des alias
      « corrects » (fallback sur le preset disque, fausse preuve de cohérence). Corrigé dans
      `tutor/server.py` : `_adopt_or_refresh(model, wait_up_to)` — routeur géré déjà up → si
      au moins un alias du preset **actuel** est servi par `/v1/models`, adoption simple (pas
      de kill au switch) ; sinon `stop()` + `_wait_port_free()` + `start()` (régénère le
      preset via `render_preset`) **une seule fois**, puis `_mark_alias`. `_wait_port_free()`
      attend que le port ne réponde plus (anti-rebond). Validation : 3 tests `EnsureRefreshTest`
      (alias vieux → restart ; alias actuels → adoption sans kill ; un seul alias actuel parsé
      suffit) + réel (routeur 8025 redémarré, `/v1/models` = qwen3.5-4B / ornith-1.5-9B /
      ministral-3-8B-Reasoning, `model=ornith-1.5-9B` → HTTP 200).

---

## 7. Tests et validation

- [x] **Run de bout en bout** : rejouer les 5 tours étudiants de `demo-ask-harness.py` sur
      chaque modèle (qwen3.5-4B, ornith-1.5-9B, ministral-3-8B-Reasoning), même message
      d'ouverture (persona), et comparer
      au transcript Playground correspondant (↔ `synthese-*.md`).
      - q8 / ornith : transcripts `sessions/transcripts/{q8,ornith}-bilal-e2e.json` (déjà passés).
      - ministral : rejoué le 2026-09-01 avec le moteur corrigé — `sessions/transcripts/ministral-bilal-e2e.json`
        (5 tours + clôture), digest `sessions/transcripts/digests/ministral-bilal-e2e.txt`.
- [x] **Grille ministral-3-8B-Reasoning** : aucun HTTP 500 (alternance stricte respectée), raisonnement NON vide
      à chaque tour, `[THINK]…[/THINK]` équilibrés ou proprement non capturés, pas de fuite de
      raisonnement dans le visible. → e2e T1-T6 : `reasoning` non vide partout, `content` réel
      partout, 0 artefact « (réflexion) » dans le visible (détail dans le bilan ci-dessous).
- [x] **Anti-fuite / anti-invention** : sur les 4 modèles, tester une fausse API
      (ex. `run_after`) — attendu « absent du matériel », pas de signature inventée ; et une
      clôture (« je passe à la suite ») — attendu : arrêt, pas de question.
      → ministral-3-8B-Reasoning e2e : anti-invention tenue (« absent du matériel »), clôture propre (T6).
      → Option B (outils standard) re-validée le 2026-09-01 sur qwen3.5-4B/ornith-1.5-9B/ministral-3-8B-Reasoning :
      `.e2e/RESULTS-optb-outils.md` (PASS 3/3, run_after signalé « absent du matériel »,
      aucune signature inventée). T2/T3 peuvent donner une structure de code quasi complète
      quand l'étudiant insiste — limitation pédagogique documentée au bilan.
- [x] **Option B — mode outils standard ACP (décision encadrant)** : les 4 modèles appellent
      eux-mêmes `grep_files` / `read_lines` via de vrais tool_calls ACP (au lieu de la
      pré-exécution de plan + QUOTE de l'Option A). Fuite de reasoning corrigée (root cause :
      marqueur réel ` think` 5 lettres, pas `<thinking>` 8) → `.e2e/RESULTS-optb-outils.md`.
      Nettoyage des vestiges Option A : `DEFAULT_TOOL_PLAN` / `tool_plan` supprimés de
      `tutor/engine.py` (plus aucun appel à `run_tool_plan`), callsites `.e2e/{drive,
      switch_check,robust_corpus,robust_retry}.py` nettoyés — grep
      `tool_plan|DEFAULT_TOOL_PLAN|TOOL_PLAN` scoped `Tutor-agent/**/*.py` : 0 match.
- [x] **Switch** : qwen3.5-4B → ornith-1.5-9B → ministral-3-8B-Reasoning en cours de session, vérifier que le tour de contrôle
      repart correctement avec le nouveau profil. → `.e2e/RESULTS-switch-robustesse.md` §1 (SW1-SW5 PASS).
- [x] **Robustesse** : retry contenu vide, `session/cancel` en plein génération, serveur qui
      tombe (relance auto), fichier corpus introuvable (message clair).
      → `.e2e/RESULTS-switch-robustesse.md` §2 (C1-C4 PASS, dont fix `read_lines` pour C2).
- [x] Nettoyage : serveur 8030 hors projet tué (fait le 2026-09-01) ; sondes jetables
      `.e2e/probe_*.py` + `_probe_stream_tools.py` supprimées ; `sessions/` tronqué (retirés :
      résidus conversation libre Playground-empty, `ornith-ere-des-cristaux`,
      `.bak-{bug,brief-fallback,corrige-fallback}` de ministral, `bilal-switch-{min,ornith}`,
      `digests/` racine vide, nœud `.e2e/.e2e`). **Conservés** : e2e `{q8,ornith,ministral}-bilal-e2e.json`,
      `switch-check-*`, optb/ministral-checks + leurs `.REGRESSION`, transcript
      `ornith-tmp.json` (témoin du realtest switch §5), `.e2e/RESULTS-*.md`,
      `drive.py`, `switch_*.py`, `robust_*.py`.

### Bilan §7 — ministral « ne raisonne qu'à T1 » : bug du harness, corrigé

**Diagnostic (racine)** : ce n'est PAS un trait du modèle mais un bug du harness.
1. `tutor/llm.py` n'envoyait pas `reasoning_format` → llama.cpp appliquait son extraction
   native → le raisonnement partait dans `reasoning_content` et le `content` ne gardait que le
   texte visible, **sans la trace**. La doc officielle Mistral est explicite : « always replay
   the full assistant message (including ThinkChunk) » — sans sa trace en contexte, le modèle
   ne raisonne plus à T2+.
2. Découvert en route : le modèle « pense sans répondre » — à T2+, il émet `[THINK]` (token 34)
   mais oublie souvent de fermer `[/THINK]` (token 35) → sa réponse complète est englobée dans
   le think non fermé, invisible pour le splitter. C'est **probabiliste** (~40-60 % par essai,
   instable dans le temps), indépendant de la troncature de trace, de la température (0.3-1.0
   testé), de `repeat_penalty` et de `max_tokens`.

**Fix (code)** :
- `tutor/llm.py` : `reasoning_format="none"` envoyé explicitement → llama.cpp ne fait plus
  l'extraction native, les tags `[THINK]…[/THINK]` passent dans `content` où `BrutStreamSplitter`
  les sépare (raisonnement / visible) en streaming.
- `tutor/engine.py` : ré-injection multi-tours au format **natif** `[THINK]…[/THINK]` + réponse
  (`reinject_raw` — conformément à la doc Mistral), ce qui fait raisonner le modèle à chaque
  tour. Les formats alternatifs testés sont pires : neutre « (réflexion)… » (fuite du format
  dans le visible à T4+), contexte user (le modèle ne raisonne plus à T3+), ré-injection sans
  trace (ancien bug).
- `tutor/engine.py` : récupération **close-and-continue** — si après `RETRY_ATTEMPTS` le
  contenu visible est vide, on ferme `[/THINK]` nous-mêmes, on réinjecte le raisonnement, et on
  demande la réponse à l'étudiant (`RECOVERY_PROMPT`) ; les messages de récupération sont
  retirés de l'historique (le tour est ré-injecté proprement au format natif) et le « --- » de
  tête est nettoyé. Testé 8/8 sur T2 (sonde dédiée, `.e2e/` — supprimée au nettoyage).
  `FALLBACK_RESPONSE` ne sert plus qu'en dernier recours.

**Validation** : e2e `ministral-bilal-e2e` rejoué (5 tours + clôture) — `reasoning` non vide
et `content` réel à **chaque** tour (T1 : 1340/1095, T2 : 1081/1214, T3 : 2266/913, T4 : 970/1562,
T5 : 951/818, T6 : 1967/997), aucune fuite « (réflexion) », aucun « --- » de tête, clôture
propre. Tests unitaires : 14 OK. `reasoning_format="none"` explicite + ré-injection native +
recovery = raisonnement présent à chaque tour avec des réponses réelles (vs fallback générique
avant le fix).

**Limitations restantes (à documenter pour l'encadrant)** :
- Le « pense sans répondre » reste probabiliste → certains tours passent par la recovery
  (latence +~20-30 s/tour) ; c'est le prix du format natif qui garantit le raisonnement partout.
- Anti-fuite perfectible : selon les runs, ministral peut donner une structure de code quasi
  complète quand l'étudiant insiste (« balance-moi l'exemple ») — c'est une limite pédagogique
  du modèle, distincte du bug de raisonnement (voir `synthese-ministral.md`).
- Sampling inchangé (0.7 / 0.95 / 40 / 0.05 / 0.0 / 1.1 — reco model card).

## 8. Intégration gemma-4-E4B

*(Abrégé « 4ᵉ modèle » dans l'historique : ce contexte comptait ministral ; le livrable final sert 3 modèles.)*

- [x] **Décision template (utilisateur)** : garder le **template EMBARQUÉ** de
      gemma-4-E4B — pas de `chat_template_gemma4.jinja` externe. Avec
      `--jinja --reasoning-preserve` + `reasoning_format="none"`, le template
      intégré émet bien des pensées interleaved (preserve thinking fonctionnel,
      vérifié réel) → `chat-template-file` jamais branché pour gemma (§4/§6).
- [x] **Profil `gemma-4-E4B` dans `config.json`** : gguf `gemma-4-E4B_q4_0-it.gguf`,
      alias `gemma-4-E4B`, `template: embedded`, `mode: normal` (gemma accepte un
      vrai message système comme qwen3.5-4B / ornith-1.5-9B — PAS brut),
      `marker: "gemma"` (choix du splitter), `prompt: tuteur-gemma4.md`, sampling
      socle Qwen (0.6/0.95/20/0/0/1.0). `default_model` inchangé (`ornith-1.5-9B`).
- [x] **Splitter gemma** (`tutor/engine.py`) : `GemmaStreamSplitter(QwenStreamSplitter)`
      avec les marqueurs de canal réels `<|channel>thought\n…<channel|>` (OPEN =
      `<|channel>thought`, CLOSE = `<|channel|>`) — les pensées interleaved du
      `content` sont séparées du visible en streaming, aucun `<|channel|>`
      résiduel dans la réponse affichée ; `_opening_splitter` retenu par
      `config.is_gemma` (mode normal, ≠ marqueurs `[THINK]` du brut ministral).
- [x] **`tutor/prompts/tuteur-gemma4.md`** : variante gemma (approche « normale »,
      système accepté) = copie du socle `tuteur-q8.md` + « Réglages spécifiques
      gemma-4-E4B » (détection de clôture, pas de cédage « juste une ligne »,
      boucle d'événements, suivi de l'état étudiant, concision/checklist).
- [x] **Programme d'installation** : entrée gemma dans `models/fetch_models.py`
      (`gemma-4-E4B_q4_0-it.gguf` ← `google/gemma-4-E4B-it-qat-q4_0-gguf`,
      ~5.15 Go, docstring « les 4 ») ; sur la machine dev, symlink
      `models/gemma-4-E4B_q4_0-it.gguf` → `Small-Models/gguf/` (écrasé par le
      fetch / la clé USB chez l'élève, §1.3).
- [x] **Routeur** : `render_preset` génère la section `[gemma-4-E4B]` (template
      embarqué → pas de `chat-template-file` ; `load-on-startup = false` car
      défaut = ornith). Aucun changement de `tutor/server.py` requis au-delà des
      docstrings (4 profils).
- [x] **Tests** : `test_server.py` (`test_gemma_4_e4b_embedded_template`, boucle
      « champs communs » sur les 4 modèles) + `test_protocol.py`
      (`test_new_session_returns_config_options` : 4 options) — suite complète
      (29 tests) verte.
- [x] **Doc** : README et TODO mis à jour (4 modèles, table, model card
      `google/gemma-4-E4B-it-qat-q4_0-gguf`, prérequis ~22 Go).
- [x] **Réel (e2e)** : drive e2e (`uv run python3 .e2e/drive.py gemma-4-E4B
      gemma-bilal-e2e`, Bilal 5 tours) sur le routeur 8025 — séparation
      pensée/visible confirmée (visible **sans** `<channel|>`/`<|channel|>`,
      aucun fallback, T1..T5 tous en contenu socratique réel). Le bug
      `<|channel|>` → `<channel|>` (close du splitter gemma) était la cause des
      fallbacks T1/T5 du run précédent ; corrigé dans `tutor/engine.py`
      (`GemmaStreamSplitter.CLOSE`) et validé en réel. Les 4 modèles sont
      servis par le routeur ; chaque profil passe un e2e réel (§7 + celui-ci).

---

## Pièges à garder en tête

- **ministral-3-8B-Reasoning = le piège central** : pas de système, fusion mono-message user,
  alternance stricte, mode BRUT `[THINK]`, sampling 0.7/0.95/40/0.05/0.0/1.1 (≠ socle Qwen).
- **qwen3.5-4B = seul des 4 à utiliser le template externe** `qwen3.5-chat-template.jinja` ;
  ornith-1.5-9B, ministral-3-8B-Reasoning et gemma-4-E4B = template embarqué (pas de
  `--chat-template-file`).
- **gemma-4-E4B = marqueurs de canal `<|channel>thought…<channel|>`** (template embarqué,
  preserve thinking interleaved, mode normal ; sampling socle Qwen). Ne pas confondre avec
  les `[THINK]…[/THINK]` du brut ministral (splitter choisi par `marker: "gemma"`).
- **Petits modèles** : ne jamais injecter de syntaxe d'outil (`[tool result …]`, `<tool_call>`) —
  ils la reflètent ; toujours les blocs `QUOTE`/`PYTHON-RUN` neutres + consigne « ne réimprime
  pas ».
- **`session/set_model` unstable** → fallback implémenté : sélecteur ACP (`config_options` /
  `session/set_config_option`) + `.tutor-model` lu au `session/new` (redémarrage de l'agent).
- **Poids des .gguf** (~1–4 Go chacun) : trancher tôt la stratégie de copie (§1.3).
- **`Tutor-agent/` part de zéro** : aucun code existant, tout est à créer (vérifier que le dépôt
  est bien initialisé — actuellement vide, pas de `.git` à supprimer).
- **Ne jamais afficher** `Small-Models/.env-secret` / `MISTRAL_API_TOKEN` (ministral14/mistral-live
  ne sont pas dans les 4 retenus, mais restent présents dans le Playground).
- **Terminal** : pas de `cd X && …` long (paramètre `cd` + commandes courtes) ; `head -n` plutôt
  que l'option `head_lines` en argument shell ; pas d'option `--time-style` GNU (`ls -laT`).
