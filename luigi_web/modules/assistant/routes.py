"""Assistant HTTP controllers."""
from __future__ import annotations
from fastapi import APIRouter
import threading
from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse
from ...auth import require_auth
from ...auth import COOKIE_NAME as _AUTH_COOKIE

router = APIRouter()


_CHAT_LOCK = threading.Lock()


def _chat_session_id(request: Request) -> str:
    """Use the auth cookie itself as the chat session key. Falls back to the
    remote address so the panel still works for token/bearer-only clients."""
    sid = request.cookies.get(_AUTH_COOKIE)
    if sid:
        return f"cookie:{sid}"
    client = request.client.host if request.client else "unknown"
    return f"addr:{client}"


@router.get("/chat/panel", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def chat_panel(request: Request):
    from ... import application as host

    provider = host._LLM_PROVIDER
    with host._CHAT_LOCK:
        messages = [
            {"role": message["role"], "content": message["content"]}
            for message in host.llm_mod.get_history(_chat_session_id(request))
            if message.get("role") in {"user", "assistant"}
            and isinstance(message.get("content"), str)
            and message.get("content")
            and not message.get("tool_calls")
        ]
    return host.templates.TemplateResponse(
        "partials/chat_panel.html",
        {
            "request": request,
            "chat_enabled": not isinstance(provider, host.llm_mod.DisabledProvider),
            "chat_provider": getattr(provider, "name", "disabled"),
            "chat_model": getattr(provider, "model", ""),
            "chat_disabled_reason": getattr(provider, "reason", "Assistant is not configured"),
            "chat_messages": messages,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/chat", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def chat_send(request: Request, message: str = Form(...)):
    from ... import application as host

    host._require_v2()
    text = (message or "").strip()
    if not text:
        # Render nothing — HTMX will just no-op the swap.
        return HTMLResponse("")

    session_id = host._chat_session_id(request)
    # The provider is synchronous. A sync route runs in FastAPI's threadpool,
    # keeping slow LLM calls from freezing every other request. Serialize chat
    # turns so two rapid submissions cannot interleave one shared history.
    with host._CHAT_LOCK:
        history = host.llm_mod.get_history(session_id)
        if not history:
            history.append({"role": "system", "content": host.chat_tools.SYSTEM_PROMPT})
        turn_start = len(history)
        history.append({"role": "user", "content": text})

        try:
            result = host.llm_mod.run_chat_with_tools(host._LLM_PROVIDER, history, host._LLM_TOOLS)
            reply = result.reply or "(no response)"
            tool_calls = result.tool_calls
            error = None
        except host.llm_mod.LLMError as exc:
            # Remove the complete partial turn (user, assistant and any tool
            # messages), not merely whichever message happened to be last.
            del history[turn_start:]
            reply = ""
            tool_calls = []
            error = str(exc)
        finally:
            host.llm_mod.trim_history(session_id)

    return host.templates.TemplateResponse(
        "partials/chat_exchange.html",
        {
            "request": request,
            "user_message": text,
            "assistant_message": reply,
            "tool_calls": tool_calls,
            "error": error,
        },
    )


@router.post("/chat/reset", dependencies=[Depends(require_auth)])
def chat_reset(request: Request):
    from ... import application as host

    host.llm_mod.reset_history(host._chat_session_id(request))
    return HTMLResponse("")
