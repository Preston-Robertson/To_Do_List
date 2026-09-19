"""Live date-based history over LuigiBot's existing Discipline records."""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, timedelta
import re
import secrets
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ... import clock
from ...auth import require_auth
from ..tasks import repository as db
from .progress import weekly_progress

router = APIRouter(dependencies=[Depends(require_auth)])
_NO_STORE = {"Cache-Control": "no-store"}
_UNDO_TTL = 12
_UNDO_LIMIT = 256
_undo: OrderedDict[str, dict[str, Any]] = OrderedDict()
_undo_lock = threading.Lock()


def _error(status: int, detail: str) -> HTTPException:
    return HTTPException(status, detail, headers=_NO_STORE)


def _year(value: Any) -> int:
    today = clock.local_today()
    try:
        selected = today.year if value is None else int(value)
    except (ValueError, TypeError):
        raise _error(422, "Invalid history year") from None
    if not 1900 <= selected <= today.year + 1:
        raise _error(422, "Invalid history year")
    return selected


def _groups(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        raw = str(row["completed_date"])[:10]
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            continue
        if day.isoformat() == raw:
            groups.setdefault(raw, []).append(row)
    return groups


def history_state(row_uuid: str, year: int) -> dict[str, Any]:
    discipline = db.get_discipline(row_uuid)
    if discipline is None:
        raise _error(404, "Discipline not found")
    today = clock.local_today()
    task = str(discipline["task"])
    groups = _groups(db.list_discipline_history(task, date(year, 1, 1), date(year, 12, 31)))
    week_start = today - timedelta(days=today.weekday())
    week_days = _groups(db.list_discipline_history(task, week_start, today))
    completions = []
    for day, rows in sorted(groups.items()):
        timestamps = []
        for row in rows:
            if row["logged_at"]:
                try:
                    timestamps.append(clock.parse_timestamp_local(str(row["logged_at"])))
                except ValueError:
                    pass
        completions.append({
            "date": day, "loggedAt": min(timestamps).isoformat() if timestamps else None,
            "version": db.discipline_history_version(rows),
        })
    target = int(discipline["frequency_per_week"])
    return {
        "uuid": row_uuid, "name": task, "category": discipline.get("catagory"),
        "active": bool(int(discipline.get("active") or 0)), "today": today.isoformat(),
        "year": year, "timezone": clock.timezone_name(), "weeklyTarget": target,
        "weekly": weekly_progress(set(week_days), target, today),
        "completions": completions, "emptyVersion": db.discipline_history_version([]),
    }


def _prune_undo() -> None:
    expired = [token for token, entry in _undo.items() if entry["expires"] <= time.monotonic()]
    for token in expired:
        _undo.pop(token, None)


def _state(row_uuid: str, year: int) -> dict[str, Any]:
    with _undo_lock:
        _prune_undo()
    try:
        return history_state(row_uuid, year)
    except HTTPException:
        raise
    except Exception:
        raise _error(503, "Discipline history is unavailable") from None


def _require_schema() -> None:
    from ... import application as host

    host._require_v2()


@router.get("/discipline/{row_uuid}/history", response_class=HTMLResponse)
def history_page(request: Request, row_uuid: str, year: str | None = None):
    from ... import application as host

    _require_schema()
    state = _state(row_uuid, _year(year))
    return host.templates.TemplateResponse("history.html", {
        "request": request, "history_seed": state,
        "active_nav": "discipline", "page_title": "Discipline history",
    }, headers=_NO_STORE)


@router.get("/discipline/{row_uuid}/history/data")
def history_data(row_uuid: str, year: str | None = None):
    _require_schema()
    return JSONResponse(_state(row_uuid, _year(year)), headers=_NO_STORE)


@router.post("/discipline/{row_uuid}/history")
async def history_change(request: Request, row_uuid: str):
    _require_schema()
    form = await request.form()
    year = _year(form.get("year"))
    raw_day = str(form.get("day") or "")
    try:
        day = date.fromisoformat(raw_day)
    except ValueError:
        raise _error(422, "Invalid completion date") from None
    if day.isoformat() != raw_day or day.year != year or day > clock.local_today():
        raise _error(422, "Invalid completion date")
    action = form.get("action")
    version = str(form.get("expected_version") or "")
    if action not in {"mark", "unmark"} or not re.fullmatch(r"[0-9a-f]{64}", version):
        raise _error(400, "Invalid history change")
    try:
        snapshot = db.change_discipline_history(row_uuid, day, version, action == "mark")
    except db.DisciplineHistoryConflict:
        raise _error(409, "History changed or this date cannot be edited. Reload before saving.") from None
    except Exception:
        raise _error(503, "History save could not be confirmed. Reload before another change.") from None
    token = None
    if snapshot["changed"]:
        token = secrets.token_urlsafe(24)
        with _undo_lock:
            _prune_undo()
            while len(_undo) >= _UNDO_LIMIT:
                _undo.popitem(last=False)
            _undo[token] = {
                "uuid": row_uuid, "snapshot": snapshot,
                "expires": time.monotonic() + _UNDO_TTL,
            }
    return JSONResponse({
        "ok": True, "state": _state(row_uuid, year),
        "undo_token": token, "undo_ttl_ms": _UNDO_TTL * 1000,
    }, headers=_NO_STORE)


@router.post("/discipline/{row_uuid}/history/undo")
async def history_undo(request: Request, row_uuid: str):
    _require_schema()
    form = await request.form()
    year = _year(form.get("year"))
    token = str(form.get("token") or "")
    with _undo_lock:
        _prune_undo()
        entry = _undo.get(token)
        if entry is None or entry["uuid"] != row_uuid:
            raise _error(409, "Undo expired or is unavailable")
        snapshot = entry["snapshot"]
        try:
            db.change_discipline_history(
                row_uuid, date.fromisoformat(snapshot["day"]), snapshot["after_version"],
                bool(snapshot["before"]), restore=snapshot,
            )
        except db.DisciplineHistoryConflict:
            raise _error(409, "History changed. Undo cannot overwrite newer records.") from None
        except Exception:
            raise _error(503, "Undo could not be confirmed. Reload before another change.") from None
        _undo.pop(token, None)
    return JSONResponse({"ok": True, "state": _state(row_uuid, year)}, headers=_NO_STORE)