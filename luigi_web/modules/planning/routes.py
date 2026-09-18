"""Planning HTTP controllers."""
from __future__ import annotations
from fastapi import APIRouter, FastAPI
import calendar as calendar_mod
from datetime import date, timedelta
from typing import Any
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from ...auth import require_auth

router = APIRouter()


def startup(app: FastAPI | None = None) -> None:
    from ... import application as host

    host.review.init_db()


def _parse_iso_date(s: Any) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _status_slug(s: str | None) -> str:
    return (s or "not-started").lower().replace(" ", "-")


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
    from ... import application as host

    if not rows:
        return None

    from collections import defaultdict

    scheduled: list[dict[str, Any]] = []
    unscheduled: list[dict[str, Any]] = []
    today = host.clock.local_today()

    for r in rows:
        end = host._parse_iso_date(r.get("due_date"))
        if not end:
            unscheduled.append(r)
            continue
        start = host._parse_iso_date(r.get("start_time")) or host._parse_iso_date(r.get("task_creation"))
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
    total_width = max(host._GANTT_MIN_WIDTH, span_days * px_per_day)
    if span_days * px_per_day < host._GANTT_MIN_WIDTH:
        px_per_day = host._GANTT_MIN_WIDTH / span_days

    def x_for(d: date) -> float:
        return (d - chart_start).days * px_per_day

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in scheduled:
        groups[r.get("project") or "(none)"].append(r)

    swimlanes: list[dict[str, Any]] = []
    y = host._GANTT_HEADER_H
    for cat_name in sorted(groups.keys()):
        tasks_in = groups[cat_name]
        cat_y = y
        y += host._GANTT_CAT_H
        lane_tasks = []
        for t in tasks_in:
            x1 = x_for(t["_start"])
            x2 = x_for(t["_end"])
            lane_tasks.append({
                "task": t["task"],
                "status": t["status"] or "Not Started",
                "status_class": host._status_slug(t["status"]),
                "priority": t.get("priority") or 0,
                "uuid": t["uuid"],
                "source": t.get("source", "task"),
                "catagory": t.get("catagory") or "",
                "project": t.get("project") or "",
                "start_iso": t["_start"].isoformat(),
                "end_iso": t["_end"].isoformat(),
                "bar_x": x1,
                "bar_y": y + (host._GANTT_ROW_H - host._GANTT_BAR_H) / 2,
                "bar_w": max(2.0, x2 - x1),
                "row_y": y,
            })
            y += host._GANTT_ROW_H
        swimlanes.append({
            "catagory": cat_name,
            "count": len(tasks_in),
            "cat_y": cat_y,
            "y_start": cat_y,
            "y_end": y,
            "tasks": lane_tasks,
        })

    total_height = max(host._GANTT_HEADER_H + 60, y + 8)

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
        "header_h": host._GANTT_HEADER_H,
        "row_h": host._GANTT_ROW_H,
        "cat_h": host._GANTT_CAT_H,
        "bar_h": host._GANTT_BAR_H,
        "swimlanes": swimlanes,
        "months": months,
        "today_x": today_x,
        "chart_start_iso": chart_start.isoformat(),
        "chart_end_iso": chart_end.isoformat(),
        "unscheduled": unscheduled,
        "scheduled_count": len(scheduled),
    }


