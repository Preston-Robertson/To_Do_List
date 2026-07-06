"""LuigiBot to-do web GUI — FastAPI app.

Server-rendered HTML + HTMX partials. Kanban board for tasks/recurring,
GitHub-style heatmap for discipline, plain table for follow-ups.

All routes except ``/healthz`` and the login pages require the shared token.
See ``auth.py`` for the auth model.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
from auth import login_response, logout_response, require_auth

app = FastAPI(title="LuigiBot Web GUI")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Repo root (used by the /admin update flow to run git/pip in the right place).
REPO_DIR = Path(__file__).resolve().parent

# LLM chat: build the provider + tool registry once at import. Provider is
# either a real OpenAI-compat client or a DisabledProvider that shows a
# friendly 'not configured' message on use. The registry is the *only* code
# path the LLM can reach — see chat_tools.py for the security contract.
import chat_tools
import llm as llm_mod
_LLM_PROVIDER = llm_mod.build_provider_from_env()
_LLM_TOOLS = chat_tools.build_registry()

import env_file


def _asset_version() -> str:
    """Highest mtime among static assets — appended to <link>/<script> URLs
    so browsers stop serving stale CSS/JS after a code push. Recomputed on
    import (fine — every restart bumps the query string)."""
    static_dir = REPO_DIR / "static"
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


# --------------------------------------------------------------------------- #
# Startup — refuse to serve if the DB schema isn't v2
# --------------------------------------------------------------------------- #
_STARTUP_SCHEMA: dict[str, Any] = {"version": None, "error": None}


@app.on_event("startup")
def _startup_schema_check() -> None:
    try:
        v = db.check_schema_version()
        _STARTUP_SCHEMA["version"] = v
        if v < 2:
            _STARTUP_SCHEMA["error"] = f"schema_version={v}; luigi-web requires 2"
    except Exception as exc:  # pragma: no cover — surfaced via /healthz
        _STARTUP_SCHEMA["error"] = f"schema check failed: {exc}"


def _require_v2() -> None:
    if _STARTUP_SCHEMA["error"]:
        raise HTTPException(status_code=503, detail=_STARTUP_SCHEMA["error"])


# --------------------------------------------------------------------------- #
# Public routes
# --------------------------------------------------------------------------- #

@app.get("/healthz")
def healthz():
    return {
        "status": "ok" if not _STARTUP_SCHEMA["error"] else "degraded",
        "schema_version": _STARTUP_SCHEMA["version"],
        "error": _STARTUP_SCHEMA["error"],
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
    return (
        "<!doctype html><meta charset=utf-8><title>Login</title>"
        "<link rel='stylesheet' href='/static/css/app.css'>"
        "<main class='login-page'><form method='post' action='/login' class='login-form'>"
        "<h1>LuigiBot Web GUI</h1>"
        "<p class='error'>Invalid token.</p>"
        "<label>Token <input type='password' name='token' autofocus required></label>"
        "<button type='submit'>Sign in</button></form></main>"
    )


@app.post("/logout")
def logout():
    return logout_response()


# --------------------------------------------------------------------------- #
# Root
# --------------------------------------------------------------------------- #

@app.get("/", dependencies=[Depends(require_auth)])
def root():
    return RedirectResponse(url="/home", status_code=303)


# --------------------------------------------------------------------------- #
# TASKS (Kanban)
# --------------------------------------------------------------------------- #

def _kanban_columns(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket task-like rows by status, preserving the fixed enum order."""
    columns = {s: [] for s in db.STATUS_VALUES}
    for row in rows:
        status = row.get("status") or "Not Started"
        columns.setdefault(status, []).append(row)
    return columns


