"""Appel au backend modèle — portage de ``harness.complete`` (endpoint OpenAI).

Le champ ``model`` est l'**alias** de llama.cpp (ex. ``ornith-1.5-9B``) : le serveur
ignore le nom et utilise le modèle chargé au démarrage (``--alias`` / ``start``).
Le sampling par modèle est celui du profil config.json (voir tutor.config).

``stream_complete`` est le cœur : appel **streamé** (``stream: True``), génère un
tuple ``(delta_reasoning, delta_content, finish_reason, usage, tool_calls)`` par
chunk SSE — le raisonnement (``delta.reasoning_content``) arrive sur son propre
canal, séparé du contenu visible (grâce à ``--reasoning-preserve`` côté llama.cpp).

Quand ``tools`` est fourni, le body embarque ``tools`` + ``tool_choice: auto`` : le
modèle peut émettre des **tool_calls natifs** (mode outils standard, Option B). Les
"arguments" JSON arrivent par fragments SSE — ils sont concaténés par ``index`` et
la liste assemblée est rendue sur le chunk ``finish_reason: "tool_calls"``.
``complete`` (non-streamé, pour compatibilité) est implémenté comme drain de
``stream_complete``.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Iterator

from .config import DEFAULT_SAMPLING


def stream_complete(
    messages: list[dict],
    model: str,
    base_url: str,
    max_tokens: int,
    sampling: dict | None = None,
    reasoning_format: str = "none",
    tools: list | None = None,
    api_key: str | None = None,
) -> Iterator[tuple[str | None, str | None, str | None, dict | None, list | None]]:
    """Appel ``/v1/chat/completions`` STREAMÉ (SSE, line-delimited).

    Rend à chaque chunk ``(reasoning, content, finish, usage, tool_calls)`` — les
    champs qui n'apparaissent pas dans le chunk valent ``None``. ``[DONE]`` clôt le
    flux. ``tool_calls`` (liste OpenAI ordonnée par ``index``, arguments JSON
    assemblés) n'est rendu que sur le chunk ``finish_reason: "tool_calls"``.

    `sampling` : dict de paramètres par modèle (socle Qwen par défaut).
    `tools` : spec OpenAI des outils (``{"role":"tool",…}`` non, ``{"type":"function",…}``
    oui) — si non vide, ``body["tools"]`` + ``body["tool_choice"]="auto"``.
    `reasoning_format` : passé au body **même à "none"** — sans ce champ,
    llama.cpp applique son extraction native du raisonnement (le think part dans
    `reasoning_content`, absent du content) et la trace
    ne peut pas être ré-injectée multi-tours. "none" explicite force le flux
    complet (tags [THINK]…[/THINK] compris) dans `content`, où l'engine
    l'extrait. (reasoning_format=deepseek était abandonné : il avalait la
    réponse multi-tours dans reasoning_content → contenu vide.)
    """
    if sampling is None:
        sampling = DEFAULT_SAMPLING
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if reasoning_format:
        body["reasoning_format"] = reasoning_format
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    body.update(sampling)
    headers = {"Content-Type": "application/json"}
    if api_key:
        # Endpoint distant protégé (ex. llama-swap `apiKeys:`) : clef en-tête
        # Bearer. Jamais loggée ni incluse dans les payloads du tuteur.
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        base_url + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        finish: str | None = None
        usage: dict | None = None
        tc_acc: dict[int, dict] = {}  # index -> {id, name, args} (fragments concaténés)
        for raw in r:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or [{}]
            delta = choices[0].get("delta") or {}
            fr = choices[0].get("finish_reason")
            if fr:
                finish = fr
            if chunk.get("usage"):
                usage = chunk["usage"]
            # Les arguments d'un tool_call arrivent par fragments SSE ; on les
            # concatène par index, en gardant id/name du premier chunk de l'index.
            for tc in delta.get("tool_calls") or []:
                index = int(tc.get("index", 0))
                acc = tc_acc.setdefault(index, {"id": None, "name": None, "args": ""})
                fn = tc.get("function") or {}
                if tc.get("id"):
                    acc["id"] = tc["id"]
                if fn.get("name"):
                    acc["name"] = fn["name"]
                if fn.get("arguments"):
                    acc["args"] += fn["arguments"]
            tool_calls = None
            if finish == "tool_calls":
                tool_calls = [
                    {
                        "id": tc_acc[i]["id"],
                        "type": "function",
                        "function": {"name": tc_acc[i]["name"], "arguments": tc_acc[i]["args"]},
                    }
                    for i in sorted(tc_acc)
                ]
            yield (
                delta.get("reasoning_content") or None,
                delta.get("content") or None,
                finish,
                usage,
                tool_calls,
            )


def complete(
    messages: list[dict],
    model: str,
    base_url: str,
    max_tokens: int,
    sampling: dict | None = None,
    reasoning_format: str = "none",
    api_key: str | None = None,
) -> tuple[str, str, str, dict]:
    """Appel non streamé (drain de ``stream_complete``) — compatibilité.

    Retourne (reasoning, content, finish_reason, usage).
    """
    reasoning: list[str] = []
    content: list[str] = []
    finish = ""
    usage: dict = {}
    for dr, dc, fr, us, _tc in stream_complete(
        messages, model, base_url, max_tokens, sampling, reasoning_format,
        api_key=api_key,
    ):
        if dr:
            reasoning.append(dr)
        if dc:
            content.append(dc)
        if fr:
            finish = fr
        if us:
            usage = us
    return ("".join(reasoning), "".join(content), finish, usage)