@router.get("/projects", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def projects_page(request: Request):
    """Gantt-style view of open items grouped by ``catagory``.

    Category selection comes from the query string (repeated ``catagory``
    params). The page renders an empty state until at least one is picked,
    so first-time load stays snappy on large DBs.
    """
    from ... import application as host

    host._require_v2()
    selected = [p for p in request.query_params.getlist("project") if p]
    # Preserve old bookmarks from the category-grouped version.
    if not selected:
        selected = [p for p in request.query_params.getlist("catagory") if p]
    include_recurring = request.query_params.get("include_recurring", "1") == "1"

    all_projects = host.db.list_projects_with_open_tasks(
        include_recurring=include_recurring
    )
    # The old empty-by-default screen looked broken until chips were selected.
    if not selected:
        selected = [row["project"] for row in all_projects]
    rows = host.db.list_project_rows(selected, include_recurring=include_recurring)
    chart = host._build_gantt(rows)

    return host.templates.TemplateResponse(
        "projects.html",
        {
            "request": request,
            "active_nav": "projects",
            "page_title": "Projects",
            "all_projects": all_projects,
            "selected_projects": set(selected),
            "project_grouping_enabled": host.db.project_grouping_enabled(),
            "include_recurring": include_recurring,
            "chart": chart,
            "today_iso": host.clock.local_today().isoformat(),
        },
    )


@router.get("/calendar", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def calendar_page(request: Request, month: str | None = None):
    from ... import application as host

    host._require_v2()
    host._reactivate_recurring()
    today = host.clock.local_today()
    try:
        current = date.fromisoformat(f"{month}-01") if month else today.replace(day=1)
    except ValueError:
        raise HTTPException(400, "month must be YYYY-MM")
    last_day = calendar_mod.monthrange(current.year, current.month)[1]
    month_end = current.replace(day=last_day)
    # Full Sunday..Saturday weeks around the selected month.
    grid_start = current - timedelta(days=(current.weekday() + 1) % 7)
    grid_end = month_end + timedelta(days=(5 - month_end.weekday()) % 7)
    rows = host.db.list_calendar_rows(grid_start, grid_end)
    for row in rows:
        row["_calendar_layer"] = "planned"
    existing = {
        (str(row.get("uuid") or ""), str(row.get("due_date") or "")[:10])
        for row in rows
    }
    for rule in host.db.list_recurring():
        for occurrence in host.recurrence.calendar_occurrence_dates(rule, grid_start, grid_end):
            key = (str(rule.get("uuid") or ""), occurrence.isoformat())
            if key in existing:
                continue
            projected = dict(rule)
            projected.update({
                "due_date": occurrence.isoformat(),
                "completed": 0,
                "status": "Not Started",
                "source": "recurring",
                "_projected": True,
                "_calendar_layer": "projected",
            })
            rows.append(projected)
            existing.add(key)
    history_status, completion_rows = host.db.list_task_completion_events(
        grid_start, grid_end
    )
    rows.extend(completion_rows)
    activity_status, activity_rows = host.db.list_calendar_activity_events(
        grid_start, grid_end
    )
    rows.extend(activity_rows)
    rows.sort(key=lambda row: (
        str(row.get("due_date") or ""),
        -int(row.get("priority") or 0),
        str(row.get("task") or "").casefold(),
    ))
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(str(row.get("due_date") or "")[:10], []).append(row)
    weeks: list[list[dict[str, Any]]] = []
    cursor = grid_start
    while cursor <= grid_end:
        week: list[dict[str, Any]] = []
        for _ in range(7):
            week.append({
                "date": cursor,
                "iso": cursor.isoformat(),
                "in_month": cursor.month == current.month,
                "tasks": by_day.get(cursor.isoformat(), []),
            })
            cursor += timedelta(days=1)
        weeks.append(week)
    prev_month = (current - timedelta(days=1)).replace(day=1)
    next_month = (month_end + timedelta(days=1)).replace(day=1)
    return host.templates.TemplateResponse(
        "calendar.html",
        {
            "request": request,
            "active_nav": "calendar",
            "page_title": "Calendar",
            "month_label": current.strftime("%B %Y"),
            "month_value": current.strftime("%Y-%m"),
            "prev_month": prev_month.strftime("%Y-%m"),
            "next_month": next_month.strftime("%Y-%m"),
            "weeks": weeks,
            "today_iso": today.isoformat(),
            "completion_history_available": (
                history_status.available or bool(completion_rows)
            ),
            "completion_history_complete": history_status.available,
            "completion_history_reason": history_status.reason,
            "activity_history_complete": activity_status.available,
            "activity_history_reason": activity_status.reason,
            "completion_day_policy": host.task_events.server_time_policy(),
        },
    )


@router.get("/activity", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def activity_page(
    request: Request,
    days: int = 30,
    kind: str = "",
    q: str = "",
):
    from ... import application as host

    host._require_v2()
    allowed_kinds = {
        "", host.task_events.COMPLETED, host.task_events.COMPLETION_REVERSED,
        "created", "discipline",
    }
    if kind not in allowed_kinds:
        raise HTTPException(400, "invalid activity type")
    try:
        days = min(max(int(days), 1), 365)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "days must be an integer") from exc
    status, rows = host.db.list_activity_timeline(
        days=days, event_type=kind, query=q, limit=300
    )
    return host.templates.TemplateResponse("activity.html", {
        "request": request,
        "active_nav": "calendar",
        "page_title": "Calendar",
        "rows": rows,
        "days": days,
        "kind": kind,
        "query": q,
        "history_complete": status.available,
        "history_reason": status.reason,
    })


@router.get("/review", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def review_page(request: Request, scope: str = "daily"):
    from ... import application as host

    host._require_v2()
    try:
        state = host.review.build(scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return host.templates.TemplateResponse(
        "review.html",
        {
            "request": request,
            "active_nav": "review",
            "page_title": "Review",
            "review": state,
            "scope": state["scope"],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/review/{scope}", dependencies=[Depends(require_auth)])
async def review_save(scope: str, request: Request):
    from ... import application as host

    host._require_v2()
    form = await request.form()
    try:
        host.review.save_session(
            scope,
            completed_steps=form.getlist("completed_steps"),
            notes=str(form.get("notes") or ""),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(status_code=204, headers={
        "Cache-Control": "no-store",
        "HX-Trigger": host._hx_trigger(
            flashSuccess={"message": f"{scope.title()} review saved"}
        ),
        "HX-Refresh": "true",
    })


@router.get("/home", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def home_page(request: Request):
    from ... import application as host

    host._require_v2()
    host._reactivate_recurring()
    today = host.clock.local_today()
    monday = today - timedelta(days=today.weekday())
    open_tasks = host.db.list_open_tasks(limit=25)
    disciplines_pending = host.db.list_disciplines_pending_today()
    disc_week = host.db.weekly_discipline_counts(today)
    task_week = host.db.weekly_task_completion_counts(today)
    overdue_tasks = host.db.list_overdue_tasks(limit=10)
    upcoming_tasks = host.db.list_upcoming_tasks(days=7, limit=10)
    recent_completions = host.db.list_recent_completions(limit=8)
    discipline_streaks = host.db.list_discipline_streaks(limit=8)
    follow_ups = host.db.list_follow_ups_preview(limit=8)
    recent_activity = host.db.list_recent_activity(limit=15, days=14)
    weekly_review = host.db.weekly_review()
    disciplines_at_risk = host.db.list_disciplines_at_risk()
    # Game'N'Watch: "currently playing/watching" widgets. Best-effort — never
    # let a Sheets hiccup break the home page.
    gnw_playing: list[dict[str, Any]] = []
    gnw_watching: list[dict[str, Any]] = []
    gnw_enabled = host._module_enabled("media", request) and host.gnw.is_enabled()
    if gnw_enabled:
        try:
            gnw_playing = [i for i in host.gnw.list_items("games") if i["status"] == "playing"][:8]
            gnw_watching = [i for i in host.gnw.list_items("shows") if i["status"] == "watching"][:8]
        except Exception:  # noqa: BLE001
            gnw_playing, gnw_watching = [], []
    assistant_enabled = host._module_enabled("assistant", request)
    provider = host._LLM_PROVIDER if assistant_enabled else None
    return host.templates.TemplateResponse(
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
            "weekly_review": weekly_review,
            "disciplines_at_risk": disciplines_at_risk,
            "gnw_enabled": gnw_enabled,
            "gnw_playing": gnw_playing,
            "gnw_watching": gnw_watching,
            "week_of": monday.isoformat(),
            "today_iso": today.isoformat(),
            "chat_enabled": assistant_enabled and not isinstance(provider, host.llm_mod.DisabledProvider),
            "chat_provider": getattr(provider, "name", "disabled"),
            "chat_model": getattr(provider, "model", ""),
            "chat_disabled_reason": getattr(provider, "reason", "Assistant module is disabled"),
        },
    )
