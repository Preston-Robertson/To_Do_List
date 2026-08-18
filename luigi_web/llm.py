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

import asyncio
import contextlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

import httpx

from .paths import COPILOT_DATA_DIR


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
            try:
                r = client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise LLMError(f"Could not reach {self.base_url}: {exc}") from exc
        if r.status_code >= 400:
            # Surface the error body — many providers put useful hints there.
            raise LLMError(f"{r.status_code} from {self.base_url}: {r.text[:500]}")
        try:
            data = r.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise LLMError(
                f"Unexpected non-JSON response from {self.base_url}: {r.text[:500]}"
            ) from exc
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError):
            raise LLMError(f"Unexpected response shape: {json.dumps(data)[:500]}")


class CopilotSDKProvider:
    """GitHub Copilot subscription provider with no host-capability tools.

    The SDK runs in ``mode='empty'`` and receives only this application's
    custom tool allow-list. Copilot CLI shell, filesystem, web, MCP, skills,
    memory, and instruction discovery are never exposed to the model.
    """

    name = "github-copilot"

    def __init__(self, github_token: str | None, model: str, timeout: float,
                 base_directory: str):
        self.github_token = github_token
        self.model = model or "auto"
        self.timeout = timeout
        self.base_directory = base_directory

    def chat_completion(self, messages, tools=None):
        raise LLMError("Copilot SDK must use its isolated agent runner")

    @staticmethod
    def _prompt(messages: list[Message]) -> tuple[str, str]:
        system = next(
            (str(message.get("content") or "") for message in messages
             if message.get("role") == "system"),
            "You are a concise assistant.",
        )
        transcript: list[str] = []
        for message in messages:
            role = message.get("role")
            content = str(message.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                transcript.append(f"{str(role).title()}: {content}")
        return system, "\n\n".join(transcript)

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        detail = str(exc).lower()
        return "auth" in detail or "401" in detail or "403" in detail

    def _safe_error(self, exc: Exception, *, using_token: bool | None = None) -> str:
        detail = str(exc)
        if self.github_token:
            detail = detail.replace(self.github_token, "[redacted]")
        detail = detail[:500]
        if self._is_auth_error(exc):
            token_used = bool(self.github_token) if using_token is None else using_token
            if token_used:
                return (
                    "GitHub Copilot rejected the configured token. Replace "
                    "LUIGI_WEB_LLM_API_KEY with a supported token for an account "
                    "that has Copilot access, or clear it to use the service login."
                )
            return (
                "GitHub Copilot login is unavailable to the service account. "
                "Authenticate that account with GitHub Copilot or configure a "
                "supported LUIGI_WEB_LLM_API_KEY."
            )
        return f"GitHub Copilot SDK failed: {detail or type(exc).__name__}"

    async def _run_async(
        self,
        messages: list[Message],
        tools: dict[str, Tool],
    ) -> ChatResult:
        try:
            from copilot import CopilotClient
            from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
            from copilot.tools import Tool as CopilotTool
        except ImportError as exc:
            raise LLMError(
                "GitHub Copilot SDK is not installed; reinstall requirements.txt"
            ) from exc

        Path(self.base_directory).mkdir(parents=True, exist_ok=True)
        audit: list[ToolCallRecord] = []
        audit_lock = threading.Lock()
        tool_call_limit = _max_tool_iterations()
        tool_calls_started = 0
        cap_recorded = False

        def adapt_tool(tool: Tool):
            def handler(invocation):
                nonlocal tool_calls_started, cap_recorded
                raw_arguments = getattr(invocation, "arguments", {}) or {}
                if isinstance(raw_arguments, str):
                    try:
                        arguments = json.loads(raw_arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                else:
                    arguments = dict(raw_arguments)
                with audit_lock:
                    if tool_calls_started >= tool_call_limit:
                        error = f"tool-call cap reached ({tool_call_limit})"
                        if not cap_recorded:
                            audit.append(ToolCallRecord(
                                name=tool.name,
                                arguments=arguments,
                                ok=False,
                                error=error,
                            ))
                            cap_recorded = True
                        return json.dumps({"ok": False, "error": error})
                    tool_calls_started += 1
                try:
                    result = tool.handler(arguments)
                    record = ToolCallRecord(
                        name=tool.name, arguments=arguments, ok=True, result=result
                    )
                    response = {"ok": True, "result": result}
                except Exception as exc:  # noqa: BLE001
                    record = ToolCallRecord(
                        name=tool.name,
                        arguments=arguments,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    response = {"ok": False, "error": record.error}
                with audit_lock:
                    audit.append(record)
                return json.dumps(response, default=str)

            return CopilotTool(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
                handler=handler,
            )

        allowed_names = set(tools)

        def allow_custom_tools_only(request, invocation):
            tool_name = str(getattr(request, "tool_name", "") or "")
            if tool_name.split(":")[-1] in allowed_names:
                return PermissionDecisionApproveOnce()
            return PermissionDecisionReject(
                feedback="Only LuigiBot's allow-listed task tools are available"
            )

        system_message, prompt = self._prompt(messages)
        runtime_env = _copilot_runtime_env(self.base_directory)

        def build_client(github_token: str | None):
            extract_dir = runtime_env["COPILOT_CLI_EXTRACT_DIR"]
            previous_extract_dir = os.environ.get("COPILOT_CLI_EXTRACT_DIR")
            os.environ["COPILOT_CLI_EXTRACT_DIR"] = extract_dir
            try:
                return CopilotClient(
                    mode="empty",
                    github_token=github_token,
                    use_logged_in_user=not bool(github_token),
                    base_directory=self.base_directory,
                    env=runtime_env,
                    log_level="error",
                )
            finally:
                if previous_extract_dir is None:
                    os.environ.pop("COPILOT_CLI_EXTRACT_DIR", None)
                else:
                    os.environ["COPILOT_CLI_EXTRACT_DIR"] = previous_extract_dir

        async def run_client(github_token: str | None) -> ChatResult:
            client = build_client(github_token)
            session = None
            try:
                await asyncio.wait_for(client.start(), timeout=self.timeout)
                session = await client.create_session(
                    model=None if self.model == "auto" else self.model,
                    tools=[adapt_tool(tool) for tool in tools.values()],
                    available_tools=["custom:*"],
                    on_permission_request=allow_custom_tools_only,
                    system_message={"mode": "replace", "content": system_message},
                    enable_session_telemetry=False,
                    enable_session_store=False,
                    enable_config_discovery=False,
                    enable_file_hooks=False,
                    enable_host_git_operations=False,
                    enable_skills=False,
                    skip_custom_instructions=True,
                    mcp_servers={},
                )
                response = await session.send_and_wait(prompt, timeout=self.timeout)
                data = getattr(response, "data", None)
                reply = str(getattr(data, "content", "") or "").strip()
                if not reply:
                    reply = "(no response)"
                messages.append({"role": "assistant", "content": reply})
                return ChatResult(
                    reply=reply,
                    tool_calls=audit,
                    provider=self.name,
                    model=self.model,
                )
            finally:
                if session is not None:
                    with contextlib.suppress(Exception, BaseExceptionGroup):
                        await session.disconnect()
                with contextlib.suppress(Exception, BaseExceptionGroup):
                    await client.stop()

        try:
            return await run_client(self.github_token)
        except LLMError:
            raise
        except Exception as exc:
            if self.github_token and self._is_auth_error(exc) and not audit:
                try:
                    return await run_client(None)
                except Exception as fallback_exc:
                    fallback_message = self._safe_error(
                        fallback_exc, using_token=False
                    )
                    raise LLMError(
                        "GitHub Copilot rejected the configured token, and the "
                        f"service-login fallback also failed. {fallback_message}"
                    ) from fallback_exc
            raise LLMError(self._safe_error(exc)) from exc

    def run_chat(
        self,
        messages: list[Message],
        tools: dict[str, Tool],
    ) -> ChatResult:
        return asyncio.run(self._run_async(messages, tools))


class DisabledProvider:
    """Placeholder returned when no LLM is configured. Raises on use so the
    UI shows a clear 'not configured' message instead of silently failing."""
    name = "disabled"
    model = ""

    def __init__(self, reason: str | None = None):
        self.reason = reason or (
            "LLM is not configured. Choose a provider and add its credentials "
            "on the Admin page."
        )

    def chat_completion(self, messages, tools=None):
        raise LLMError(self.reason)


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Provider factory
# --------------------------------------------------------------------------- #

def build_provider_from_env() -> LLMProvider:
    """Read env once at import time. Any change requires a restart.

    Env vars (all optional — if the API key is missing we return a disabled
    provider):

        LUIGI_WEB_LLM_PROVIDER    "copilot", "openai", or "disabled"
        LUIGI_WEB_LLM_BASE_URL    OpenAI-compatible base URL
        LUIGI_WEB_LLM_API_KEY     GitHub token or provider API key
        LUIGI_WEB_LLM_MODEL       Copilot model (blank = auto) or API model
        LUIGI_WEB_LLM_TIMEOUT     seconds (default 60)
    """
    provider = os.environ.get("LUIGI_WEB_LLM_PROVIDER", "disabled").strip().lower()
    api_key = os.environ.get("LUIGI_WEB_LLM_API_KEY", "").strip()
    if provider == "disabled":
        return DisabledProvider()
    try:
        timeout = float(os.environ.get("LUIGI_WEB_LLM_TIMEOUT", "60"))
    except (TypeError, ValueError):
        timeout = 60.0
    timeout = min(max(timeout, 1.0), 600.0)

    configured_base_url = os.environ.get("LUIGI_WEB_LLM_BASE_URL", "").strip()
    retired_github_models = (
        configured_base_url.rstrip("/").lower()
        == "https://models.github.ai/inference"
    )
    if provider == "copilot" or retired_github_models:
        model = os.environ.get("LUIGI_WEB_LLM_MODEL", "").strip()
        if model.startswith("openai/") or model.startswith("<"):
            model = ""
        base_directory = os.environ.get("LUIGI_WEB_COPILOT_HOME", "").strip()
        if not base_directory:
            base_directory = str(COPILOT_DATA_DIR)
        return CopilotSDKProvider(
            github_token=api_key or None,
            model=model,
            timeout=timeout,
            base_directory=base_directory,
        )

    if provider != "openai":
        return DisabledProvider(
            f"Unsupported LLM provider '{provider}'. Use copilot, openai, or disabled."
        )
    if not api_key:
        return DisabledProvider("OpenAI-compatible provider API key is not configured.")
    base_url = configured_base_url or "https://api.openai.com/v1"
    model = os.environ.get("LUIGI_WEB_LLM_MODEL", "gpt-4o-mini").strip()
    if base_url.startswith("https://api.openai.com/") and model.startswith("openai/"):
        model = model.removeprefix("openai/")
    return OpenAICompatProvider(base_url=base_url, api_key=api_key,
                                model=model, timeout=timeout)


# --------------------------------------------------------------------------- #
# Tool loop
# --------------------------------------------------------------------------- #

# Hard cap on tool round-trips per user turn. A well-behaved model finishes in
# 1-3; anything more usually means it's confused or looping. Never remove.
def _max_tool_iterations() -> int:
    """Read the cap per turn so Admin hot-reloads actually take effect."""
    try:
        value = int(os.environ.get("LUIGI_WEB_LLM_MAX_TOOL_ITERATIONS", "5"))
    except (TypeError, ValueError):
        value = 5
    return min(max(value, 1), 20)


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
    if isinstance(provider, CopilotSDKProvider):
        return provider.run_chat(messages, tools)
    if isinstance(provider, DisabledProvider):
        # Preserve the disabled behavior instead of returning a mystery blank.
        return ChatResult(
            reply=provider.reason,
            provider=provider.name, model=provider.model,
        )

    tool_schemas = [t.as_openai_schema() for t in tools.values()] if tools else None
    audit: list[ToolCallRecord] = []

    for _ in range(_max_tool_iterations()):
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
_HISTORY_MAX = 64           # keep only the most-recent N messages per session
_HISTORY_SESSIONS_MAX = 64  # evict oldest session when this many exist
_HISTORY_LOCK = threading.RLock()

_COPILOT_ENV_ALLOW_LIST = {
    "ALL_PROXY", "APPDATA", "COMSPEC", "GH_CONFIG_DIR", "GH_HOST",
    "GITHUB_CONFIG_DIR", "HOME", "HTTPS_PROXY", "HTTP_PROXY", "LANG",
    "LC_ALL", "LOCALAPPDATA", "NO_PROXY", "PATH", "PATHEXT",
    "SSL_CERT_DIR", "SSL_CERT_FILE", "SYSTEMROOT", "TEMP", "TMP",
    "USERPROFILE", "WINDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
}


def _copilot_runtime_env(base_directory: str) -> dict[str, str]:
    runtime_env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _COPILOT_ENV_ALLOW_LIST and value
    }
    runtime_env["COPILOT_CLI_EXTRACT_DIR"] = str(
        Path(base_directory) / "runtime"
    )
    return runtime_env


def get_history(session_id: str) -> list[Message]:
    with _HISTORY_LOCK:
        # Pop/reinsert makes dict order an inexpensive least-recently-used
        # order rather than arbitrary creation order.
        hist = _HISTORY.pop(session_id, [])
        _HISTORY[session_id] = hist
        while len(_HISTORY) > _HISTORY_SESSIONS_MAX:
            _HISTORY.pop(next(iter(_HISTORY)))
        return hist


def trim_history(session_id: str) -> None:
    """Enforce the bound without starting history on an orphan tool message."""
    with _HISTORY_LOCK:
        hist = _HISTORY.get(session_id)
        if hist is None or len(hist) <= _HISTORY_MAX:
            return
        head = hist[:1] if hist[0].get("role") == "system" else []
        body = hist[len(head):]
        budget = _HISTORY_MAX - len(head)
        cutoff = max(0, len(body) - budget)
        # Prefer the next complete user turn. If the newest single turn itself
        # crosses the cutoff, retain that whole turn; the tool-loop cap keeps
        # one turn below this history budget.
        start = next(
            (i for i in range(cutoff, len(body)) if body[i].get("role") == "user"),
            None,
        )
        if start is None:
            start = next(
                (i for i in range(cutoff, -1, -1) if body[i].get("role") == "user"),
                cutoff,
            )
        tail = body[start:]
        _HISTORY[session_id] = head + tail


def append_history(session_id: str, new_msgs: Iterable[Message]) -> None:
    with _HISTORY_LOCK:
        get_history(session_id).extend(new_msgs)
        trim_history(session_id)


def reset_history(session_id: str) -> None:
    with _HISTORY_LOCK:
        _HISTORY.pop(session_id, None)
