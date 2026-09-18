"""Discipline HTTP controllers."""
from __future__ import annotations
from fastapi import APIRouter
from datetime import date, timedelta
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from ...auth import require_auth

router = APIRouter()


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
    from ... import application as host

    current = host.clock.local_today().year
    with host.db.get_engine().connect() as conn:
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


@router.get("/discipline", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def discipline_page(request: Request, year: int | None = None):
    from ... import application as host

    host._require_v2()
    if year is None:
        year = host.clock.local_today().year
    disciplines = host.db.list_disciplines(include_inactive=True)
    completions = host.db.list_completions_for_year(year)
    today_iso = host.clock.local_today().isoformat()
    today_tasks = host.db.list_completion_tasks_for_day(today_iso)
    # Index completions by a normalized task key too, so a completion logged
    # under a slightly different string (trailing space, different case) still
    # lights up its discipline's heatmap instead of silently going missing.
    def _norm(s: str | None) -> str:
        return (s or "").strip().lower()

    completions_by_norm: dict[str, set[str]] = {}
    for _task_name, _days in completions.items():
        completions_by_norm.setdefault(_norm(_task_name), set()).update(_days)
    today_by_norm = {_norm(task) for task in today_tasks}
    # Attach year-specific completion sets + computed streak (from all-time in-year data).
    for d in disciplines:
        days = completions.get(d["task"])
        if not days:
            days = completions_by_norm.get(_norm(d["task"]), set())
        d["_year_days"] = days
        d["_today_done"] = _norm(d["task"]) in today_by_norm
        # Streak is computed against the CURRENT date, so use full history when
        # viewing the current year and just the year's data otherwise.
        if year == host.clock.local_today().year:
            d["_streak"] = host.db.compute_streak(days)
        else:
            d["_streak"] = d.get("current_streak") or 0
    return host.templates.TemplateResponse(
        "discipline.html",
        {
            "request": request,
            "active_nav": "discipline",
            "page_title": "Discipline",
            "disciplines": disciplines,
            "year": year,
            "years": host._available_years(),
            "grid": host._year_grid(year),
            "today_iso": today_iso,
        },
    )


@router.get("/discipline/new", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def discipline_new_form(request: Request):
    from ... import application as host

    return host.templates.TemplateResponse(
        "partials/discipline_form.html",
        {"request": request, "d": {}, "is_new": True},
    )


@router.post("/discipline", dependencies=[Depends(require_auth)])
async def discipline_create(request: Request):
    from ... import application as host

    host._require_v2()
    form = dict(await request.form())
    try:
        host.db.create_discipline(form)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Full-page reload is fine here — the heatmap grid depends on the discipline list.
    return Response(
        status_code=204,
        headers={
            "HX-Trigger": host._hx_trigger(
                flashSuccess={"message": "Discipline created"},
                closeModal=None,
            ),
            "HX-Refresh": "true",
        },
    )


@router.get(
    "/discipline/{row_uuid}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def discipline_edit_form(request: Request, row_uuid: str):
    from ... import application as host

    host._require_v2()
    row = host.db.get_discipline(row_uuid)
    if not row:
        raise HTTPException(404)
    return host.templates.TemplateResponse(
        "partials/discipline_form.html",
        {"request": request, "d": row, "is_new": False},
    )


@router.post("/discipline/{row_uuid}", dependencies=[Depends(require_auth)])
async def discipline_update(request: Request, row_uuid: str):
    from ... import application as host

    host._require_v2()
    form = dict(await request.form())
    try:
        host.db.update_discipline(row_uuid, form)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(
        status_code=204,
        headers={
            "HX-Trigger": host._hx_trigger(
                flashSuccess={"message": "Discipline saved"},
                closeModal=None,
            ),
            "HX-Refresh": "true",
        },
    )


@router.post("/discipline/{row_uuid}/deactivate", dependencies=[Depends(require_auth)])
def discipline_deactivate(row_uuid: str):
    from ... import application as host

    host._require_v2()
    host.db.deactivate_discipline(row_uuid)
    return Response(status_code=204, headers={
        "HX-Trigger": host._hx_trigger(flashSuccess={"message": "Discipline deactivated"}),
        "HX-Refresh": "true",
    })


@router.post("/discipline/{row_uuid}/delete", dependencies=[Depends(require_auth)])
def discipline_delete(row_uuid: str):
    """Hard-delete a discipline (and its completions), with a 12s undo.

    The snapshot returned by ``db.delete_discipline`` bundles the
    discipline_list row + all its completions rows so ``restore_discipline_row``
    can put both back.
    """
    from ... import application as host

    host._require_v2()
    snapshot = host.db.delete_discipline(row_uuid)
    if snapshot is None:
        raise HTTPException(404, "discipline not found")
    if not host._module_enabled("tasks"):
        return Response(status_code=204, headers={
            "HX-Trigger": host._hx_trigger(
                flashSuccess={"message": "Discipline deleted"},
                reloadBoard=None,
            ),
        })
    task_name = (snapshot.get("discipline") or {}).get("task") or "discipline"
    op_id = host._stash_undo("discipline_list", snapshot, f"Deleted ‘{task_name}’")
    return Response(
        status_code=204,
        headers={
            "HX-Trigger": host._hx_trigger(
                showUndo={"op_id": op_id, "label": f"Deleted ‘{task_name}’",
                          "ttl_ms": host._UNDO_TTL_SECONDS * 1000},
                reloadBoard=None,
            ),
        },
    )


@router.post("/discipline/{row_uuid}/today", dependencies=[Depends(require_auth)])
async def discipline_today(row_uuid: str, request: Request):
    """Explicit, discoverable mark/unmark action for the current day.

    The server—not the browser—chooses today's date and resolves the current
    canonical task/category by UUID before touching the legacy text-keyed
    completion table.
    """
    from ... import application as host

    host._require_v2()
    discipline = host.db.get_discipline(row_uuid)
    if not discipline:
        raise HTTPException(404, "discipline not found")
    if not int(discipline.get("active") or 0):
        raise HTTPException(409, "inactive disciplines cannot be marked")
    form = dict(await request.form())
    action = str(form.get("action") or "mark").strip().lower()
    if action not in {"mark", "unmark"}:
        raise HTTPException(400, "action must be mark or unmark")
    task = str(discipline["task"])
    day = host.clock.local_today().isoformat()
    try:
        if action == "mark":
            ok = host.db.mark_completion(task, discipline.get("catagory"), day)
            message = f"{task} marked done for today"
        else:
            ok = host.db.unmark_completion(task, day)
            message = f"{task} cleared for today"
    except Exception as exc:  # noqa: BLE001
        return host._discipline_toggle_error(
            f"Couldn't {action} “{task}” for {day}: {type(exc).__name__}: {exc}"
        )
    if not ok:
        return host._discipline_toggle_error(
            f"“{task}” for {day} did not persist in the requested state."
        )
    marked = host.db.completion_exists(task, day)
    if marked != (action == "mark"):
        return host._discipline_toggle_error(
            f"“{task}” for {day} changed during verification. Refresh and try again."
        )
    return JSONResponse({
        "ok": True,
        "discipline_uuid": row_uuid,
        "task": task,
        "day": day,
        "marked": marked,
        "streak": host.db.computed_discipline_streak(task),
        "message": message,
    })


@router.post("/discipline/toggle", dependencies=[Depends(require_auth)])
async def discipline_toggle(request: Request):
    """Mark or unmark a single (task, day) — HTMX target is the cell itself.

    Every write is verified against the DB (see ``db.mark_completion`` /
    ``db.unmark_completion``, which re-read inside the same transaction) so a
    save that never landed is reported instead of silently swallowed. Any
    failure returns HTTP 422 with a plain-text reason and an ``HX-Trigger:
    flashError`` header — HTMX won't swap the cell (so it can't lie about being
    saved) and the frontend shows a toast with the reason.
    """
    from ... import application as host

    host._require_v2()
    form = dict(await request.form())
    task = form.get("task", "")
    catagory = form.get("catagory") or None
    day = form.get("day", "")
    action = form.get("action", "toggle")
    discipline_uuid = form.get("discipline_uuid", "")
    if discipline_uuid:
        discipline = host.db.get_discipline(discipline_uuid)
        if not discipline:
            raise HTTPException(404, "discipline not found")
        # Resolve the canonical current values server-side. Completion rows
        # are keyed by task text in the LuigiBot schema, so stale/rendered
        # text must not create a detached history row.
        task = discipline["task"]
        catagory = discipline.get("catagory")
    if not task or not day:
        raise HTTPException(400, "task and day required")

    try:
        if action == "mark":
            want_marked = True
            ok = host.db.mark_completion(task, catagory, day)
        elif action == "unmark":
            want_marked = False
            ok = host.db.unmark_completion(task, day)
        else:
            # Default: read current state and flip it.
            if host.db.completion_exists(task, day):
                want_marked = False
                ok = host.db.unmark_completion(task, day)
            else:
                want_marked = True
                ok = host.db.mark_completion(task, catagory, day)
    except Exception as exc:  # noqa: BLE001
        verb = "unmark" if action == "unmark" else "save"
        return host._discipline_toggle_error(
            f"Couldn't {verb} “{task}” for {day}: {type(exc).__name__}: {exc}"
        )

    if not ok:
        # The transaction committed but the row isn't in the state we asked for
        # — surface it rather than showing a cell that claims it saved.
        did = "record" if want_marked else "clear"
        return host._discipline_toggle_error(
            f"“{task}” for {day} didn't {did} — the database accepted the write "
            "but the change isn't there. Try again; if it persists, check the "
            "discipline_completions table."
        )

    return host.templates.TemplateResponse(
        "partials/discipline_cell.html",
        {
            "request": request,
            "discipline_uuid": discipline_uuid,
            "task": task,
            "catagory": catagory,
            "day": day,
            "marked": want_marked,
            "today_iso": host.clock.local_today().isoformat(),
        },
    )


def _discipline_toggle_error(message: str) -> Response:
    """A non-swapping error response for the discipline toggle. 422 keeps HTMX
    from applying the swap (so the cell/row stays truthful), and the
    ``flashError`` trigger drives the frontend error toast."""
    from ... import application as host

    return Response(
        content=message,
        status_code=422,
        media_type="text/plain; charset=utf-8",
        headers={"HX-Trigger": host._hx_trigger(flashError={"message": message})},
    )
