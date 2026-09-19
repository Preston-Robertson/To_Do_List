"""Admin HTTP controllers."""
from __future__ import annotations
from fastapi import APIRouter
import os
import shlex
import subprocess
import sys
import signal
import threading
import time
from pathlib import Path
from typing import Any
from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from ...auth import require_auth
from datetime import datetime as _dt

router = APIRouter()


def _require_tasks(request: Request | None = None) -> None:
    from ... import application as host

    if not host._module_enabled("tasks", request):
        raise HTTPException(404, "Tasks module is disabled")


def _git_head_short() -> str:
    """Best-effort short git SHA; returns empty string if git unavailable."""
    from ... import application as host

    try:
        out = subprocess.run(
            ["git", "-C", str(host.REPO_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _git_status_line() -> str:
    from ... import application as host

    try:
        out = subprocess.run(
            ["git", "-C", str(host.REPO_DIR), "log", "-1", "--pretty=%h %s (%cr)"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _git_branch() -> str:
    from ... import application as host

    try:
        out = subprocess.run(
            ["git", "-C", str(host.REPO_DIR), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


@router.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def admin_page(request: Request):
    from ... import application as host

    env_path = host.env_file.env_file_path(host.REPO_DIR)
    writable, unwritable_reason = host.env_file.env_file_writable(env_path)
    try:
        current_env = host.env_file.read_env_file(env_path)
    except Exception as exc:  # e.g. permission error on read
        current_env = {}
        env_read_error = f"{type(exc).__name__}: {exc}"
    else:
        env_read_error = ""
    return host.templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "active_nav": "admin",
            "page_title": "Admin",
            "repo_dir": str(host.REPO_DIR),
            "git_head": host._git_head_short(),
            "git_branch": host._git_branch(),
            "git_last": host._git_status_line(),
            "python_exe": sys.executable,
            "schema_version": host._STARTUP_SCHEMA["version"],
            "recurrence_status": host.recurrence_status() if host._module_enabled("tasks", request) else None,
            "env_file_path": str(env_path),
            "env_file_exists": env_path.exists(),
            "env_writable": writable,
            "env_unwritable_reason": unwritable_reason,
            "env_read_error": env_read_error,
            "env_groups": host.env_file.grouped_view(current_env),
            "protected_env_keys_present": sorted(
                host.env_file.PROTECTED_KEYS.intersection(current_env)
            ),
            "gnw": host.gnw.credentials_status() if host._module_enabled("media", request) else {},
        },
    )


def _integration_result(name: str, check) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        detail = str(check() or "connected")
        return {
            "name": name,
            "ok": True,
            "detail": detail,
            "ms": round((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": name,
            "ok": False,
            "detail": f"{type(exc).__name__}: {exc}",
            "ms": round((time.perf_counter() - started) * 1000),
        }


def _llm_integration_health() -> str:
    from ... import application as host

    if isinstance(host._LLM_PROVIDER, host.llm_mod.DisabledProvider):
        raise RuntimeError(host._LLM_PROVIDER.reason)
    if isinstance(host._LLM_PROVIDER, host.llm_mod.CopilotSDKProvider):
        return host._LLM_PROVIDER.check_ready()
    return f"{host._LLM_PROVIDER.name} · {host._LLM_PROVIDER.model}"


@router.get("/admin/integrations", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def admin_integrations(request: Request):
    """Run bounded, read-only checks without requiring server-terminal access."""
    from ... import application as host

    import httpx
    from sqlalchemy import text as sql_text

    def check_db():
        with host.db.get_engine().connect() as conn:
            return f"query returned {conn.execute(sql_text('SELECT 1')).scalar_one()}"

    def check_sheets():
        reason = host.gnw.disabled_reason()
        if reason:
            raise RuntimeError(reason)
        sheet = host.gnw._get_sheet()
        return f"sheet: {sheet.title}"

    def check_discipline():
        return host.db.discipline_storage_health()

    def check_task_events():
        return host.db.task_events_storage_health()

    def check_tvmaze():
        response = httpx.get("https://api.tvmaze.com/shows/1", timeout=8)
        response.raise_for_status()
        return "catalog reachable"

    def check_anilist():
        data = host.gnw._anilist_request("query { Media(id: 1) { id } }", {})
        if not data.get("Media"):
            raise RuntimeError("unexpected response")
        return "catalog reachable"

    def check_youtube():
        if not os.environ.get("LUIGI_WEB_YOUTUBE_API_KEY", "").strip():
            raise RuntimeError("optional API key not configured")
        return "playlist search configured"

    def check_steam():
        response = httpx.get(
            "https://store.steampowered.com/api/appdetails",
            params={"appids": 10, "cc": "us", "l": "en"}, timeout=8,
        )
        response.raise_for_status()
        progress = bool(os.environ.get("LUIGI_WEB_STEAM_API_KEY") and os.environ.get("LUIGI_WEB_STEAM_ID"))
        return "store reachable; progress configured" if progress else "store reachable; progress not configured"

    def check_git():
        head = host._git_head_short()
        if not head:
            raise RuntimeError("git checkout unavailable")
        return f"{host._git_branch()} · {head}"

    def check_env():
        path = host.env_file.env_file_path(host.REPO_DIR)
        writable, reason = host.env_file.env_file_writable(path)
        if not writable:
            raise RuntimeError(reason)
        return f"writable: {path}"

    def check_finance():
        if not host.finance_is_configured():
            raise RuntimeError("separate Finance token not configured")
        return host.finance.storage_health()

    def check_operations():
        return host.operations.storage_health()

    probes = (
        ("PostgreSQL", ("tasks", "discipline"), check_db),
        ("Discipline storage", ("discipline",), check_discipline),
        ("Task event history", ("tasks",), check_task_events),
        ("Google Sheets", ("media",), check_sheets),
        ("Steam", ("media",), check_steam),
        ("TVMaze", ("media",), check_tvmaze),
        ("AniList", ("media",), check_anilist),
        ("YouTube", ("media",), check_youtube),
        ("LLM", ("assistant",), lambda: host._llm_integration_health()),
        ("Git checkout", (), check_git),
        ("Environment file", (), check_env),
        ("Finance storage", ("finance",), check_finance),
        ("Task rules and reminders", ("tasks",), check_operations),
    )
    checks = [
        host._integration_result(name, check)
        for name, module_ids, check in probes
        if not module_ids or any(host._module_enabled(module_id, request) for module_id in module_ids)
    ]
    return host.templates.TemplateResponse(
        "partials/admin_integrations.html",
        {"request": request, "checks": checks, "checked_at": _dt.now().strftime("%H:%M:%S")},
    )


@router.post("/admin/gnw-credentials", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def admin_gnw_credentials(request: Request):
    """Save a pasted Game'N'Watch service-account credentials.json.

    Writes it to the app-managed path (see gnw.credentials_path) so no
    host-side file placement is needed, then hot-reloads the Sheets client.
    """
    from ... import application as host

    if not host._module_enabled("media", request):
        raise HTTPException(404, "Media module is disabled")
    form = await request.form()
    raw = form.get("credentials", "")
    ok, message = host.gnw.save_credentials(raw if isinstance(raw, str) else "")
    return host.templates.TemplateResponse(
        "partials/admin_gnw_result.html",
        {"request": request, "ok": ok, "message": message,
         "status": host.gnw.credentials_status()},
    )


@router.post("/admin/env", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def admin_env_save(request: Request):
    """Save changes to the managed keys in the .env file.

    Contract:
      * Only keys in env_file.KNOWN_KEYS are accepted; anything else is
        rejected by env_file.update_env_file.
      * Secret fields sent empty mean 'keep the current value' — see the
        UI copy on the form. This avoids blanking a password by mistake.
      * The file itself does the atomic write; we just prepare the payload.
    """
    from ... import application as host

    env_path = host.env_file.env_file_path(host.REPO_DIR)
    form = await request.form()

    try:
        current = host.env_file.read_env_file(env_path)
    except Exception:
        host.logger.error("Could not read the environment file")
        return host.templates.TemplateResponse(
            "partials/admin_env_result.html",
            {"request": request, "ok": False,
             "error": "Could not read the environment file",
             "changed": [], "unchanged_secrets": [],
             "hot_reloaded": [], "restart_needed": []},
        )

    updates: dict[str, str] = {}
    unchanged_secrets: list[str] = []
    for spec in host.env_file.KNOWN_KEYS:
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
        return host.templates.TemplateResponse(
            "partials/admin_env_result.html",
            {"request": request, "ok": True, "error": None,
             "changed": [], "unchanged_secrets": unchanged_secrets,
             "hot_reloaded": [], "restart_needed": []},
        )

    try:
        changed = host.env_file.update_env_file(env_path, updates, known_only=True)
    except host.env_file.EnvUpdateError as exc:
        return host.templates.TemplateResponse(
            "partials/admin_env_result.html",
            {"request": request, "ok": False, "error": str(exc),
             "changed": [], "unchanged_secrets": unchanged_secrets,
             "hot_reloaded": [], "restart_needed": []},
        )
    except Exception:
        host.logger.error("Could not update the environment file")
        return host.templates.TemplateResponse(
            "partials/admin_env_result.html",
            {"request": request, "ok": False,
             "error": "Could not update the environment file",
             "changed": [], "unchanged_secrets": unchanged_secrets,
             "hot_reloaded": [], "restart_needed": []},
        )

    hot_reloaded, restart_needed = host._hot_reload_env(changed, updates)

    return host.templates.TemplateResponse(
        "partials/admin_env_result.html",
        {"request": request, "ok": True, "error": None,
         "changed": changed, "unchanged_secrets": unchanged_secrets,
         "hot_reloaded": hot_reloaded, "restart_needed": restart_needed},
    )


_HOT_RELOADABLE = {
    "LUIGI_WEB_LLM_PROVIDER",
    "LUIGI_WEB_LLM_BASE_URL",
    "LUIGI_WEB_LLM_API_KEY",
    "LUIGI_WEB_LLM_MODEL",
    "LUIGI_WEB_LLM_TIMEOUT",
    "LUIGI_WEB_LLM_MAX_TOOL_ITERATIONS",
    "LUIGI_WEB_COPILOT_HOME",
    "LUIGI_WEB_STEAM_API_KEY",
    "LUIGI_WEB_STEAM_ID",
    "LUIGI_WEB_YOUTUBE_API_KEY",
    "LUIGI_WEB_DAY_CUTOFF",
    "LUIGI_WEB_TIMEZONE",
}


def _hot_reload_env(changed: list[str], updates: dict[str, str]) -> tuple[list[str], list[str]]:
    """Push freshly-saved values into os.environ and rebuild any live singletons
    that depend on them. Returns (hot_reloaded_keys, restart_needed_keys)."""
    from ... import application as host

    hot: list[str] = []
    cold: list[str] = []
    llm_touched = False
    for key in changed:
        if key in host._HOT_RELOADABLE:
            os.environ[key] = updates[key]
            hot.append(key)
            if key.startswith("LUIGI_WEB_LLM_") or key == "LUIGI_WEB_COPILOT_HOME":
                llm_touched = True
        else:
            cold.append(key)
    if llm_touched and host._module_enabled("assistant"):
        host._LLM_PROVIDER = host.llm_mod.build_provider_from_env()
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


@router.post("/admin/update", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def admin_update(request: Request):
    """Pull latest git + reinstall requirements. Does NOT restart."""
    from ... import application as host

    steps: list[dict[str, Any]] = []

    # Environment for subprocesses: force pip to be quiet-ish and cacheless so
    # systemd's ProtectHome=true doesn't trip us up.
    env = os.environ.copy()
    env["PIP_NO_CACHE_DIR"] = "1"
    env.setdefault("HOME", str(host.REPO_DIR))  # keep git happy under ProtectHome=true

    # 1. Verify this is a git checkout.
    if not (host.REPO_DIR / ".git").exists():
        steps.append({
            "name": "git check",
            "rc": 1,
            "out": f"{host.REPO_DIR} is not a git checkout. Cannot self-update.",
        })
        return host.templates.TemplateResponse(
            "partials/admin_update_result.html",
            {"request": request, "steps": steps, "ok": False, "restarted": False},
        )

    # 2. git fetch
    rc, out = host._run(["git", "fetch", "--all", "--prune"], cwd=host.REPO_DIR, env=env)
    steps.append({"name": "git fetch", "rc": rc, "out": out})
    ok = rc == 0

    # 3. git pull (fast-forward only — refuse to auto-merge)
    if ok:
        rc, out = host._run(["git", "pull", "--ff-only"], cwd=host.REPO_DIR, env=env)
        steps.append({"name": "git pull --ff-only", "rc": rc, "out": out})
        ok = rc == 0

    # 4. pip install -r requirements.txt (uses the running interpreter's venv)
    if ok:
        rc, out = host._run(
            [sys.executable, "-m", "pip", "install", "--no-cache-dir",
             "-r", "requirements.txt"],
            cwd=host.REPO_DIR, env=env, timeout=600,
        )
        steps.append({"name": "pip install -r requirements.txt", "rc": rc, "out": out})
        ok = rc == 0

    return host.templates.TemplateResponse(
        "partials/admin_update_result.html",
        {"request": request, "steps": steps, "ok": ok, "restarted": False,
         "git_head": host._git_head_short(), "git_last": host._git_status_line()},
    )


@router.post("/admin/restart", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def admin_restart(request: Request):
    """Exit the process; systemd (Restart=always) brings it back with new code.

    ``os._exit(0)`` on its own only kills the *current* uvicorn worker — the
    other worker(s) keep serving stale bytecode after a code pull, which
    makes new-code and old-code behaviour appear "randomly". So we signal
    our parent (the uvicorn master) first, which cleanly reaps every worker
    and lets systemd's ``Restart=always`` bring the whole tree back on the
    new code. If we're somehow not a worker (dev mode, single-process), the
    ``os._exit`` fallback still gets us a fresh process.
    """
    from ... import application as host

    def _exit_soon() -> None:
        time.sleep(0.6)
        try:
            ppid = os.getppid()
            # ppid == 1 means our parent is already init/systemd (we ARE
            # the top process), so killing it would take out unrelated
            # services. Fall through to _exit in that case.
            if ppid > 1:
                os.kill(ppid, signal.SIGTERM)
        except OSError:
            pass
        os._exit(0)

    threading.Thread(target=_exit_soon, daemon=True).start()
    return host.templates.TemplateResponse(
        "partials/admin_update_result.html",
        {"request": request, "steps": [], "ok": True, "restarted": True},
    )


@router.get("/admin/backup", dependencies=[Depends(require_auth)])
def admin_backup():
    """Full read-only JSON dump of the luigi_todo tables the GUI touches.

    Streams as a file attachment named ``luigi-backup-YYYYMMDD-HHMMSS.json``.
    Respects the "no whole-table rewrites, no DDL" contract — this is read
    traffic only and never writes back.
    """
    from ... import application as host

    _require_tasks()
    host._require_v2()
    payload = host.db.export_backup()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"luigi-backup-{stamp}.json"
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get(
    "/admin/restore",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def admin_restore_form(request: Request):
    from ... import application as host

    _require_tasks(request)
    return host.templates.TemplateResponse(
        "partials/admin_restore.html",
        {"request": request, "plan": None, "token": "", "error": "", "result": None},
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/admin/restore/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
async def admin_restore_preview(
    request: Request,
    backup: UploadFile = File(...),
):
    from ... import application as host

    _require_tasks(request)
    raw = await backup.read(host.task_backup.MAX_UPLOAD_BYTES + 1)
    try:
        token, plan = host.task_backup.prepare(raw)
        error = ""
    except (host.task_backup.RestoreError, RuntimeError) as exc:
        token, plan, error = "", None, str(exc)
    return host.templates.TemplateResponse(
        "partials/admin_restore.html",
        {"request": request, "plan": plan, "token": token,
         "error": error, "result": None},
        headers={"Cache-Control": "no-store"},
        status_code=200,
    )


@router.post(
    "/admin/restore/commit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def admin_restore_commit(request: Request, token: str = Form(...)):
    from ... import application as host

    _require_tasks(request)
    try:
        result = host.task_backup.commit(token)
        error = ""
    except (host.task_backup.RestoreError, RuntimeError) as exc:
        result, error = None, str(exc)
    return host.templates.TemplateResponse(
        "partials/admin_restore.html",
        {"request": request, "plan": None, "token": "",
         "error": error, "result": result},
        headers={"Cache-Control": "no-store"},
        status_code=200,
    )