@app.get("/tasks", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def tasks_page(request: Request):
    _require_v2()
    rows = db.list_tasks()
    return templates.TemplateResponse(
        "tasks.html",
        {
            "request": request,
            "active_nav": "tasks",
            "columns": _kanban_columns(rows),
            "statuses": db.STATUS_DISPLAY_ORDER,
            "endpoint_root": "/tasks",
            "page_title": "Tasks",
        },
    )


@app.post("/tasks", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def tasks_create(request: Request):
    _require_v2()
    form = dict(await request.form())
    row_uuid = db.create_task(form)
    row = db.get_task(row_uuid)
    return templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": "/tasks"},
        headers={"HX-Trigger": "closeModal"},
    )


@app.get(
    "/tasks/{row_uuid}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def tasks_edit_form(request: Request, row_uuid: str):
    _require_v2()
    row = db.get_task(row_uuid)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "partials/task_form.html",
        {
            "request": request,
            "t": row,
            "statuses": db.STATUS_VALUES,
            "endpoint_root": "/tasks",
            "is_new": False,
        },
    )


@app.get(
    "/tasks/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def tasks_new_form(request: Request):
    return templates.TemplateResponse(
        "partials/task_form.html",
        {
            "request": request,
            "t": {},
            "statuses": db.STATUS_VALUES,
            "endpoint_root": "/tasks",
            "is_new": True,
        },
    )


@app.post(
    "/tasks/{row_uuid}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
async def tasks_update(request: Request, row_uuid: str):
    _require_v2()
    form = dict(await request.form())
    db.update_task(row_uuid, form)
    row = db.get_task(row_uuid)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": "/tasks"},
        headers={"HX-Trigger": "closeModal,reloadBoard"},
    )


@app.post("/tasks/{row_uuid}/status", dependencies=[Depends(require_auth)])
async def tasks_set_status(request: Request, row_uuid: str):
    _require_v2()
    form = dict(await request.form())
    new_status = form.get("status", "")
    try:
        db.set_task_status(row_uuid, new_status)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return Response(status_code=204)


@app.post(
    "/tasks/{row_uuid}/complete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def tasks_toggle_complete(request: Request, row_uuid: str):
    _require_v2()
    db.toggle_task_completed(row_uuid)
    row = db.get_task(row_uuid)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": "/tasks"},
        headers={"HX-Trigger": "reloadBoard"},
    )


@app.post("/tasks/{row_uuid}/delete", dependencies=[Depends(require_auth)])
def tasks_delete(row_uuid: str):
    _require_v2()
    db.delete_task(row_uuid)
    # HTMX swaps the card with an empty response, removing it from the DOM.
    return Response(status_code=200, content="", headers={"HX-Trigger": "closeModal"})


@app.post(
    "/tasks/{row_uuid}/snooze",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
async def tasks_snooze(request: Request, row_uuid: str):
    _require_v2()
    form = dict(await request.form())
    try:
        days = int(form.get("days", "1"))
    except ValueError:
        raise HTTPException(400, "days must be an integer")
    if not db.snooze_task(row_uuid, days):
        raise HTTPException(404)
    row = db.get_task(row_uuid)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": "/tasks"},
    )


# --------------------------------------------------------------------------- #
# RECURRING TASKS (Kanban, same shape as tasks)
# --------------------------------------------------------------------------- #

@app.get("/recurring", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def recurring_page(request: Request):
    _require_v2()
    rows = db.list_recurring()
    return templates.TemplateResponse(
        "tasks.html",
        {
            "request": request,
            "active_nav": "recurring",
            "columns": _kanban_columns(rows),
            "statuses": db.STATUS_DISPLAY_ORDER,
            "endpoint_root": "/recurring",
            "page_title": "Recurring",
        },
    )


@app.post("/recurring", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def recurring_create(request: Request):
    _require_v2()
    form = dict(await request.form())
    row_uuid = db.create_recurring(form)
    row = db.get_recurring(row_uuid)
    return templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": "/recurring"},
        headers={"HX-Trigger": "closeModal"},
    )


@app.get(
    "/recurring/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def recurring_new_form(request: Request):
    return templates.TemplateResponse(
        "partials/task_form.html",
        {
            "request": request,
            "t": {},
            "statuses": db.STATUS_VALUES,
            "endpoint_root": "/recurring",
            "is_new": True,
        },
    )


@app.get(
    "/recurring/{row_uuid}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def recurring_edit_form(request: Request, row_uuid: str):
    _require_v2()
    row = db.get_recurring(row_uuid)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "partials/task_form.html",
        {
            "request": request,
            "t": row,
            "statuses": db.STATUS_VALUES,
            "endpoint_root": "/recurring",
            "is_new": False,
        },
    )


@app.post(
    "/recurring/{row_uuid}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
async def recurring_update(request: Request, row_uuid: str):
    _require_v2()
    form = dict(await request.form())
    db.update_recurring(row_uuid, form)
    row = db.get_recurring(row_uuid)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": "/recurring"},
        headers={"HX-Trigger": "closeModal,reloadBoard"},
    )


@app.post("/recurring/{row_uuid}/status", dependencies=[Depends(require_auth)])
async def recurring_set_status(request: Request, row_uuid: str):
    _require_v2()
    form = dict(await request.form())
    new_status = form.get("status", "")
    try:
        db.set_recurring_status(row_uuid, new_status)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return Response(status_code=204)


@app.post(
    "/recurring/{row_uuid}/complete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def recurring_toggle_complete(request: Request, row_uuid: str):
    _require_v2()
    db.toggle_recurring_completed(row_uuid)
    row = db.get_recurring(row_uuid)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": "/recurring"},
        headers={"HX-Trigger": "reloadBoard"},
    )


@app.post("/recurring/{row_uuid}/delete", dependencies=[Depends(require_auth)])
def recurring_delete(row_uuid: str):
    _require_v2()
    db.delete_recurring(row_uuid)
    return Response(status_code=200, content="", headers={"HX-Trigger": "closeModal"})


@app.post(
    "/recurring/{row_uuid}/snooze",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
async def recurring_snooze(request: Request, row_uuid: str):
    _require_v2()
    form = dict(await request.form())
    try:
        days = int(form.get("days", "1"))
    except ValueError:
        raise HTTPException(400, "days must be an integer")
    if not db.snooze_recurring(row_uuid, days):
        raise HTTPException(404)
    row = db.get_recurring(row_uuid)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": "/recurring"},
    )


# --------------------------------------------------------------------------- #
# DISCIPLINE
# --------------------------------------------------------------------------- #

def _year_grid(year: int) -> list[list[date | None]]:
    """Build a 7-row × ~53-col grid of ``date`` cells for a whole year.

    Column = ISO week starting Sunday; row 0 = Sunday .. row 6 = Saturday.
    ``None`` in a slot means "before Jan 1" or "after Dec 31" (padding).
    """
    first = date(year, 1, 1)
    last = date(year, 12, 31)
    # Align the first column to the Sunday on/before Jan 1.
    # Python's weekday(): Mon=0..Sun=6; we want Sun=0..Sat=6.
    def sun_index(d: date) -> int:
        return (d.weekday() + 1) % 7

    start = first - timedelta(days=sun_index(first))
    end = last + timedelta(days=(6 - sun_index(last)))
    weeks: list[list[date | None]] = []
    cur = start
    while cur <= end:
        week: list[date | None] = []
        for _ in range(7):
            week.append(cur if (first <= cur <= last) else None)
            cur += timedelta(days=1)
        weeks.append(week)
    # transpose to rows=day-of-week, cols=week
    rows: list[list[date | None]] = [[] for _ in range(7)]
    for w in weeks:
        for i, d in enumerate(w):
            rows[i].append(d)
    return rows


def _available_years() -> list[int]:
    """Years to show in the dropdown: from earliest completion → next year."""
    current = date.today().year
    with db.get_engine().connect() as conn:
        from sqlalchemy import text as _t
        row = conn.execute(
            _t("SELECT MIN(completed_date) AS mn FROM discipline_completions")
        ).first()
    earliest_str = row.mn if row and row.mn else None
    try:
        earliest = int(earliest_str[:4]) if earliest_str else current
    except (TypeError, ValueError):
        earliest = current
    start = min(earliest, current)
    return list(range(start, current + 2))


@app.get("/discipline", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def discipline_page(request: Request, year: int | None = None):
    _require_v2()
    if year is None:
        year = date.today().year
    disciplines = db.list_disciplines(include_inactive=True)
    completions = db.list_completions_for_year(year)
    # Attach year-specific completion sets + computed streak (from all-time in-year data).
    for d in disciplines:
        days = completions.get(d["task"], set())
        d["_year_days"] = days
        # Streak is computed against the CURRENT date, so use full history when
        # viewing the current year and just the year's data otherwise.
        if year == date.today().year:
            d["_streak"] = db.compute_streak(days)
        else:
            d["_streak"] = d.get("current_streak") or 0
    return templates.TemplateResponse(
        "discipline.html",
        {
            "request": request,
            "active_nav": "discipline",
            "page_title": "Discipline",
            "disciplines": disciplines,
            "year": year,
            "years": _available_years(),
            "grid": _year_grid(year),
            "today_iso": date.today().isoformat(),
        },
    )


@app.get("/discipline/new", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def discipline_new_form(request: Request):
    return templates.TemplateResponse(
        "partials/discipline_form.html",
        {"request": request, "d": {}, "is_new": True},
    )


@app.post("/discipline", dependencies=[Depends(require_auth)])
async def discipline_create(request: Request):
    _require_v2()
    form = dict(await request.form())
    db.create_discipline(form)
    # Full-page reload is fine here — the heatmap grid depends on the discipline list.
    return Response(
        status_code=204,
        headers={"HX-Trigger": "closeModal", "HX-Refresh": "true"},
    )


@app.get(
    "/discipline/{row_uuid}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def discipline_edit_form(request: Request, row_uuid: str):
    _require_v2()
    row = db.get_discipline(row_uuid)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "partials/discipline_form.html",
        {"request": request, "d": row, "is_new": False},
    )


@app.post("/discipline/{row_uuid}", dependencies=[Depends(require_auth)])
async def discipline_update(request: Request, row_uuid: str):
    _require_v2()
    form = dict(await request.form())
    db.update_discipline(row_uuid, form)
    return Response(
        status_code=204,
        headers={"HX-Trigger": "closeModal", "HX-Refresh": "true"},
    )


@app.post("/discipline/{row_uuid}/deactivate", dependencies=[Depends(require_auth)])
def discipline_deactivate(row_uuid: str):
    _require_v2()
    db.deactivate_discipline(row_uuid)
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@app.post("/discipline/toggle", dependencies=[Depends(require_auth)])
async def discipline_toggle(request: Request):
    """Mark or unmark a single (task, day) — HTMX target is the cell itself."""
    _require_v2()
    form = dict(await request.form())
    task = form.get("task", "")
    catagory = form.get("catagory") or None
    day = form.get("day", "")
    action = form.get("action", "toggle")
    if not task or not day:
        raise HTTPException(400, "task and day required")

    if action == "mark":
        db.mark_completion(task, catagory, day)
        marked = True
    elif action == "unmark":
        db.unmark_completion(task, day)
        marked = False
    else:
        # Default: probe current state and flip. We do a mark first, then delete
        # if it was already present — but ON CONFLICT DO NOTHING makes mark safe,
        # so a cleaner probe is to query completions for the day.
        with db.get_engine().begin() as conn:
            from sqlalchemy import text as _t
            existing = conn.execute(
                _t("SELECT 1 FROM discipline_completions WHERE task=:t AND completed_date=:d"),
                {"t": task, "d": day},
            ).first()
        if existing:
            db.unmark_completion(task, day)
            marked = False
        else:
            db.mark_completion(task, catagory, day)
            marked = True

    return templates.TemplateResponse(
        "partials/discipline_cell.html",
        {
            "request": request,
            "task": task,
            "catagory": catagory,
            "day": day,
            "marked": marked,
            "today_iso": date.today().isoformat(),
        },
    )


# --------------------------------------------------------------------------- #
# FOLLOW-UPS
# --------------------------------------------------------------------------- #

@app.get("/follow-ups", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def follow_ups_page(request: Request):
    _require_v2()
    rows = db.list_follow_ups()
    return templates.TemplateResponse(
        "follow_ups.html",
        {
            "request": request,
            "active_nav": "follow-ups",
            "page_title": "Follow-ups",
            "rows": rows,
        },
    )


@app.get("/follow-ups/new", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def follow_ups_new_form(request: Request):
    return templates.TemplateResponse(
        "partials/follow_up_form.html",
        {"request": request, "f": {}, "is_new": True},
    )


@app.post("/follow-ups", dependencies=[Depends(require_auth)])
async def follow_ups_create(request: Request):
    _require_v2()
    form = dict(await request.form())
    db.create_follow_up(form)
    return Response(status_code=204, headers={"HX-Trigger": "closeModal", "HX-Refresh": "true"})


@app.get(
    "/follow-ups/{row_uuid}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def follow_ups_edit_form(request: Request, row_uuid: str):
    _require_v2()
    row = db.get_follow_up(row_uuid)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "partials/follow_up_form.html",
        {"request": request, "f": row, "is_new": False},
    )


@app.post("/follow-ups/{row_uuid}", dependencies=[Depends(require_auth)])
async def follow_ups_update(request: Request, row_uuid: str):
    _require_v2()
    form = dict(await request.form())
    db.update_follow_up(row_uuid, form)
    return Response(status_code=204, headers={"HX-Trigger": "closeModal", "HX-Refresh": "true"})


@app.post("/follow-ups/{row_uuid}/delete", dependencies=[Depends(require_auth)])
def follow_ups_delete(row_uuid: str):
    _require_v2()
    db.delete_follow_up(row_uuid)
    return Response(status_code=200, content="", headers={"HX-Trigger": "closeModal"})


# --------------------------------------------------------------------------- #
# PROJECTS — Gantt chart, grouped by catagory
# --------------------------------------------------------------------------- #
# All layout math (px/day scale, swimlane y-coords, month gridlines) lives
# here in the route so the template only iterates over pre-shaped data. Keeps
# Jinja readable and makes the numbers unit-testable if we ever want to.

def _parse_iso_date(s: Any) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _status_slug(s: str | None) -> str:
    return (s or "not-started").lower().replace(" ", "-")


# Row/header/bar heights are used by both the SVG and the paired HTML name
# column, so the two panes stay row-aligned. Change here → change nowhere else.
_GANTT_HEADER_H = 42
_GANTT_ROW_H = 28
_GANTT_CAT_H = 32
_GANTT_BAR_H = 16
_GANTT_MIN_WIDTH = 900


def _build_gantt(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Shape a row set into everything projects.html needs to draw one SVG.

    Task placement rules:
      * ``end`` = ``due_date``. Tasks without one land in "unscheduled".
      * ``start`` = ``start_time`` if set, else ``task_creation``. If neither
        is usable (or start > end), we fall back to min(today, end) so the
        bar has a sensible width instead of collapsing to zero.
    """
    if not rows:
        return None

    from collections import defaultdict

    scheduled: list[dict[str, Any]] = []
    unscheduled: list[dict[str, Any]] = []
    today = date.today()

    for r in rows:
        end = _parse_iso_date(r.get("due_date"))
        if not end:
            unscheduled.append(r)
            continue
        start = _parse_iso_date(r.get("start_time")) or _parse_iso_date(r.get("task_creation"))
        if not start or start > end:
            start = min(today, end)
        entry = dict(r)
        entry["_start"] = start
        entry["_end"] = end
        scheduled.append(entry)

    if not scheduled and not unscheduled:
        return None

    if scheduled:
        chart_start = min(r["_start"] for r in scheduled)
        chart_end = max(r["_end"] for r in scheduled)
        chart_start = min(chart_start, today)
        chart_end = max(chart_end, today)
        # Padding so bars don't touch the panel edges.
        chart_start -= timedelta(days=3)
        chart_end += timedelta(days=3)
    else:
        # Only unscheduled — still produce a nominal axis so the template
        # doesn't have to handle a missing chart.
        chart_start = today - timedelta(days=30)
        chart_end = today + timedelta(days=30)

    span_days = max(1, (chart_end - chart_start).days)

    # Choose a base px/day per span, then stretch to at least _GANTT_MIN_WIDTH
    # so short-span charts don't render as a stubby column.
    if span_days <= 90:
        px_per_day: float = 12.0
    elif span_days <= 365:
        px_per_day = 5.0
    else:
        px_per_day = 2.0
    total_width = max(_GANTT_MIN_WIDTH, span_days * px_per_day)
    if span_days * px_per_day < _GANTT_MIN_WIDTH:
        px_per_day = _GANTT_MIN_WIDTH / span_days

    def x_for(d: date) -> float:
        return (d - chart_start).days * px_per_day

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in scheduled:
        groups[r.get("catagory") or "(none)"].append(r)

    swimlanes: list[dict[str, Any]] = []
    y = _GANTT_HEADER_H
    for cat_name in sorted(groups.keys()):
        tasks_in = groups[cat_name]
        cat_y = y
        y += _GANTT_CAT_H
        lane_tasks = []
        for t in tasks_in:
            x1 = x_for(t["_start"])
            x2 = x_for(t["_end"])
            lane_tasks.append({
                "task": t["task"],
                "status": t["status"] or "Not Started",
                "status_class": _status_slug(t["status"]),
                "priority": t.get("priority") or 0,
                "uuid": t["uuid"],
                "source": t.get("source", "task"),
                "catagory": t.get("catagory") or "",
                "start_iso": t["_start"].isoformat(),
                "end_iso": t["_end"].isoformat(),
                "bar_x": x1,
                "bar_y": y + (_GANTT_ROW_H - _GANTT_BAR_H) / 2,
                "bar_w": max(2.0, x2 - x1),
                "row_y": y,
            })
            y += _GANTT_ROW_H
        swimlanes.append({
            "catagory": cat_name,
            "count": len(tasks_in),
            "cat_y": cat_y,
            "y_start": cat_y,
            "y_end": y,
            "tasks": lane_tasks,
        })

    total_height = max(_GANTT_HEADER_H + 60, y + 8)

    # Month ticks — a vertical gridline + label on the first of each month.
    months: list[dict[str, Any]] = []
    d = date(chart_start.year, chart_start.month, 1)
    while d <= chart_end:
        if d >= chart_start:
            months.append({"x": x_for(d), "label": d.strftime("%b %Y")})
        d = date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)

    today_x = x_for(today) if chart_start <= today <= chart_end else None

    return {
        "total_width": total_width,
        "total_height": total_height,
        "px_per_day": px_per_day,
        "header_h": _GANTT_HEADER_H,
        "row_h": _GANTT_ROW_H,
        "cat_h": _GANTT_CAT_H,
        "bar_h": _GANTT_BAR_H,
        "swimlanes": swimlanes,
        "months": months,
        "today_x": today_x,
        "chart_start_iso": chart_start.isoformat(),
        "chart_end_iso": chart_end.isoformat(),
        "unscheduled": unscheduled,
        "scheduled_count": len(scheduled),
    }


@app.get("/projects", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def projects_page(request: Request):
    """Gantt-style view of open items grouped by ``catagory``.

    Category selection comes from the query string (repeated ``catagory``
    params). The page renders an empty state until at least one is picked,
    so first-time load stays snappy on large DBs.
    """
    _require_v2()
    selected = [c for c in request.query_params.getlist("catagory") if c]
    include_recurring = request.query_params.get("include_recurring", "1") == "1"

    all_categories = db.list_categories_with_open_tasks(
        include_recurring=include_recurring
    )
    rows = db.list_project_rows(selected, include_recurring=include_recurring)
    chart = _build_gantt(rows)

    return templates.TemplateResponse(
        "projects.html",
        {
            "request": request,
            "active_nav": "projects",
            "page_title": "Projects",
            "all_categories": all_categories,
            "selected_categories": set(selected),
            "include_recurring": include_recurring,
            "chart": chart,
            "today_iso": date.today().isoformat(),
        },
    )


# --------------------------------------------------------------------------- #
# HOME (customizable widget dashboard)
# --------------------------------------------------------------------------- #

@app.get("/home", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def home_page(request: Request):
    _require_v2()
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    open_tasks = db.list_open_tasks(limit=25)
    disciplines_pending = db.list_disciplines_pending_today()
    disc_week = db.weekly_discipline_counts(today)
    task_week = db.weekly_task_completion_counts(today)
    overdue_tasks = db.list_overdue_tasks(limit=10)
    upcoming_tasks = db.list_upcoming_tasks(days=7, limit=10)
    recent_completions = db.list_recent_completions(limit=8)
    discipline_streaks = db.list_discipline_streaks(limit=8)
    follow_ups = db.list_follow_ups_preview(limit=8)
    recent_activity = db.list_recent_activity(limit=15, days=14)
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "active_nav": "home",
            "page_title": "Home",
            "open_tasks": open_tasks,
            "disciplines_pending": disciplines_pending,
            "disc_week": disc_week,
            "task_week": task_week,
            "overdue_tasks": overdue_tasks,
            "upcoming_tasks": upcoming_tasks,
            "recent_completions": recent_completions,
            "discipline_streaks": discipline_streaks,
            "follow_ups": follow_ups,
            "recent_activity": recent_activity,
            "week_of": monday.isoformat(),
            "today_iso": today.isoformat(),
            "chat_enabled": not isinstance(_LLM_PROVIDER, llm_mod.DisabledProvider),
            "chat_provider": _LLM_PROVIDER.name,
            "chat_model": _LLM_PROVIDER.model,
        },
    )


# --------------------------------------------------------------------------- #
# CHAT — LLM-driven natural-language interface to the task tools
# --------------------------------------------------------------------------- #
# Security notes:
#   - The LLM can only invoke tools registered in chat_tools.build_registry().
#   - No shell, no eval, no filesystem write, no dynamic code loading.
#   - Chat history lives in-memory keyed by the session cookie value; a
#     restart clears everything.

from auth import COOKIE_NAME as _AUTH_COOKIE  # keep import local — no top-of-file churn


def _chat_session_id(request: Request) -> str:
    """Use the auth cookie itself as the chat session key. Falls back to the
    remote address so the panel still works for token/bearer-only clients."""
    sid = request.cookies.get(_AUTH_COOKIE)
    if sid:
        return f"cookie:{sid}"
    client = request.client.host if request.client else "unknown"
    return f"addr:{client}"


@app.post("/chat", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def chat_send(request: Request, message: str = Form(...)):
    _require_v2()
    text = (message or "").strip()
    if not text:
        # Render nothing — HTMX will just no-op the swap.
        return HTMLResponse("")

    session_id = _chat_session_id(request)
    history = llm_mod.get_history(session_id)
    if not history:
        history.append({"role": "system", "content": chat_tools.SYSTEM_PROMPT})
    history.append({"role": "user", "content": text})

    try:
        result = llm_mod.run_chat_with_tools(_LLM_PROVIDER, history, _LLM_TOOLS)
        reply = result.reply or "(no response)"
        tool_calls = result.tool_calls
        error = None
    except llm_mod.LLMError as exc:
        # Roll back the user message so a retry doesn't double-log.
        history.pop()
        reply = ""
        tool_calls = []
        error = str(exc)

    return templates.TemplateResponse(
        "partials/chat_exchange.html",
        {
            "request": request,
            "user_message": text,
            "assistant_message": reply,
            "tool_calls": tool_calls,
            "error": error,
        },
    )


@app.post("/chat/reset", dependencies=[Depends(require_auth)])
def chat_reset(request: Request):
    llm_mod.reset_history(_chat_session_id(request))
    return HTMLResponse("")


# --------------------------------------------------------------------------- #
# ADMIN — self-update (git pull + pip install) and restart
# --------------------------------------------------------------------------- #

def _git_head_short() -> str:
    """Best-effort short git SHA; returns empty string if git unavailable."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _git_status_line() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_DIR), "log", "-1", "--pretty=%h %s (%cr)"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _git_branch() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_DIR), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def admin_page(request: Request):
    env_path = env_file.env_file_path(REPO_DIR)
    writable, unwritable_reason = env_file.env_file_writable(env_path)
    try:
        current_env = env_file.read_env_file(env_path)
    except Exception as exc:  # e.g. permission error on read
        current_env = {}
        env_read_error = f"{type(exc).__name__}: {exc}"
    else:
        env_read_error = ""
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "active_nav": "admin",
            "page_title": "Admin",
            "repo_dir": str(REPO_DIR),
            "git_head": _git_head_short(),
            "git_branch": _git_branch(),
            "git_last": _git_status_line(),
            "python_exe": sys.executable,
            "schema_version": _STARTUP_SCHEMA["version"],
            "env_file_path": str(env_path),
            "env_file_exists": env_path.exists(),
            "env_writable": writable,
            "env_unwritable_reason": unwritable_reason,
            "env_read_error": env_read_error,
            "env_groups": env_file.grouped_view(current_env),
        },
    )


@app.post("/admin/env", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def admin_env_save(request: Request):
    """Save changes to the managed keys in the .env file.

    Contract:
      * Only keys in env_file.KNOWN_KEYS are accepted; anything else is
        rejected by env_file.update_env_file.
      * Secret fields sent empty mean 'keep the current value' — see the
        UI copy on the form. This avoids blanking a password by mistake.
      * The file itself does the atomic write; we just prepare the payload.
    """
    env_path = env_file.env_file_path(REPO_DIR)
    form = await request.form()

    try:
        current = env_file.read_env_file(env_path)
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/admin_env_result.html",
            {"request": request, "ok": False,
             "error": f"could not read {env_path}: {exc}",
             "changed": [], "unchanged_secrets": [],
             "hot_reloaded": [], "restart_needed": []},
        )

    updates: dict[str, str] = {}
    unchanged_secrets: list[str] = []
    for spec in env_file.KNOWN_KEYS:
        submitted = form.get(spec.name)
        if submitted is None:
            continue
        new_val = str(submitted)
        if spec.is_secret and new_val == "":
            # Blank secret = keep current. Only skip when the user actually
            # left it blank (submitted == "" but the field was sent).
            unchanged_secrets.append(spec.name)
            continue
        if new_val == current.get(spec.name, ""):
            continue  # nothing changed — skip the write
        updates[spec.name] = new_val

    if not updates:
        return templates.TemplateResponse(
            "partials/admin_env_result.html",
            {"request": request, "ok": True, "error": None,
             "changed": [], "unchanged_secrets": unchanged_secrets,
             "hot_reloaded": [], "restart_needed": []},
        )

    try:
        changed = env_file.update_env_file(env_path, updates, known_only=True)
    except env_file.EnvUpdateError as exc:
        return templates.TemplateResponse(
            "partials/admin_env_result.html",
            {"request": request, "ok": False, "error": str(exc),
             "changed": [], "unchanged_secrets": unchanged_secrets,
             "hot_reloaded": [], "restart_needed": []},
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/admin_env_result.html",
            {"request": request, "ok": False,
             "error": f"{type(exc).__name__}: {exc}",
             "changed": [], "unchanged_secrets": unchanged_secrets,
             "hot_reloaded": [], "restart_needed": []},
        )

    hot_reloaded, restart_needed = _hot_reload_env(changed, updates)

    return templates.TemplateResponse(
        "partials/admin_env_result.html",
        {"request": request, "ok": True, "error": None,
         "changed": changed, "unchanged_secrets": unchanged_secrets,
         "hot_reloaded": hot_reloaded, "restart_needed": restart_needed},
    )


# Keys we can safely apply live by mutating os.environ + rebuilding singletons.
# Anything not listed here still needs a systemctl restart to take effect.
_HOT_RELOADABLE = {
    "LUIGI_WEB_UI_TOKEN",           # auth.py reads os.environ per-request
    "LUIGI_WEB_LLM_PROVIDER",
    "LUIGI_WEB_LLM_BASE_URL",
    "LUIGI_WEB_LLM_API_KEY",
    "LUIGI_WEB_LLM_MODEL",
    "LUIGI_WEB_LLM_TIMEOUT",
    "LUIGI_WEB_LLM_MAX_TOOL_ITERATIONS",
}


def _hot_reload_env(changed: list[str], updates: dict[str, str]) -> tuple[list[str], list[str]]:
    """Push freshly-saved values into os.environ and rebuild any live singletons
    that depend on them. Returns (hot_reloaded_keys, restart_needed_keys)."""
    global _LLM_PROVIDER
    hot: list[str] = []
    cold: list[str] = []
    llm_touched = False
    for key in changed:
        if key in _HOT_RELOADABLE:
            os.environ[key] = updates[key]
            hot.append(key)
            if key.startswith("LUIGI_WEB_LLM_"):
                llm_touched = True
        else:
            cold.append(key)
    if llm_touched:
        _LLM_PROVIDER = llm_mod.build_provider_from_env()
    return hot, cold


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None,
         timeout: int = 180) -> tuple[int, str]:
    """Run a shell command, capture combined output, return (rc, text)."""
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except FileNotFoundError as exc:
        return 127, f"$ {' '.join(shlex.quote(c) for c in cmd)}\n{exc}"
    except subprocess.TimeoutExpired:
        return 124, f"$ {' '.join(shlex.quote(c) for c in cmd)}\nTIMEOUT after {timeout}s"
    combined = (proc.stdout or "") + (proc.stderr or "")
    header = f"$ {' '.join(shlex.quote(c) for c in cmd)}\n"
    return proc.returncode, header + combined


@app.post("/admin/update", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def admin_update(request: Request):
    """Pull latest git + reinstall requirements. Does NOT restart."""
    steps: list[dict[str, Any]] = []

    # Environment for subprocesses: force pip to be quiet-ish and cacheless so
    # systemd's ProtectHome=true doesn't trip us up.
    env = os.environ.copy()
    env["PIP_NO_CACHE_DIR"] = "1"
    env.setdefault("HOME", str(REPO_DIR))  # keep git happy under ProtectHome=true

    # 1. Verify this is a git checkout.
    if not (REPO_DIR / ".git").exists():
        steps.append({
            "name": "git check",
            "rc": 1,
            "out": f"{REPO_DIR} is not a git checkout. Cannot self-update.",
        })
        return templates.TemplateResponse(
            "partials/admin_update_result.html",
            {"request": request, "steps": steps, "ok": False, "restarted": False},
        )

    # 2. git fetch
    rc, out = _run(["git", "fetch", "--all", "--prune"], cwd=REPO_DIR, env=env)
    steps.append({"name": "git fetch", "rc": rc, "out": out})
    ok = rc == 0

    # 3. git pull (fast-forward only — refuse to auto-merge)
    if ok:
        rc, out = _run(["git", "pull", "--ff-only"], cwd=REPO_DIR, env=env)
        steps.append({"name": "git pull --ff-only", "rc": rc, "out": out})
        ok = rc == 0

    # 4. pip install -r requirements.txt (uses the running interpreter's venv)
    if ok:
        rc, out = _run(
            [sys.executable, "-m", "pip", "install", "--no-cache-dir",
             "-r", "requirements.txt"],
            cwd=REPO_DIR, env=env, timeout=600,
        )
        steps.append({"name": "pip install -r requirements.txt", "rc": rc, "out": out})
        ok = rc == 0

    return templates.TemplateResponse(
        "partials/admin_update_result.html",
        {"request": request, "steps": steps, "ok": ok, "restarted": False,
         "git_head": _git_head_short(), "git_last": _git_status_line()},
    )


@app.post("/admin/restart", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def admin_restart(request: Request):
    """Exit the process; systemd (Restart=always) brings it back with new code."""
    def _exit_soon() -> None:
        time.sleep(0.6)
        os._exit(0)

    threading.Thread(target=_exit_soon, daemon=True).start()
    return templates.TemplateResponse(
        "partials/admin_update_result.html",
        {"request": request, "steps": [], "ok": True, "restarted": True},
    )


@app.get("/admin/backup", dependencies=[Depends(require_auth)])
def admin_backup():
    """Full read-only JSON dump of the luigi_todo tables the GUI touches.

    Streams as a file attachment named ``luigi-backup-YYYYMMDD-HHMMSS.json``.
    Respects the "no whole-table rewrites, no DDL" contract — this is read
    traffic only and never writes back.
    """
    _require_v2()
    payload = db.export_backup()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"luigi-backup-{stamp}.json"
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# --------------------------------------------------------------------------- #
# Dev entry point
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.environ.get("LUIGI_WEB_BIND", "0.0.0.0"),
        port=int(os.environ.get("LUIGI_WEB_PORT", "8080")),
        reload=False,
    )
