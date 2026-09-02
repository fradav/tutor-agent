# Tuteur socratique — agent ACP pour Zed

Agent tuteur de programmation socratique branché sur Zed via le protocole
[ACP](https://github.com/zed-industries/agent-client-protocol). C'est un
**agent externe** (clef `agent_servers` de Zed, `"type": "custom"`) : il possède
tson propre appel de modèle (llama.cpp), son propre prompt système (les prompts
`tutor/prompts/tuteur-*.md`) et ses propres outils — Zed ne fait qu'héberger le
fil de discussion. Il **ne fait que lire** le corpus de cours (jamais de
modification de fichiers) et n'exécute que le code de l'étudiant quand vous le
lui demandez explicitement.

Deux modalités de lecture du corpus, **par modèle** (clef
`profiles.<modèle>.tools` de `config.json`) :

- **outils natifs** (défaut de tous les profils sauf ministral) : le modèle
  déclenche lui-même `grep_files` / `read_lines` (spec `tools` OpenAI) et
  l'engine exécute la vraie lecture, lecture seule, résultats réels ;
- **Ask** (`"tools": "ask"`) : l'engine exécute un plan de lecture prédéfini et
  injecte les résultats dans le prompt en blocs QUOTE neutres — le modèle ne
  voit jamais de syntaxe d'outil. Expérimental (testé sur ministral, sans gain).

Quatre modèles locaux (llama.cpp) :

| Modèle | gguf | Template | Particularité |
|--------|------|----------|----------------|
| `qwen3.5-4B` | `Qwen3.5-4B-UD-Q8_K_XL.gguf` | externe (`qwen3.5-chat-template.jinja`) | **défaut** — léger, ancrage fiable |
| `ornith-1.5-9B` | `Ornith-1.5-9B-Q4_K_M.gguf` | embarqué | longues sessions socratiques |
| `ministral-3-8B-Reasoning` | `Ministral-3-8B-Reasoning-2512-Q4_K_M.gguf` | embarqué | **expérimental** (hallucine `run_after`), mode BRUT `[THINK]` |
| `gemma-4-E4B` | `gemma-4-E4B_q4_0-it.gguf` | embarqué | option exactitude, à superviser |

---

## 1. Prérequis

- Python **3.10+**
- `uv` (gestionnaire d'environnement Python) — `brew install uv` ou l'installeur
  autonome <https://docs.astral.sh/uv/> (`uv sync` crée et gère le `.venv/`).
- `llama-server` (llama.cpp) — installé via Homebrew (`brew install llama.cpp`,
  **version stable ≥ 0.3.0** requise : c'est elle qui porte le **mode routeur**)
  ou un binaire précompilé présent dans le PATH.
- ~22 Go libres au total (les 4 .gguf font ~4-6 Go chacun).

## 2. Installation

```bash
cd /chemin/vers/Tutor-agent

# 1. Dépendances Python — crée .venv/ (seule dépendance : le SDK ACP)
uv sync

# 2. Télécharger les modèles (reprise `curl -L -C -` : relancer suffit)
uv run python models/fetch_models.py        # les 4 .gguf → models/
# options utiles :
uv run python models/fetch_models.py --list # noms + tailles attendues
uv run python models/fetch_models.py --only Qwen3.5-4B-UD-Q8_K_XL.gguf
```

> Sans `uv` : `python3 -m venv .venv && .venv/bin/pip install 'agent-client-protocol>=0.12'`
> puis remplacez `uv run python` par `.venv/bin/python` dans les commandes ci-dessous.

Le template externe `models/qwen3.5-chat-template.jinja` est **fourni** dans le
livrable (artefact adapté pour llama.cpp) — pas besoin de le télécharger.
Le corpus de cours (`corpus/Courses/*.qmd`) est aussi fourni.

## 3. Branchement dans Zed

Dans `settings.json` de Zed :

```json
{
  "agent_servers": {
    "tuteur": {
      "type": "custom",
      "command": ["uv", "run", "--project", "/chemin/vers/Tutor-agent", "python", "/chemin/vers/Tutor-agent/acp_agent.py"]
    }
  }
}
```

C'est `uv` qui résout (et crée s'il manque) le `.venv/` — aucun chemin vers un
interpréteur à câbler. Si `uv` n'est pas dans le PATH de Zed, donnez son chemin
absolu (ex. `/opt/homebrew/bin/uv`).

Relancez Zed (ou rechargez les settings), puis sélectionnez **tuteur** comme
agent dans le sélecteur d'agent.

## 4. Premier lancement

Un **seul** llama-server (mode **routeur**) sert les 4 modèles sur le port
8025 : il est lancé automatiquement par l'agent au besoin, en préchargeant le
modèle par défaut (`qwen3.5-4B`). Pour vérifier/contrôler le backend à la main :

```bash
uv run run.py start qwen3.5-4B  # s'assure que le routeur sert le modèle
uv run run.py status           # port + alias servis + provenance
uv run run.py stop             # arrête le llama-server routeur géré
```

`start` (et `server.ensure` appelé par l'agent) **synchronise automatiquement le
preset du routeur** : le routeur ne relit `servers/models-router.ini` qu'au
démarrage, donc si le preset en mémoire ne sert pas les alias actuels de
`config.json` (ex. après édition de la config), il est régénéré + redémarré une
fois ; si le routeur sert déjà un modèle du preset actuel, il est **adopté sans
redémarrage** (le switch instantané par requête reste de mise).

Quand Zed recharge l'agent (ou redémarre) avec une conversation encore ouverte,
il envoie `session/load` : l'agent restaure la session persistée
(`sessions/<id>.json`) et rejoue l'historique dans l'interface — la discussion
reprend où elle s'était arrêtée, sans démarrage de llama-server supplémentaire.
L'état est écrit dès la **création** de la session (`session/new`) : même une
conversation vide se recharge ; en l'absence de tout état, l'agent reconstruit
une **session vierge** (modèle déduit du préfixe du `sessionId`) au lieu
d'échouer.

## 5. Changer de modèle

Le modèle est choisi à la création de session via le **sélecteur ACP** (voir
ci-dessous) ; le fichier `.tutor-model` à la racine du répertoire depuis lequel
vous lancez Zed ne sert que de **défaut** lu au `session/new` (par défaut
`qwen3.5-4B` s'il n'existe pas). Pour fixer ce défaut, créez ce fichier et
écrivez-y le nom du modèle :

```
qwen3.5-4B
```

Valeurs : `qwen3.5-4B`, `ornith-1.5-9B`, `ministral-3-8B-Reasoning`, `gemma-4-E4B`
(ministral expérimental).

### Sélecteur ACP (recommandé)

L'agent expose un sélecteur natif **modèle** (`config_options` renvoyées par
`session/new`, handler `session/set_config_option`) : le modèle se choisit
directement dans l'interface Zed à la création de la session, sans toucher
au disque. Changer le modèle recrée la session avec le nouveau profil (le fil
de discussion n'est pas reporté d'un modèle à l'autre).

> Avec le mode routeur, le changement de modèle est **instantané et sans
> redémarrage** : le routeur décharge/charge le modèle demandé à la prochaine
> requête (une seule instance llama-server, le process n'est jamais tué).

> Si vous préférez servir les modèles sur des endpoints **distant**s (machine
> de labo, autre poste) : mettez l'URL dans `profiles.<modèle>.endpoint` de
> `config.json` — l'agent pilotera alors rien du tout (pas de start/stop
> local), il pinguera simplement l'endpoint.

### Model cards (référence)

- Qwen3.5-4B — <https://huggingface.co/unsloth/Qwen3.5-4B-GGUF>
- Ornith-1.5-9B — <https://huggingface.co/ornith-ai/Ornith-1.5-9B-GGUF>
- Ministral-3-8B-Reasoning-2512 — <https://huggingface.co/mistralai/Ministral-3-8B-Reasoning-2512-GGUF>
- Gemma-4-E4B (qat) — <https://huggingface.co/google/gemma-4-E4B-it-qat-q4_0-gguf>

## 6. `config.json` — déjà réglé pour un usage autonome

Le fichier est livré avec des chemins **relatifs**, résolus par rapport à
`Tutor-agent/` (pas au dossier courant) — aucune édition n'est nécessaire pour
un usage clé-en-main :

- `paths.gguf_dir` → `models` (les .gguf téléchargés ou copiés y arrivent) ;
- `paths.external_template` → `models/qwen3.5-chat-template.jinja` (qwen3.5-4B seul) ;
- `paths.corpus_root` → `corpus/Courses` ;
- `server.llama_bin` → `llama-server` (résolu via le PATH).

Valeurs par défaut livrées :

```json
"paths": {
  "gguf_dir": "models",
  "external_template": "models/qwen3.5-chat-template.jinja",
  "corpus_root": "corpus/Courses",
  "sessions_dir": "sessions"
}
```

Le sampling, le template et le mode de chaque modèle sont déjà réglés dans
`profiles` — pas besoin d'y toucher.

Si vous placez les .gguf ailleurs, changez `paths.gguf_dir` (ou passez `--dest`
à `fetch_models.py`) ; `llama-server` peut être remplacé par un chemin absolu
vers un autre binaire.

## 7. Tests

```bash
# tests backend (génération du preset routeur + cmd llama-server) — aucun modèle requis
uv run python -m unittest tests/test_server.py -v

# tests protocole (moteur en STUB) — aucun serveur ni corpus requis
TUTOR_STUB=1 uv run python -m unittest tests/test_protocol.py -v

# tout-en-un (46 tests) — test_server pur, test_protocol en STUB, test_tools
# couvre avec des vrais contenus (carte sections.json + serveur localhost sur corpus/www)
TUTOR_STUB=1 uv run python -m unittest discover -s tests -v
```

Le mode routeur est documenté par llama.cpp (`tools/server/README.md`, § Using
multiple models). Le preset généré est traçable dans `servers/models-router.ini`
(une section par alias, régénéré à chaque `start`).

## 8. Citations cliquables vers une doc locale

Pour que les citations du modèle soient consultables, l'engine réécrit les
références `fichier.qmd:ligne` qu'il voit sortir du modèle en **liens HTTP
cliquables** vers une copie locale du book rendu :

- **Serveur statique** (`uv run python run.py serve-docs`, auto-démarré par
  `acp_agent.py`) : sert `corpus/www/` — copie du `docs/` rendu du book
  (<https://fradav.github.io/miashs-2026-2027-advanced-programming/>), HTML +
  `site_libs/` + `images/` + `search.json`, port 8765 (`config.json` → `docs`).
- **Réécriture déterministe** côté engine (`tutor/docslinks.py`) : dans le message
  visible sortant (jamais dans le contexte envoyé au modèle),
  `Cours/x.qmd:ligne` → `[Cours/x.qmd:ligne](http://127.0.0.1:8765/Courses/x.html#ancre)`.
  Présent en outils natifs comme en mode Ask, aucun changement de prompt.
- **Carte ligne → section** (`corpus/sections.json`, générée par
  `tools/build_docs_map.py` depuis les `.qmd` + les ancres réelles du HTML rendu) :
  ligne hors section → lien vers la page sans `#` ; fichier inconnu → pas de lien.

Re-synchroniser le cache (le book est rendu dans
`Cours-programmation-MIASHS-2026/docs/`) :

```bash
rsync -a --delete --exclude slides/ --exclude downloads/ \
  ../Cours-programmation-MIASHS-2026/docs/ corpus/www/
uv run python tools/build_docs_map.py
```

Exclusions par défaut : `slides/` (lourd) et `downloads/`, et volontairement
**aucune solution** (risque de fuite de corrigé dans le contexte du modèle).

Limites connues : le clic sur le lien dans la conversation Zed dépend du rendu
markdown de Zed (à valider en pratique) ; la doc Python officielle
(`MIASHS-2026/python-doc/`) n'a pas ce rendu HTML cliquable.

## 9. Pourquoi un agent ACP ? (et pourquoi pas « Zed + Ask + prompt effacé »)

**Peut-on simplement effacer le prompt système de Zed ?** Pour un **modèle**
déclaré dans *LLM Providers* et utilisé par l'agent Zed intégré : **non**. Zed
préfixe chaque requête d'un gros prompt système (~3 000-4 000 tokens — règles de
communication, formatage, usage détaillé des outils) que l'on ne peut que
**compléter** (instructions, profils d'agent, context servers), jamais
remplacer. La demande visant à paramétrer/minimiser ce prompt (cruciale pour les
petits modèles locaux) fait l'objet d'une discussion ouverte non résolue chez
zed-industries/zed (`#58770`).

Pour un **agent externe ACP** (`agent_servers`, `"type": "custom"`), en
revanche, Zed héberge le fil de discussion mais l'agent « owns its own runtime,
auth, model selection, tools, and native configuration » (doc *External
Agents*). Le prompt envoyé au modèle est donc **entièrement** celui du harnais
(`tuteur-*.md` + `PREAMBLE.md`), avec le sampling et le template du profil, sans
rien du socle Zed. C'est la seule voie, dans Zed, vers un prompt « ad hoc,
entièrement maîtrisé » pour des petits modèles — c'est ce que fait ce dépôt.

**Alors « l'agent Zed + Ask (context server) + routeur llama.cpp » ne
suffirait pas ?** Non pour l'objectif « prompt maîtrisé » : le modèle passerait
quand même sous le prompt système Zed (non effaçable) et sous toute la surface
d'outils native de l'agent Zed (édition, terminal…), dont les schémas gonflent
le premier appel de plusieurs milliers de tokens — exactement le contexte qui
fait déraper les modèles 4-9 B locaux (confusion de format d'outils, fuite de
corrigé, répétition). Le harnais apporte ce que Zed ne laisse pas régler : prompt
ad hoc par modèle, deux outils de lecture minimalistes déclenchés par le modèle
(résolution tolérante des chemins, retry/recovery), sampling + template +
splitter de raisonnement par famille, mode Ask en option de profil, persistance
de session, et un routeur llama.cpp à instance unique (switch instantané, sans
redémarrage).
