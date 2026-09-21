"""Public FastAPI host and shared compatibility APIs for Luigi Web modules."""

from __future__ import annotations
import os
import logging
import shlex
import subprocess
import sys
import signal
import threading
import time
import calendar as calendar_mod
from datetime import date, timedelta
from inspect import isawaitable
from pathlib import Path
from typing import Any
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from .core.static_assets import ModuleStaticFiles
from . import clock
from .core.module_registry import build_registry, mount_modules, start_modules, stop_modules
from .core.optional_modules import module_available, optional_module, require_module
from .core.templating import create_templates
from .auth import (
    COOKIE_NAME,
    CSRF_COOKIE_NAME,
    csrf_matches,
    csrf_token,
    finance_is_configured,
    is_authenticated,
    login_response,
    logout_response,
    require_auth,
    secure_cookies,
)

db = optional_module("luigi_web.modules.tasks.repository")
recurrence = optional_module("luigi_web.modules.tasks.recurrence")
task_events = optional_module("luigi_web.modules.tasks.events")

app = FastAPI(
    title="LuigiBot Web GUI",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

logger = logging.getLogger("luigi_web.app")

from .paths import PROJECT_ROOT, STATIC_DIR, TEMPLATES_DIR

app.mount("/static", ModuleStaticFiles(directory=str(STATIC_DIR)), name="static")

templates = create_templates()


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    """Double-submit CSRF protection for authenticated browser mutations."""
    if (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and request.url.path not in {"/login", "/logout"}
        and request.cookies.get(COOKIE_NAME)
        and not is_authenticated(None, request.headers.get("authorization"), None)
        and not csrf_matches(
            request.cookies.get(CSRF_COOKIE_NAME),
            request.headers.get("x-csrf-token"),
        )
    ):
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache", "Expires": "0", "Referrer-Policy": "no-referrer"} if request.url.path.startswith("/finance") else None
        return JSONResponse({"detail": "CSRF validation failed"}, status_code=403, headers=headers)

    try:
        response = await call_next(request)
    except Exception:
        if not request.url.path.startswith("/finance"):
            raise
        response = JSONResponse({"detail": "Finance is temporarily unavailable"}, status_code=503)
    if request.url.path.startswith("/finance"):
        if response.status_code == 422:
            response = JSONResponse({"detail": "Invalid Finance request. Check the submitted fields."}, status_code=422)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path.startswith("/feedback"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    if request.url.path.startswith("/characters"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    if request.url.path.startswith("/cards") and not request.url.path.endswith(".svg"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    if not request.cookies.get(CSRF_COOKIE_NAME):
        response.set_cookie(
            CSRF_COOKIE_NAME,
            csrf_token(),
            httponly=False,
            samesite="strict",
            secure=secure_cookies(),
            max_age=60 * 60 * 24 * 30,
            path="/",
        )
    return response


REPO_DIR = PROJECT_ROOT


def _asset_version() -> str:
    """Highest mtime among static assets — appended to <link>/<script> URLs
    so browsers stop serving stale CSS/JS after a code push. Recomputed on
    import (fine — every restart bumps the query string)."""
    static_dir = STATIC_DIR
    latest = 0.0
    if static_dir.exists():
        for p in static_dir.rglob("*"):
            if p.is_file():
                try:
                    m = p.stat().st_mtime
                    if m > latest:
                        latest = m
                except OSError:
                    pass
    return str(int(latest)) if latest else "0"


templates.env.globals["asset_version"] = _asset_version()

if module_available("luigi_web.modules.tasks"):
    templates.env.globals.update(
        reactivation_date=db.reactivation_date,
        WEEKDAY_LABELS=db.WEEKDAY_LABELS,
        recurring_days_list=db.recurring_days_list,
        recurring_days_labels=db.recurring_days_labels,
        recurrence_schedule_type=db.recurrence_schedule_type,
        recurrence_schedule_label=db.recurrence_schedule_label,
        MONTH_ORDINAL_OPTIONS=recurrence.MONTH_ORDINAL_OPTIONS,
        has_web_column=db.has_web_column,
        completion_day_policy=task_events.server_time_policy,
    )

_STARTUP_SCHEMA: dict[str, Any] = {"version": None, "error": None}
_RECURRENCE_ERROR: str | None = None


def recurrence_status() -> dict[str, Any]:
    occurrences = require_module("luigi_web.modules.tasks.occurrences")

    try:
        state = occurrences.scheduler_state()
    except ValueError:
        return {"owner": "invalid", "enabled": False, "message": "Recurring generation is disabled: invalid scheduler ownership.", "error": True}
    return {**state, "error": bool(_RECURRENCE_ERROR), "message": _RECURRENCE_ERROR or state["message"]}


def _module_enabled(module_id: str, request: Request | None = None) -> bool:
    application = request.scope.get("app", app) if request is not None else app
    return application.state.modules.is_enabled(module_id)


def _startup_schema_check() -> None:
    _STARTUP_SCHEMA.update(version=None, error=None)
    if not (_module_enabled("tasks") or _module_enabled("discipline")):
        return
    require_module("luigi_web.modules.tasks.repository")
    try:
        version = db.check_schema_version()
        _STARTUP_SCHEMA["version"] = version
        if version < 2:
            _STARTUP_SCHEMA["error"] = f"schema_version={version}; luigi-web requires 2"
            return
        db.ensure_web_columns()
        if _module_enabled("tasks"):
            _reactivate_recurring()
    except Exception:
        _STARTUP_SCHEMA["error"] = "Shared task storage unavailable"


@app.on_event("startup")
async def _startup_modules() -> None:
    _startup_schema_check()
    result = start_modules(app)
    if isawaitable(result):
        await result


@app.on_event("shutdown")
async def _shutdown_modules() -> None:
    result = stop_modules(app)
    if isawaitable(result):
        await result


def _require_v2() -> None:
    if _STARTUP_SCHEMA["error"]:
        raise HTTPException(status_code=503, detail=_STARTUP_SCHEMA["error"])


def _reactivate_recurring() -> None:
    """Generate due copies only when this host explicitly owns recurrence."""
    global _RECURRENCE_ERROR
    try:
        db.reactivate_due_recurring()
        _RECURRENCE_ERROR = None
    except Exception:
        _RECURRENCE_ERROR = "Recurring generation is unavailable. Completed occurrences were not reset."
        logger.warning("Recurring occurrence generation failed")


import json as _json
import secrets as _secrets
from datetime import datetime as _dt

_UNDO_TTL_SECONDS = 12

_UNDO_MAX_ENTRIES = 64

_UNDO_LOCK = threading.Lock()

_UNDO_QUEUE: dict[str, dict[str, Any]] = {}


def _sweep_undo(now_ts: float | None = None) -> None:
    """Drop expired snapshots. Also caps the queue size so a burst can't grow
    unbounded. Caller holds ``_UNDO_LOCK``."""
    ts = now_ts if now_ts is not None else time.monotonic()
    expired = [k for k, v in _UNDO_QUEUE.items() if v["expires_at"] <= ts]
    for k in expired:
        _UNDO_QUEUE.pop(k, None)
    if len(_UNDO_QUEUE) > _UNDO_MAX_ENTRIES:
        # Drop oldest first (dict is insertion-ordered).
        for k in list(_UNDO_QUEUE.keys())[: len(_UNDO_QUEUE) - _UNDO_MAX_ENTRIES]:
            _UNDO_QUEUE.pop(k, None)


def _stash_undo(
    table: str,
    snapshot: dict[str, Any],
    label: str,
    generated_task_uuids: list[str] | None = None,
    task_event_uuid: str | None = None,
    task_event_type: str | None = None,
    operation_records: dict[str, Any] | None = None,
) -> str:
    """Record a 'before' snapshot for a task-like mutation. Returns an
    opaque ``op_id`` the client uses to POST ``/undo/{op_id}`` within the
    TTL window."""
    op_id = _secrets.token_urlsafe(8)
    now = time.monotonic()
    with _UNDO_LOCK:
        _sweep_undo(now)
        _UNDO_QUEUE[op_id] = {
            "table": table,
            "snapshot": snapshot,
            "label": label,
            "generated_task_uuids": list(generated_task_uuids or []),
            "task_event_uuid": task_event_uuid,
            "task_event_type": task_event_type,
            "operation_records": operation_records or {},
            "expires_at": now + _UNDO_TTL_SECONDS,
        }
    return op_id


def _pop_undo(op_id: str) -> dict[str, Any] | None:
    with _UNDO_LOCK:
        _sweep_undo()
        return _UNDO_QUEUE.pop(op_id, None)


def _hx_trigger(**events: Any) -> str:
    """Encode an ``HX-Trigger`` header value. Pass keyword args where each
    value is either ``None`` (event with no detail) or a JSON-serializable
    dict / value used as the event ``detail``. Insertion order is preserved,
    which matters because HTMX fires events in that order — the ``showUndo``
    handler must run before any ``reloadBoard`` that would nav away."""
    return _json.dumps(events, default=str)


@app.get("/healthz")
def healthz():
    return {
        "status": "ok" if not _STARTUP_SCHEMA["error"] else "degraded",
        "schema_version": _STARTUP_SCHEMA["version"],
        "error": "Shared task storage unavailable" if _STARTUP_SCHEMA["error"] else None,
    }


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def login_submit(token: str = Form(...)):
    try:
        return login_response(token)
    except HTTPException:
        # Re-render the form with an error, keeping the status generic.
        return HTMLResponse(
            content=_login_error_html(), status_code=401
        )


def _login_error_html() -> str:
    asset_version = _asset_version()
    return (
        "<!doctype html><meta charset=utf-8><title>Login</title>"
        f"<link rel='icon' href='/static/icons/luigi-mark.svg?v={asset_version}' type='image/svg+xml'>"
        "<link rel='stylesheet' href='/static/css/app.css'>"
        "<main class='login-page'><form method='post' action='/login' class='login-form'>"
        f"<h1><img class='login-mark' src='/static/icons/luigi-mark.svg?v={asset_version}' alt=''> Luigi Web</h1>"
        "<p class='error'>Invalid token.</p>"
        "<label>Token <input type='password' name='token' autofocus required></label>"
        "<button type='submit'>Sign in</button></form></main>"
    )


@app.post("/logout")
def logout():
    return logout_response()


@app.get("/", dependencies=[Depends(require_auth)])
def root(request: Request):
    return RedirectResponse(url=request.app.state.modules.landing_path, status_code=303)


@app.get(
    "/command-palette",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def command_palette_results(request: Request, q: str = ""):
    """Global navigation/action search rendered into the Ctrl+K palette."""
    query = (q or "").strip()
    tasks: list[dict[str, Any]] = []
    disciplines: list[dict[str, Any]] = []
    games: list[dict[str, Any]] = []
    shows: list[dict[str, Any]] = []
    search_error = None
    if query:
        if _module_enabled("tasks", request):
            try:
                tasks = db.find_tasks_by_name(query, include_completed=True, limit=8)
            except Exception:
                search_error = "Task search unavailable"
        if _module_enabled("discipline", request):
            try:
                disciplines = db.search_disciplines(query, limit=5)
            except Exception:
                search_error = search_error or "Discipline search unavailable"
        if _module_enabled("media", request):
            try:
                media_service = getattr(sys.modules[__name__], "gnw")
                if media_service.is_enabled():
                    needle = query.lower()
                    games = [
                        item for item in media_service.list_items("games")
                        if needle in item["title"].lower()
                    ][:5]
                    shows = [
                        item for item in media_service.list_items("shows")
                        if needle in item["title"].lower()
                    ][:5]
            except Exception:
                search_error = search_error or "Media search unavailable"
    return templates.TemplateResponse(
        "partials/command_results.html",
        {
            "request": request,
            "query": query,
            "tasks": tasks,
            "disciplines": disciplines,
            "games": games,
            "shows": shows,
            "search_error": search_error,
        },
    )


_LEGACY_EXPORTS = {
    "_CHAT_LOCK": "assistant",
    "_GANTT_BAR_H": "planning",
    "_GANTT_CAT_H": "planning",
    "_GANTT_HEADER_H": "planning",
    "_GANTT_MIN_WIDTH": "planning",
    "_GANTT_ROW_H": "planning",
    "_HOT_RELOADABLE": "admin",
    "_available_years": "discipline",
    "_build_gantt": "planning",
    "_chat_session_id": "assistant",
    "_discipline_toggle_error": "discipline",
    "_evaluate_notifications": "tasks",
    "_form_dict": "tasks",
    "_git_branch": "admin",
    "_git_head_short": "admin",
    "_git_status_line": "admin",
    "_gnw_board": "media",
    "_gnw_columns": "media",
    "_gnw_section": "media",
    "_hot_reload_env": "admin",
    "_integration_result": "admin",
    "_kanban_columns": "tasks",
    "_llm_integration_health": "admin",
    "_operation_task": "tasks",
    "_operation_task_rows": "tasks",
    "_parse_iso_date": "planning",
    "_reconcile_operation_records": "tasks",
    "_run": "admin",
    "_status_slug": "planning",
    "_validate_recurring_form": "tasks",
    "_year_grid": "discipline",
    "activity_page": "planning",
    "admin_backup": "admin",
    "admin_env_save": "admin",
    "admin_gnw_credentials": "admin",
    "admin_integrations": "admin",
    "admin_page": "admin",
    "admin_restart": "admin",
    "admin_restore_commit": "admin",
    "admin_restore_form": "admin",
    "admin_restore_preview": "admin",
    "admin_update": "admin",
    "archive_page": "tasks",
    "calendar_page": "planning",
    "chat_reset": "assistant",
    "chat_send": "assistant",
    "dependency_create": "tasks",
    "dependency_delete": "tasks",
    "discipline_create": "discipline",
    "discipline_deactivate": "discipline",
    "discipline_delete": "discipline",
    "discipline_edit_form": "discipline",
    "discipline_new_form": "discipline",
    "discipline_page": "discipline",
    "discipline_today": "discipline",
    "discipline_toggle": "discipline",
    "discipline_update": "discipline",
    "follow_ups_create": "tasks",
    "follow_ups_delete": "tasks",
    "follow_ups_edit_form": "tasks",
    "follow_ups_new_form": "tasks",
    "follow_ups_page": "tasks",
    "follow_ups_panel": "tasks",
    "follow_ups_update": "tasks",
    "games_page": "media",
    "gnw_add_item": "media",
    "gnw_edit_form": "media",
    "gnw_new_form": "media",
    "gnw_pick": "media",
    "gnw_search": "media",
    "gnw_set_status": "media",
    "gnw_steam_stats": "media",
    "gnw_update": "media",
    "home_page": "planning",
    "media_insights_page": "media",
    "projects_page": "planning",
    "recurring_archive": "tasks",
    "recurring_create": "tasks",
    "recurring_delete": "tasks",
    "recurring_edit_form": "tasks",
    "recurring_new_form": "tasks",
    "recurring_page": "tasks",
    "recurring_restore": "tasks",
    "recurring_set_status": "tasks",
    "recurring_snooze": "tasks",
    "recurring_toggle_complete": "tasks",
    "recurring_update": "tasks",
    "reminder_dismiss": "tasks",
    "reminder_rule_create": "tasks",
    "reminder_rule_delete": "tasks",
    "reminder_snooze": "tasks",
    "reminders_count": "tasks",
    "reminders_page": "tasks",
    "review_page": "planning",
    "review_save": "planning",
    "shows_page": "media",
    "task_rules_page": "tasks",
    "tasks_archive": "tasks",
    "tasks_bulk": "tasks",
    "tasks_create": "tasks",
    "tasks_delete": "tasks",
    "tasks_edit_form": "tasks",
    "tasks_new_form": "tasks",
    "tasks_page": "tasks",
    "tasks_quick_create": "tasks",
    "tasks_restore": "tasks",
    "tasks_set_status": "tasks",
    "tasks_snooze": "tasks",
    "tasks_toggle_complete": "tasks",
    "tasks_update": "tasks",
    "undo": "tasks",
}

_LEGACY_MODULES = {
    "cards": "luigi_web.modules.cards.repository",
    "cards_scryfall": "luigi_web.modules.cards.scryfall",
    "cards_templating": "luigi_web.modules.cards.templating",
    "chat_tools": "luigi_web.modules.assistant.tools",
    "env_file": "luigi_web.modules.admin.environment",
    "finance": "luigi_web.modules.finance.repository",
    "gnw": "luigi_web.modules.media.service",
    "llm_mod": "luigi_web.modules.assistant.providers",
    "operations": "luigi_web.modules.tasks.operations",
    "review": "luigi_web.modules.planning.repository",
    "task_backup": "luigi_web.modules.tasks.backup",
}

_LAZY_STATE_LOCK = threading.RLock()


def __getattr__(name: str) -> Any:
    feature = _LEGACY_EXPORTS.get(name)
    if feature is not None:
        return getattr(require_module(f"luigi_web.modules.{feature}.routes"), name)
    module_path = _LEGACY_MODULES.get(name)
    if module_path is not None:
        return require_module(module_path)
    if name in {"_LLM_PROVIDER", "_LLM_TOOLS"}:
        with _LAZY_STATE_LOCK:
            if name not in globals():
                if name == "_LLM_PROVIDER":
                    value = require_module(_LEGACY_MODULES["llm_mod"]).build_provider_from_env()
                else:
                    value = require_module(_LEGACY_MODULES["chat_tools"]).build_registry()
                globals()[name] = value
            return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


from .auth import COOKIE_NAME as _AUTH_COOKIE  # keep import local — no top-of-file churn
from .core.modules_routes import router as modules_router
from .core.repository_routes import router as repository_router

app.include_router(modules_router, dependencies=[Depends(require_auth)])
app.include_router(repository_router, dependencies=[Depends(require_auth)])

mount_modules(app, build_registry())

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "luigi_web.application:app",
        host=os.environ.get("LUIGI_WEB_BIND", "0.0.0.0"),
        port=int(os.environ.get("LUIGI_WEB_PORT", "8080")),
        reload=False,
    )
