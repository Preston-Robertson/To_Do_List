"""Authenticated live Home data and date-only mutations."""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse

from ...auth import require_auth
from ..tasks import occurrences, operations
from . import home_service

router = APIRouter(dependencies=[Depends(require_auth)])
_NO_STORE = {"Cache-Control": "no-store"}
_UNAVAILABLE = "Home task storage is unavailable. Please try again."
_SCHEDULE_FIELDS = (
    "recurring", "recurring_interval", "recurring_days",
    "recurring_month_ordinal", "recurring_month_weekday",
)


def _error(status: int, message: str) -> HTTPException:
    return HTTPException(status, message, headers=_NO_STORE)


def _require_schema() -> None:
    from ... import application as host

    try:
        host._require_v2()
    except Exception:
        raise _error(503, _UNAVAILABLE) from None


def _validate_reference(source: str, row_uuid: str) -> None:
    if source not in operations.SOURCES:
        raise _error(422, "Invalid task source.")
    if not row_uuid.strip() or len(row_uuid) > 200 or "\x00" in row_uuid:
        raise _error(422, "Invalid task reference.")


def _parse_date(value: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise _error(422, "Date must be YYYY-MM-DD.") from None
    if parsed.isoformat() != value:
        raise _error(422, "Date must be YYYY-MM-DD.")
    return value


def _read_task(source: str, row_uuid: str) -> dict[str, Any] | None:
    from ... import application as host

    try:
        return host.db.get_task(row_uuid) if source == "task" else host.db.get_recurring(row_uuid)
    except Exception:
        raise _error(503, _UNAVAILABLE) from None


def _current_task(source: str, row_uuid: str) -> dict[str, Any]:
    row = _read_task(source, row_uuid)
    if row is None or row.get("archived"):
        raise _error(404, "Task not found.")
    return dict(row)


def _require_mutable(source: str, row: dict[str, Any]) -> None:
    if source == "recurring" and row.get("_recurrence_generated"):
        raise _error(409, "Completed occurrence history cannot be changed.")


def get_home_state(request: Request) -> dict[str, Any]:
    from ... import application as host

    _require_schema()
    try:
        host._reactivate_recurring()
        state = home_service.load_home_state(today=host.clock.local_today())
        state["shortcuts"] = [
            {"label": label, "href": href, "icon": icon}
            for module_id, label, href, icon in (
                ("media", "Games", "/games", "gamepad-2"),
                ("cards", "Trading cards", "/cards", "layers"),
                ("characters", "Characters", "/characters", "shield"),
            )
            if host._module_enabled(module_id, request)
        ]
        return state
    except Exception:
        raise _error(503, _UNAVAILABLE) from None


@router.get("/home/data")
def home_data(request: Request) -> JSONResponse:
    return JSONResponse(get_home_state(request), headers=_NO_STORE)


@router.post("/home/today")
def home_today(
    source: str = Form(...), uuid: str = Form(...),
    selected: bool = Form(...), day: str = Form(...),
) -> JSONResponse:
    from ... import application as host

    _validate_reference(source, uuid)
    _parse_date(day)
    today = host.clock.local_today()
    if day != today.isoformat():
        raise _error(409, "The day has changed. Reload Home and try again.")
    _require_schema()
    _require_mutable(source, _current_task(source, uuid))
    try:
        stored = operations.set_today_selection(uuid, source, selected, day=today)
        verified = (source, uuid) in operations.list_today_selections(today)
    except Exception:
        raise _error(503, home_service.SELECTION_ERROR) from None
    if stored is not selected or verified != selected:
        raise _error(503, home_service.SELECTION_ERROR)
    return JSONResponse({"selected": selected, "day": day}, headers=_NO_STORE)


@router.post("/home/reschedule")
def home_reschedule(
    source: str = Form(...), uuid: str = Form(...), due_date: str = Form(""),
) -> JSONResponse:
    from ... import application as host

    _validate_reference(source, uuid)
    due = _parse_date(due_date, allow_empty=True)
    _require_schema()
    before = _current_task(source, uuid)
    _require_mutable(source, before)
    changed = home_service.due_iso(before.get("due_date")) != due or (
        not due and bool(before.get("due_date"))
    )
    if changed:
        if not int(before.get("recurring") or 0) and any(
            before.get(field) not in (None, "") for field in _SCHEDULE_FIELDS[1:]
        ):
            raise _error(409, "Task recurrence needs review before rescheduling.")
        payload = {field: before.get(field) for field in _SCHEDULE_FIELDS}
        payload["due_date"] = due or None
        try:
            if source == "task":
                host.db.update_task(uuid, payload)
            else:
                host.db.update_recurring(uuid, payload)
        except ValueError as exc:
            if str(exc) == occurrences.HISTORY_MESSAGE:
                raise _error(409, "Completed occurrence history cannot be changed.") from None
            raise _error(422, "Task could not be rescheduled. Reload Home and try again.") from None
        except Exception:
            raise _error(503, _UNAVAILABLE) from None
    after = _read_task(source, uuid)
    if (
        after is None or after.get("archived")
        or home_service.due_iso(after.get("due_date")) != due
        or (not due and after.get("due_date") not in (None, ""))
    ):
        raise _error(409, "The new due date could not be verified. Reload Home and try again.")
    if any(after.get(field) != value for field, value in before.items() if field != "due_date"):
        raise _error(409, "Task changed while rescheduling. Reload Home and try again.")
    headers = dict(_NO_STORE)
    if changed:
        try:
            label = "Rescheduled task"
            op_id = host._stash_undo("tasks" if source == "task" else "recurring_tasks", before, label)
            headers["HX-Trigger"] = host._hx_trigger(showUndo={
                "op_id": op_id, "label": label, "ttl_ms": host._UNDO_TTL_SECONDS * 1000,
            })
        except Exception:
            raise _error(503, "Due date saved, but Undo is unavailable. Reload Home.") from None
    return JSONResponse({"due": due}, headers=headers)