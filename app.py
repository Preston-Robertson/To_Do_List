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
            "week_of": monday.isoformat(),
            "today_iso": today.isoformat(),
        },
    )


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
        },
    )


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
