"""Couche protocole ACP du tuteur socratique.

Implémente le sous-ensemble ACP dont Zed a besoin pour un profil « Ask »
(lecture seule, pas de terminal) : ``initialize``, ``session/new``,
``session/load`` (reload Zed), ``session/prompt`` et ``session/cancel``.

Note d'architecture : ``TutorAgent`` n'hérite **pas** de ``acp.Agent`` (qui est
un ``Protocol``). Si on sous-classait le Protocol, ses méthodes-stubs (corps
``...``) seraient héritées, donc callables, donc exposées par le router
JSON-RPC du SDK — ce qui enverrait des réponses ``None`` cassées pour des
méthodes qu'on ne veut pas exposer. On fournit une classe « duck-typée » qui ne
définit que les méthodes voulues ; toute autre méthode renverra automatiquement
l'erreur JSON-RPC ``-32601 method not found`` par le SDK.

Le transport lui-même (boucle JSON-RPC 2.0 line-delimited sur stdio, logs sur
stderr) est géré par le SDK ``agent-client-protocol`` via ``acp.run_agent`` —
voir ``acp_agent.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import acp
from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    Implementation,
    LoadSessionResponse,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    StopReason,
)

from tutor.engine import TutorEngine, initial_state
from tutor import config, server

log = logging.getLogger("tuteur.protocol")

AGENT_NAME = "tuteur-acp"
AGENT_TITLE = "Tuteur socratique MIASHS"
AGENT_VERSION = "0.1.0"
# Modèle par défaut : suit config.json (défaut livré = qwen3.5-4B). Source
# unique pour `session/new` sans sélecteur et pour les tests.
DEFAULT_MODEL = config.default_model()
MODEL_FILE = ".tutor-model"

# Anciennes clefs de profil (avant le renommage q8/ornith/ministral → …),
# encore portées par des sessions persistées ou des sessionId antérieurs.
_LEGACY_MODEL_KEYS = {
    "ministral": "ministral-3-8B-Reasoning",
    "ornith": "ornith-1.5-9B",
    "q8": "qwen3.5-4B",
}


def _normalize_model(model: str) -> str:
    """Mappe une clef de profil (actuelle ou historique) vers la clef actuelle.

    Le moteur résout le profil via ``state["model"]``
    (``tutor.engine.complete_model_stream``) : une ancienne clef persistée
    (q8/ornith/ministral) doit être traduite au reload pour ne pas lever de
    ``profile()`` KeyError.
    """
    if model in config.profiles():
        return model
    return _LEGACY_MODEL_KEYS.get(model, config.default_model())


def _model_for_session_id(session_id: str) -> str:
    """Clef de profil la plus probable pour un sessionId non persisté.

    Le sessionId est ``<model>-<label>[-<n>]`` (§new_session) : on matche la
    clef de profil en préfixe, puis les préfixes historiques, puis le défaut.
    """
    for key in config.profiles():
        if session_id == key or session_id.startswith(key + "-"):
            return key
    for legacy, key in _LEGACY_MODEL_KEYS.items():
        if session_id == legacy or session_id.startswith(legacy + "-"):
            return key
    return config.default_model()


def _persist_state(state: dict[str, Any]) -> None:
    """Écrit l'état ``sessions/<id>.json`` (format Playground).

    Garantit qu'une session existe sur disque dès sa création (``session/new``)
    pour que le reload Zed la retrouve, et fige la session vierge reconstruite
    par le fallback de ``load_session``.
    """
    base = config.sessions_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{state['id']}.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _student_text(prompt: list[Any]) -> str:
    """Extrait le texte des blocs de contenu envoyés par le client (Zed)."""
    parts = []
    for block in prompt:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _acp_update(kind: str, payload: Any) -> Any | None:
    """Mappe un ``(kind, payload)`` du moteur vers une update ACP de session.

    ``reasoning`` → thought ; ``content`` → message ; les tool_calls livés par la
    boucle outillée (Option B) → ``tool_start`` (kind search / read) puis
    ``tool_progress`` (status completed + contenu du résultat). Jamais ``None``
    pour les kinds connus ; les kinds inattendus passent en message (ne casse
    pas un mapping futur)."""
    if kind == "reasoning":
        return acp.update_agent_thought_text(payload)
    if kind == "tool_start":
        tc_id = payload["tool_call_id"]
        if payload["tool"] == "read_lines":
            return acp.start_read_tool_call(tc_id, payload["title"],
                                            payload.get("path") or "")
        return acp.start_tool_call(tc_id, payload["title"], kind="search",
                                   status="in_progress", raw_input=payload.get("args"))
    if kind == "tool_progress":
        return acp.update_tool_call(
            payload["tool_call_id"], status="completed",
            content=[acp.tool_content(acp.text_block(text=payload["result"]))],
            raw_output=payload["result"],
        )
    return acp.update_agent_message_text(payload)


BACKEND_DOWN_MESSAGE = (
    "⚠️ Le moteur modèle ne répond pas pour le moment — il est probablement en train "
    "de charger ou de recharger le modèle (après un changement de modèle, §5). "
    "Patientez quelques secondes puis renvoyez votre message. Si l'erreur persiste, "
    "vérifiez l'état du serveur (`run.py status`)."
)


def _backend_down(exc: BaseException) -> bool:
    """Vrai si l'exception vient d'un backend modèle injoignable.

    ``urlopen`` lève ``urllib.error.URLError`` (reason =
    ConnectionRefusedError) quand llama-server ne répond pas — typiquement
    pendant l'arrêt/rechargement d'un modèle (switch §5). ``OSError`` avec
    errno ECONNREFUSED/ECONNRESET couvre les cas proches.
    """
    import errno
    import urllib.error

    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, ConnectionError):
        return True
    if isinstance(exc, OSError) and exc.errno in (errno.ECONNREFUSED, errno.ECONNRESET):
        return True
    return False


def _model_config_options(current: str) -> list[SessionConfigOptionSelect]:
    """Options du sélecteur de modèle ACP (§5), une par profil de config.json.

    ``current`` = clef de profil actif ; chaque option résume le mode (normal /
    brut) et le template (externe / embarqué) pour aider l'élève dans le sélecteur
    Zed. Utilisé par ``session/new`` et ``session/set_config_option``.
    """
    options = []
    for key, prof in config.profiles().items():
        detail = prof.get("mode", "normal")
        if prof.get("template") == "external":
            detail += " · template externe"
        options.append(
            SessionConfigSelectOption(
                value=key,
                name=key,
                description=f"Modèle tuteur socratique (mode {detail})",
            )
        )
    return [
        SessionConfigOptionSelect(
            id="model",
            name="Modèle",
            description="Modèle tuteur socratique actif pour cette session",
            category="model",
            current_value=current,
            type="select",
            options=options,
        )
    ]


class TutorAgent:
    """Agent ACP tuteur (sous-ensemble « Ask », structural vs ``acp.Agent``)."""

    def __init__(self, default_model: str = DEFAULT_MODEL) -> None:
        self._conn: acp.Client | None = None
        self._default_model = default_model
        self._sessions: dict[str, dict[str, Any]] = {}
        self._running: dict[str, asyncio.Task[Any]] = {}

    # -- connexion (appelée par AgentSideConnection quand on_connect existe) --

    def on_connect(self, conn: acp.Client) -> None:
        """Garde une référence vers la connexion pour émettre ``session/update``."""
        self._conn = conn

    # -- initialize -----------------------------------------------------------

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> acp.InitializeResponse:
        version = min(protocol_version, acp.PROTOCOL_VERSION)
        log.info("initialize : client version=%s → agent version=%s", protocol_version, version)
        return acp.InitializeResponse(
            protocol_version=version,
            agent_capabilities=AgentCapabilities(load_session=True),
            agent_info=Implementation(name=AGENT_NAME, title=AGENT_TITLE, version=AGENT_VERSION),
        )

    # -- session/new ----------------------------------------------------------

    def _resolve_model(self, cwd: str) -> str:
        """Modèle actif : lu dans ``<cwd>/.tutor-model``, défaut config.json.

        Mécanisme de switch custom §5 : le fichier ne fixe que le **défaut** lu au
        ``session/new`` ; le switch en cours de session passe par le sélecteur ACP
        (``session/set_config_option``, voir ``set_config_option``).
        """
        try:
            value = Path(cwd, MODEL_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            return self._default_model
        # La valeur est normalisée (``_normalize_model``) : un fichier portant
        # encore une ancienne clef (q8/ornith/ministral, avant le renommage) doit
        # donner la clef actuelle — sinon ``config.profile`` lèverait une KeyError
        # « profil inconnu » au session/new.
        return _normalize_model(value or self._default_model)

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> acp.NewSessionResponse:
        model = self._resolve_model(cwd)
        label = Path(cwd).name or "session"
        base = f"{model}-{label}"
        session_id = base
        n = 1
        while session_id in self._sessions:
            n += 1
            session_id = f"{base}-{n}"
        # §5 : backend up avant de rendre la session. Skippé en mode STUB (tests
        # unitaires sans llama-server) ; hors STUB on laisse l'erreur remonter —
        # une session sans backend ne peut pas tutorer (message explicite au
        # client). to_thread : server.ensure peut charger un modèle (long).
        if not config.STUB:
            try:
                ensure_state = await asyncio.to_thread(server.ensure, model)
            except server.ServerError as exc:
                log.error("session/new : backend %s indisponible (%s)", model, exc)
                raise RequestError.internal_error(
                    {"reason": f"backend {model} indisponible", "detail": str(exc)}
                ) from None
            log.info("session/new : backend %s prêt (%s)", model, ensure_state.get("detail"))
        state = initial_state(
            model=model,
            session_id=session_id,
            label=label,
            cwd=str(cwd),
            persona="Étudiant·e MIASHS (persona socratique)",
        )
        self._sessions[session_id] = {
            "id": session_id,
            "model": model,
            "cwd": str(cwd),
            "state": state,
            "messages": [],
        }
        if not config.STUB:
            # Une session créée doit être rechargeable par Zed dès le départ
            # (§1.2) : sans fichier, un reload retombe sur « session inconnue ».
            _persist_state(state)
        log.info("session/new : id=%s (model=%s, cwd=%s)", session_id, model, cwd)
        return acp.NewSessionResponse(
            session_id=session_id,
            config_options=_model_config_options(model),
        )

    # -- session/load (reload Zed : restaure une session persistée) -------------

    async def load_session(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None,
        **_meta: Any,
    ) -> LoadSessionResponse:
        """Recharge une session persistée (``sessions/<id>.json``) et la rejoue.

        Zed envoie ``session/load`` quand il redémarre/recharge l'agent avec une
        conversation encore ouverte (``agent_capabilities.load_session`` annoncé à
        l'``initialize``). Le SDK route la requête vers cette méthode — nom
        ``load_session`` d'après ``acp/agent/router.py`` (la variante héritée du
        ``Protocol`` ``acp.Agent`` n'est pas définie ici, cf. en-tête de module).

        On relit l'état persisté par le moteur à chaque tour (``engine._persist``),
        on reconstruit la session en mémoire, puis on **rejoue** l'historique au
        client via ``session/update`` (user → thought → message pour chaque tour)
        pour que la discussion réapparaisse dans l'interface Zed.

        Cas sans état persisté (session créée mais jamais promptée, ou clef de
        profil renommée) : fallback tolérant — on reconstruit une **session
        vierge** (modèle relu du préfixe du sessionId) au lieu de lever -32602,
        pour que le reload d'une conversation vide réussisse.

        Surtout **pas** de ``server.ensure`` ici : l'état persisté peut venir d'un
        backend distant (endpoint §4) et le moteur prépare le backend au tour
        suivant de toute façon (la session rechargée reste utilisable telle quelle).
        """
        state_path = config.sessions_dir() / f"{session_id}.json"
        if not state_path.exists():
            # Session créée mais jamais persistée (jamais promptée avant le
            # session/new persistant, ou ancien format) : on reconstruit une
            # session vierge plutôt que de faire échouer le reload Zed (§1.2).
            log.warning("session/load : aucune trace persistée pour %s → session vierge", session_id)
            model = _model_for_session_id(session_id)
            state = initial_state(
                model=model,
                session_id=session_id,
                label=Path(cwd).name or "session",
                cwd=str(cwd),
                persona="Étudiant·e MIASHS (persona socratique)",
            )
            if not config.STUB:
                _persist_state(state)
        else:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RequestError.invalid_params(
                    {"sessionId": session_id, "reason": "état de session illisible", "detail": str(exc)}
                ) from None
            if "model" not in state or "turns" not in state:
                raise RequestError.invalid_params(
                    {"sessionId": session_id, "reason": "état de session illisible"}
                )
            # Clef de profil normalisée : les sessions d'avant le renommage
            # portent l'ancienne clef — le moteur la lit dans state["model"].
            model = _normalize_model(state["model"])
            if state["model"] != model:
                state["model"] = model
        persisted_cwd = state.get("cwd") or cwd
        self._sessions[session_id] = {
            "id": session_id,
            "model": model,
            "cwd": persisted_cwd,
            "state": state,
            "messages": [
                {"role": "user", "content": turn["student"]}
                for turn in state["turns"]
                if turn.get("student")
            ],
        }
        if self._conn is not None:
            for turn in state["turns"]:
                if turn.get("student"):
                    await self._conn.session_update(
                        session_id, acp.update_user_message_text(turn["student"]))
                if turn.get("reasoning"):
                    await self._conn.session_update(
                        session_id, acp.update_agent_thought_text(turn["reasoning"]))
                if turn.get("content"):
                    await self._conn.session_update(
                        session_id, acp.update_agent_message_text(turn["content"]))
        log.info("session/load : id=%s (model=%s, %d tour(s) rejoués)",
                 session_id, model, len(state["turns"]))
        return LoadSessionResponse(config_options=_model_config_options(model))

    # -- switch de modèle (sélecteur ACP §5) ------------------------------------

    async def set_config_option(
        self,
        session_id: str,
        config_id: str,
        value: str,
        **_meta: Any,
    ) -> acp.SetSessionConfigOptionResponse:
        """Change le modèle d'une session en cours (option ``category="model"``).

        Zed expose au ``session/new`` une option de config ``model`` (sélecteur) ;
        quand l'élève choisit un autre modèle, il émet ``session/set_config_option``
        avec ``value`` = clef profil de config.json. On recrée l'état de session du
        nouveau modèle (même ``session_id``, même ``cwd``, persona identique), on
        prépare le backend hors STUB, et on renvoie la liste d'options à jour
        (champ ``config_options`` requis par la réponse ACP).

        Le fichier ``.tutor-model`` n'est **pas** écrit : ce switch est par session,
        le fichier reste le défaut lu au ``session/new`` suivant.
        """
        if config_id != "model":
            raise RequestError.invalid_params(
                {"configId": config_id, "reason": "config inconnue (attendu 'model')"}
            )
        session = self._sessions.get(session_id)
        if session is None:
            raise RequestError.invalid_params(
                {"sessionId": session_id, "reason": "session inconnue"}
            )
        try:
            config.profile(value)
        except KeyError:
            raise RequestError.invalid_params(
                {"value": value, "reason": "profil de modèle inconnu"}
            ) from None
        if not config.STUB:
            try:
                ensure_state = await asyncio.to_thread(server.ensure, value)
            except server.ServerError as exc:
                log.error("set_config_option : backend %s indisponible (%s)", value, exc)
                raise RequestError.internal_error(
                    {"reason": f"backend {value} indisponible", "detail": str(exc)}
                ) from None
            log.info("set_config_option : backend %s prêt (%s)", value, ensure_state.get("detail"))
        cwd = session["cwd"]
        label = Path(cwd).name or "session"
        state = initial_state(
            model=value,
            session_id=session_id,
            label=label,
            cwd=str(cwd),
            persona="Étudiant·e MIASHS (persona socratique)",
        )
        session["model"] = value
        session["state"] = state
        session["messages"] = []
        log.info("set_config_option : session %s passée au modèle %s", session_id, value)
        return acp.SetSessionConfigOptionResponse(
            config_options=_model_config_options(value),
        )

    # -- session/prompt -------------------------------------------------------

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> acp.PromptResponse:
        session = self._sessions.get(session_id)
        if session is None:
            raise RequestError.invalid_params({"sessionId": session_id, "reason": "session inconnue"})
        text = _student_text(prompt)
        session["messages"].append({"role": "user", "content": text})
        task = asyncio.current_task()
        if task is None:
            raise RequestError.internal_error({"reason": "prompt appelé hors tâche asyncio"})
        self._running[session_id] = task
        try:
            stop_reason: StopReason = await self._run_turn(session, text)
        except asyncio.CancelledError:
            log.info("prompt %s interrompu par session/cancel", session_id)
            stop_reason = "cancelled"
        finally:
            self._running.pop(session_id, None)
        log.info("prompt %s terminé (stop_reason=%s)", session_id, stop_reason)
        return acp.PromptResponse(stop_reason=stop_reason)

    async def _run_turn(self, session: dict[str, Any], student_text: str) -> StopReason:
        """Un tour réel (moteur tuteur §3 — mode STUB en test).

        Le streaming vit dans le moteur : ``engine.run_turn_stream`` est un
        **générateur synchrone** (appels réseau bloquants) qu'on fait tourner
        dans un thread via ``asyncio.to_thread``, et dont on achemine les
        ``(kind, text)`` vers le client avec une file asyncio — le raisonnement
        (bloc thought) s'affiche avant/pendant le contenu, au fil de l'eau.
        ``session/cancel`` annule la lecture de la file ; le thread backend finit
        seul en arrière-plan (rien n'est streamé après l'annulation).
        """
        if self._conn is None:
            raise RequestError.internal_error({"reason": "agent non connecté"})
        state = session.get("state")
        if state is None:
            raise RequestError.internal_error({"reason": "état de session absent"})
        engine = TutorEngine(state)
        sid = session["id"]
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _put(kind: str, payload: Any) -> None:
            # La file est consommée du loop principal ; un ``put`` direct depuis
            # le thread drain lève RuntimeError (futures non thread-safe quand un
            # getter attend). call_soon_threadsafe déplace le put sur le loop.
            loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

        def _drain_thread() -> None:
            # Création du générateur et itération dans le même try : en production
            # run_turn_stream est lazy (le corps court au next, déjà couvert) ;
            # une levée SYNCHRONE à la création (doublon possible) serait sinon
            # perdue sans sentinelle → deadlock du queue.get() du loop principal.
            try:
                gen = engine.run_turn_stream(student_text)
                while True:
                    try:
                        kind, text = next(gen)
                    except StopIteration as exc:
                        _put("__end__", exc.value)  # sentinelle : dict du tour
                        return
                    _put(kind, text)
            except Exception as exc:  # erreur moteur relayée côté asyncio
                if not _backend_down(exc):
                    _put("__error__", exc)
                    return
                # Backend injoignable (down / en cours de rechargement) : plutôt
                # qu'un `urlopen … Connection refused` brut dans Zed, on streame
                # un message explicite. Le message étudiant reste dans
                # l'historique ; le tour (sans turn persisté) ne l'est pas.
                log.info("prompt %s : backend injoignable (%r)", sid, exc)
                _put("content", BACKEND_DOWN_MESSAGE)
                _put("__end__", {"backend_error": str(exc)})

        # Le drain tourne en continu pendant qu'on lit la file (référence gardée
        # pour ne pas laisser la tâche au collecteur pendant l'exécution).
        drain_task = asyncio.create_task(asyncio.to_thread(_drain_thread))
        while True:
            kind, payload = await queue.get()
            if kind == "__end__":
                break
            if kind == "__error__":
                raise payload from None
            if not payload:
                continue
            update = _acp_update(kind, payload)
            if update is not None:
                await self._conn.session_update(sid, update)
        # Le thread backend (to_thread) finit ORPHELIN en arrière-plan : ne pas
        # l'« await » ici. En chemin de cancel (IsolatedAsyncioTestCase), le await
        # d'un to_thread deadlocke car le GIL reste tenu par le main thread en
        # run_until_complete ; du côté réel, la session suivante / le shutdown
        # rendent de toute façon le résultat caduc. Le drain doit juste terminer
        # sa lecture de la file (ou être annulé), c'est tout ce qu'on attend.
        # On ne coalesce pas par la fin : le drain étant orphelin, on ne l'attend
        # jamais — le tour se termine dès la sentinelle lue.
        return "end_turn"

    # -- session/cancel (notification, sans réponse) --------------------------

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        """Interrompt le tour en cours (annulation asyncio de la tâche prompt)."""
        task = self._running.get(session_id)
        if task is None:
            log.info("cancel : aucun tour actif pour %s", session_id)
            return
        log.info("cancel : interruption de %s", session_id)
        task.cancel()
