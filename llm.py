"""LLM provider abstraction for the Home chat panel.

Design goals:

* One thin interface (`LLMProvider`) so we can swap between OpenAI-compatible
  endpoints (GitHub Models, Ollama, LM Studio, OpenAI, xAI, DeepSeek, ...)
  without touching call sites.
* No SDK dependencies beyond `httpx` — chat-completions is a stable format
  and pinning our own client keeps the surface area small.
* The tool-loop lives here (`run_chat_with_tools`) so route handlers stay
  short. The tool *implementations* live in `chat_tools.py` — this module
  only knows how to marshal JSON and cap iterations.

Security posture:

* The LLM can only invoke tools whose names appear in the registry passed in
  by the caller. Unknown names return an error message that the LLM sees on
  the next turn; they never reach Python code paths.
* This module never runs shell commands, opens files for write, or evaluates
  arbitrary strings. Tool args are validated by the tool's own parameter
  schema on the LLM side and by the tool handler on our side.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

import httpx


# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #

# One chat message. OpenAI-style: role in {"system","user","assistant","tool"}.
# For assistants with tool calls, ``tool_calls`` is a list of dicts matching
# the OpenAI schema. For "tool" role, ``tool_call_id`` links back to the call.
Message = dict[str, Any]

# A tool the LLM may invoke. ``handler`` is called with the decoded JSON args
# and must return a JSON-serializable value (dict/list/str/int/None).
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]           # JSON schema for arguments
    handler: Callable[[dict[str, Any]], Any]

    def as_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCallRecord:
    """Audit-log entry for one tool invocation (shown in the UI)."""
    name: str
    arguments: dict[str, Any]
    ok: bool
    result: Any = None
    error: str | None = None


@dataclass
class ChatResult:
    reply: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    provider: str = ""
    model: str = ""


# --------------------------------------------------------------------------- #
# Provider interface + OpenAI-compatible implementation
# --------------------------------------------------------------------------- #

class LLMProvider(Protocol):
    name: str
    model: str

    def chat_completion(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> Message: ...


class OpenAICompatProvider:
    """Works with any endpoint that speaks OpenAI's ``/chat/completions``.

    Verified endpoints:
      * GitHub Models  → https://models.github.ai/inference  (PAT with models:read)
      * OpenAI         → https://api.openai.com/v1
      * Ollama         → http://host:11434/v1                (any local model)
      * LM Studio      → http://host:1234/v1
      * xAI / DeepSeek → their documented base URLs
    """
    name = "openai-compat"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        extra_headers: dict[str, str] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._extra_headers = extra_headers or {}

    def chat_completion(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> Message:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self._extra_headers,
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(url, json=payload, headers=headers)
        if r.status_code >= 400:
            # Surface the error body — many providers put useful hints there.
            raise LLMError(f"{r.status_code} from {self.base_url}: {r.text[:500]}")
        data = r.json()
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError):
            raise LLMError(f"Unexpected response shape: {json.dumps(data)[:500]}")


class DisabledProvider:
    """Placeholder returned when no LLM is configured. Raises on use so the
    UI shows a clear 'not configured' message instead of silently failing."""
    name = "disabled"
    model = ""

    def chat_completion(self, messages, tools=None):
        raise LLMError(
            "LLM is not configured. Set LUIGI_WEB_LLM_API_KEY (and optionally "
            "LUIGI_WEB_LLM_BASE_URL and LUIGI_WEB_LLM_MODEL) and restart."
        )


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Provider factory
# --------------------------------------------------------------------------- #

def build_provider_from_env() -> LLMProvider:
    """Read env once at import time. Any change requires a restart.

    Env vars (all optional — if the API key is missing we return a disabled
    provider):

        LUIGI_WEB_LLM_PROVIDER    "openai" (default) or "disabled"
        LUIGI_WEB_LLM_BASE_URL    default: https://models.github.ai/inference
        LUIGI_WEB_LLM_API_KEY     required to enable the panel
        LUIGI_WEB_LLM_MODEL       default: openai/gpt-4o-mini
        LUIGI_WEB_LLM_TIMEOUT     seconds (default 60)
    """
    provider = os.environ.get("LUIGI_WEB_LLM_PROVIDER", "openai").strip().lower()
    api_key = os.environ.get("LUIGI_WEB_LLM_API_KEY", "").strip()
    if provider == "disabled" or not api_key:
        return DisabledProvider()

    base_url = os.environ.get(
        "LUIGI_WEB_LLM_BASE_URL",
        "https://models.github.ai/inference",
    ).strip()
    model = os.environ.get("LUIGI_WEB_LLM_MODEL", "openai/gpt-4o-mini").strip()
    timeout = float(os.environ.get("LUIGI_WEB_LLM_TIMEOUT", "60"))
    return OpenAICompatProvider(base_url=base_url, api_key=api_key,
                                model=model, timeout=timeout)


# --------------------------------------------------------------------------- #
# Tool loop
# --------------------------------------------------------------------------- #

# Hard cap on tool round-trips per user turn. A well-behaved model finishes in
# 1-3; anything more usually means it's confused or looping. Never remove.
MAX_TOOL_ITERATIONS = int(os.environ.get("LUIGI_WEB_LLM_MAX_TOOL_ITERATIONS", "5"))


def run_chat_with_tools(
    provider: LLMProvider,
    messages: list[Message],
    tools: dict[str, Tool],
) -> ChatResult:
    """Drive the provider through as many tool round-trips as needed.

    ``messages`` is *mutated* — the assistant + tool messages produced during
    the loop are appended in order, matching what OpenAI-compatible APIs
    require on subsequent turns. The caller can persist the mutated list to
    keep future turns coherent.
    """
    if isinstance(provider, DisabledProvider):
        # Preserve the disabled behavior instead of returning a mystery blank.
        return ChatResult(
            reply="Chat is not configured. See the Admin page for setup notes.",
            provider=provider.name, model=provider.model,
        )

    tool_schemas = [t.as_openai_schema() for t in tools.values()] if tools else None
    audit: list[ToolCallRecord] = []

    for _ in range(MAX_TOOL_ITERATIONS):
        assistant = provider.chat_completion(messages, tools=tool_schemas)
        messages.append(assistant)

        tool_calls = assistant.get("tool_calls") or []
        if not tool_calls:
            # Final response — return whatever text the model produced.
            reply = (assistant.get("content") or "").strip()
            return ChatResult(reply=reply, tool_calls=audit,
                              provider=provider.name, model=provider.model)

        for call in tool_calls:
            name = call.get("function", {}).get("name", "")
            raw_args = call.get("function", {}).get("arguments", "") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                args = {}

            tool = tools.get(name)
            if tool is None:
                record = ToolCallRecord(
                    name=name or "<missing>", arguments=args, ok=False,
                    error=f"unknown tool '{name}' — not in the allow-list",
                )
                audit.append(record)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps({"error": record.error}),
                })
                continue

            try:
                result = tool.handler(args)
                record = ToolCallRecord(name=name, arguments=args, ok=True, result=result)
                content = json.dumps(result, default=str)
            except Exception as exc:
                record = ToolCallRecord(name=name, arguments=args, ok=False,
                                        error=f"{type(exc).__name__}: {exc}")
                content = json.dumps({"error": record.error})
            audit.append(record)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": content,
            })

    # Fell off the loop — model kept requesting tools past the cap.
    return ChatResult(
        reply="(stopped after the tool-call cap — try rephrasing)",
        tool_calls=audit, provider=provider.name, model=provider.model,
    )


# --------------------------------------------------------------------------- #
# Per-session history (in-memory, cleared on restart)
# --------------------------------------------------------------------------- #

# A tiny bounded dict keyed by session cookie value. We intentionally do NOT
# persist history to disk — the chat is a scratchpad, not a system of record,
# and any real audit trail belongs on the DB writes the tools perform.
_HISTORY: dict[str, list[Message]] = {}
_HISTORY_MAX = 32           # keep only the most-recent N messages per session
_HISTORY_SESSIONS_MAX = 64  # evict oldest session when this many exist


def get_history(session_id: str) -> list[Message]:
    return _HISTORY.setdefault(session_id, [])


def append_history(session_id: str, new_msgs: Iterable[Message]) -> None:
    hist = get_history(session_id)
    hist.extend(new_msgs)
    # Trim from the front, but always keep the leading system message if present.
    if len(hist) > _HISTORY_MAX:
        head = hist[:1] if hist and hist[0].get("role") == "system" else []
        tail = hist[-(_HISTORY_MAX - len(head)):]
        _HISTORY[session_id] = head + tail
    if len(_HISTORY) > _HISTORY_SESSIONS_MAX:
        # Evict the arbitrarily-first key. Session IDs are opaque here.
        _HISTORY.pop(next(iter(_HISTORY)))


def reset_history(session_id: str) -> None:
    _HISTORY.pop(session_id, None)
