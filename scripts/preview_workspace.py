"""Loopback-only, disposable workspace preview with synthetic data."""
from __future__ import annotations

import argparse
import copy
import math
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import tempfile
from collections import Counter, OrderedDict
from contextlib import ExitStack, contextmanager
from datetime import date, timedelta
from http.cookies import SimpleCookie
from threading import RLock
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, Request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def _preview_discipline_toggle(request: Request):
    from luigi_web.modules.discipline.routes import discipline_toggle

    form = await request.form()
    if not form.get("discipline_uuid"):
        raise HTTPException(403, "Synthetic discipline reference required")
    return await discipline_toggle(request)


def configure_preview_security(
    app, session_token: str, enable_task_actions: bool = False, *, discipline_history_uuids=(),
) -> None:
    from fastapi import Request
    from fastapi.responses import JSONResponse, RedirectResponse
    from fastapi.routing import APIRoute
    from luigi_web import auth

    history_paths = {
        f"/discipline/{row_uuid}/history{suffix}"
        for row_uuid in discipline_history_uuids
        if re.fullmatch(r"preview-habit(?:-[A-Za-z0-9-]{1,48})?", row_uuid)
        for suffix in ("", "/undo")
    }
    if enable_task_actions:
        for index, route in enumerate(app.router.routes):
            if isinstance(route, APIRoute) and route.path == "/discipline/toggle" and "POST" in route.methods:
                app.router.routes[index] = APIRoute(
                    route.path, _preview_discipline_toggle, methods=route.methods,
                    dependencies=route.dependencies, name=route.name,
                    include_in_schema=route.include_in_schema,
                )
                break

    @app.middleware("http")
    async def preview_security(request: Request, call_next):
        if (
            not request.client
            or request.client.host not in {"127.0.0.1", "::1", "testclient"}
            or request.url.hostname not in {"127.0.0.1", "localhost", "testserver"}
        ):
            return JSONResponse({"detail": "Loopback only"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Same-origin requests only"}, status_code=403)
        path = request.url.path
        session_name = f"luigi_preview_session_{request.url.port or 80}"
        current_session = request.cookies.get(session_name, "")
        valid_session = secrets.compare_digest(current_session, session_token)
        bootstrap = request.method in {"GET", "HEAD"} or path == "/login" and request.method == "POST"
        if (path.startswith(("/admin", "/gnw", "/feedback", "/chat")) and path != "/chat/panel") or "/refresh" in path:
            return JSONResponse({"detail": "Integration and deployment actions are disabled in preview"}, status_code=403)
        task_action = enable_task_actions and request.method == "POST" and (re.fullmatch(
            r"/(?:home/(?:today|reschedule)|tasks(?:/quick|/preview-task-[A-Za-z0-9-]+(?:/(?:status|complete))?)?"
            r"|recurring(?:/preview-recurring-[A-Za-z0-9-]+(?:/(?:status|complete))?)?"
            r"|discipline/(?:preview-habit(?:-[A-Za-z0-9-]{1,48})?/(?:deactivate|resume|today)|toggle)"
            r"|undo/[A-Za-z0-9_-]+)", path,
        ) is not None or path in history_paths)
        if task_action and not valid_session:
            return JSONResponse({"detail": "Preview session required"}, status_code=401)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not task_action and path not in {"/login", "/logout", "/modules", "/home/layout"} and not path.startswith(("/cards", "/characters")):
            return JSONResponse({"detail": "This preview action is read-only"}, status_code=403)

        forwarded = SimpleCookie()
        for name, value in request.cookies.items():
            if name != auth.COOKIE_NAME:
                forwarded[name] = value
        if valid_session or bootstrap:
            forwarded[auth.COOKIE_NAME] = session_token
        request.scope["headers"] = [
            (name, value) for name, value in request.scope["headers"] if name.lower() != b"cookie"
        ] + [(b"cookie", forwarded.output(header="", sep=";").strip().encode("latin-1"))]

        if path in {"/", "/login"} and bootstrap:
            response = RedirectResponse("/home", status_code=303)
        elif path == "/logout" and request.method == "POST":
            response = RedirectResponse("/", status_code=303)
            response.delete_cookie(session_name)
        elif path == "/reminders/count":
            response = JSONResponse(0)
        else:
            response = await call_next(request)
        if bootstrap and not valid_session:
            response.set_cookie(session_name, session_token, httponly=True, samesite="strict")
        if bootstrap and not request.cookies.get(auth.CSRF_COOKIE_NAME):
            response.set_cookie(auth.CSRF_COOKIE_NAME, auth.csrf_token(), samesite="strict")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response


class _PreviewTasks:
    """Bounded synthetic records; no method can open the shared task engine."""

    def __init__(self, host, tasks, disciplines, today, recurring=()):
        self.host = host
        self.lock = RLock()
        self.records = {"task": {}, "recurring": {}}
        self.tasks = self.records["task"]
        self.recurring = self.records["recurring"]
        self.occurrence_links = {}
        for source, rows in (("task", tasks), ("recurring", recurring)):
            for task in rows:
                if not re.fullmatch(rf"preview-{source}-[A-Za-z0-9-]+", task["uuid"]):
                    raise ValueError("Only synthetic task IDs are allowed")
                row = dict.fromkeys(host.db._TASK_COLUMNS)
                row.update(task)
                row.update(source=source, project="Example project", logged_hours=0,
                           completed_time=f"{today - timedelta(days=1)}T12:00:00" if row["completed"] else None)
                self.records[source][row["uuid"]] = row
        self.disciplines = copy.deepcopy(disciplines)
        if any(not re.fullmatch(r"preview-habit(?:-[A-Za-z0-9-]{1,48})?", row["uuid"]) for row in disciplines):
            raise ValueError("Only synthetic discipline IDs are allowed")
        self.discipline_start = date(today.year - 1, 1, 1)
        self.completions = {row["task"]: {} for row in disciplines}
        for row in disciplines:
            self.seed_discipline_completions(row, today, (1, 2, 3))

    def list_tasks(self):
        with self.lock:
            return copy.deepcopy(list(self.tasks.values()))

    def get_task(self, row_uuid):
        return self._resolve("task", row_uuid)

    def list_recurring(self):
        with self.lock:
            return [self._resolve("recurring", row_uuid) for row_uuid in self.recurring]

    def get_recurring(self, row_uuid):
        return self._resolve("recurring", row_uuid)

    def _resolve(self, source, row_uuid):
        with self.lock:
            row = copy.deepcopy(self.records.get(source, {}).get(row_uuid))
            if row is None or source != "recurring":
                return row
            successor = self.occurrence_links.get(row_uuid)
            parent = next((link for link in self.occurrence_links.values() if link["child_uuid"] == row_uuid), None)
            if parent or successor:
                row.update(
                    _recurrence_parent_uuid=parent["parent_uuid"] if parent else None,
                    _recurrence_series_uuid=(parent or successor)["series_uuid"],
                    _recurrence_next_uuid=successor["child_uuid"] if successor else None,
                    _recurrence_generated=bool(successor),
                )
            return row

    def generate_due(self, today=None):
        from luigi_web.modules.tasks import occurrences

        if occurrences.scheduler_owner() != "web":
            return 0
        today = today or self.host.clock.local_today()
        generated = 0
        with self.lock:
            for row_uuid, source in tuple(self.recurring.items()):
                if len(self.recurring) >= 100:
                    break
                if row_uuid in self.occurrence_links or source.get("archived"):
                    continue
                due_date = self.host.db.reactivation_date(source)
                if not due_date or due_date > today.isoformat():
                    continue
                created_at = self.host.db.now_iso()
                payload = occurrences.occurrence_payload(source, due_date, created_at)
                child_uuid = f"preview-recurring-{payload['uuid']}"
                if child_uuid in self.recurring:
                    raise ValueError("Synthetic occurrence already exists without a link")
                child = dict.fromkeys(self.host.db._TASK_COLUMNS)
                child.update(payload, uuid=child_uuid, source="recurring")
                series_uuid = self._resolve("recurring", row_uuid).get("_recurrence_series_uuid", row_uuid)
                self.recurring[child_uuid] = child
                self.occurrence_links[row_uuid] = {
                    "parent_uuid": row_uuid, "child_uuid": child_uuid, "series_uuid": series_uuid,
                    "completed_at": source["completed_time"], "due_date": due_date, "generated_at": created_at,
                }
                generated += 1
        return generated

    def _assert_mutable(self, source, row_uuid):
        from luigi_web.modules.tasks import occurrences

        if source == "recurring" and row_uuid in self.occurrence_links:
            raise ValueError(occurrences.HISTORY_MESSAGE)

    def _require_task(self, row_uuid, source="task"):
        from fastapi import HTTPException

        row = self._resolve(source, row_uuid)
        if row is None:
            raise HTTPException(404, "Synthetic task not found")
        return row

    def _unblocked(self, row_uuid, source):
        self.host.operations.assert_unblocked(row_uuid, source, self._resolve)

    def _schedule(self, row, data):
        enabled = self.host.db._to_int_bool(data.get("recurring", row["recurring"]))
        if enabled and row["source"] != "recurring":
            raise ValueError("One-off preview tasks cannot be converted to recurring rows")
        fields = ("recurring_interval", "recurring_days", "recurring_month_ordinal", "recurring_month_weekday")
        row["recurring"] = enabled
        if not enabled:
            row.update(dict.fromkeys(fields))
            return
        schedule = {field: data.get(field, row[field]) for field in fields}
        schedule_type = str(data.get("recurring_schedule_type") or self.host.db.recurrence_schedule_type(schedule)).strip().lower()
        active_fields = {
            "interval": {"recurring_interval"}, "weekdays": {"recurring_days"},
            "monthly": {"recurring_month_ordinal", "recurring_month_weekday"},
        }.get(schedule_type, set())
        schedule = {field: value if field in active_fields else None for field, value in schedule.items()}
        self.host._validate_recurring_form({**schedule, "recurring": "1", "recurring_schedule_type": schedule_type})
        row.update(dict.fromkeys(fields))
        if schedule_type == "monthly":
            row["recurring_month_ordinal"], row["recurring_month_weekday"] = self.host.recurrence.parse_monthly_schedule(
                schedule["recurring_month_ordinal"], schedule["recurring_month_weekday"],
            )
        elif schedule_type == "weekdays":
            row["recurring_days"] = self.host.db.parse_recurring_days(schedule["recurring_days"])
        else:
            interval = schedule["recurring_interval"]
            row["recurring_interval"] = int(interval) if interval not in (None, "") else None

    def _updated(self, before, data, *, update_schedule=True):
        from fastapi import HTTPException

        row = {field: copy.deepcopy(value) for field, value in before.items() if not field.startswith("_recurrence_")}
        try:
            if update_schedule:
                self._schedule(row, data)
            for field in (*self.host.db._TASK_EDITABLE, "description"):
                if field not in data or field.startswith("recurring"):
                    continue
                value = data[field]
                if field == "priority":
                    value = int(value or 0)
                    if not 0 <= value <= 10:
                        raise ValueError("Priority must be between 0 and 10")
                elif field == "estimated_time":
                    value = float(value) if value not in (None, "") else None
                    if value is not None and (not math.isfinite(value) or value < 0):
                        raise ValueError("Estimated hours must be a nonnegative number")
                else:
                    value = str(value).strip() if value is not None else ""
                    if len(value) > (240 if field == "task" else 4000):
                        raise ValueError("Preview field is too long")
                    if field == "due_date" and value and date.fromisoformat(value).isoformat() != value:
                        raise ValueError("Date must be YYYY-MM-DD")
                    value = value or None
                row[field] = value
            if not row["task"]:
                raise ValueError("Task name is required")
            if row["status"] not in self.host.db.STATUS_VALUES:
                raise ValueError("Invalid task status")
            if "status" in data:
                if row["status"] in {"In Progress", "Completed"} and not before["completed"]:
                    self._unblocked(row["uuid"], row["source"])
                row["completed"] = int(row["status"] == "Completed")
                if row["completed"] != before["completed"]:
                    row["completed_time"] = self.host.db.now_iso() if row["completed"] else None
                if row["status"] == "In Progress" and not row["start_time"]:
                    row["start_time"] = self.host.db.now_iso()
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from None
        return row

    def create_task(self, data):
        return self._create("task", data)

    def create_recurring(self, data):
        return self._create("recurring", data)

    def _create(self, source, data):
        from fastapi import HTTPException

        with self.lock:
            records = self.records[source]
            if len(records) >= 100:
                raise HTTPException(422, "Disposable preview task limit reached")
            row_uuid = f"preview-{source}-{secrets.token_hex(16)}"
            row = dict.fromkeys(self.host.db._TASK_COLUMNS)
            row.update(uuid=row_uuid, task="", priority=0, status="Not Started", completed=0,
                       recurring=int(source == "recurring"), archived=0, logged_hours=0, source=source, description="",
                       task_creation=self.host.clock.local_today().isoformat())
            records[row_uuid] = self._updated(row, data)
            return row_uuid

    def update_task(self, row_uuid, data):
        self._update("task", row_uuid, data)

    def update_recurring(self, row_uuid, data):
        self._update("recurring", row_uuid, data)

    def _update(self, source, row_uuid, data):
        with self.lock:
            before = self._require_task(row_uuid, source)
            self._assert_mutable(source, row_uuid)
            row = self._updated(before, data)
            if row["task"] != before["task"]:
                self.host.operations.update_task_label(row_uuid, source, row["task"])
            self.records[source][row_uuid] = row

    def set_task_status(self, row_uuid, status, *, effective_date=None):
        return self._set_status("task", row_uuid, status, effective_date=effective_date)

    def set_recurring_status(self, row_uuid, status, *, effective_date=None):
        return self._set_status("recurring", row_uuid, status, effective_date=effective_date)

    def _set_status(self, source, row_uuid, status, *, effective_date=None):
        with self.lock:
            before = self._require_task(row_uuid, source)
            if source == "recurring" and row_uuid in self.occurrence_links:
                if status == "Completed":
                    return SimpleNamespace(completed=1, generated_task_uuids=(),
                                           event_uuid=None, event_type=None, history_available=False)
                self._assert_mutable(source, row_uuid)
            if status not in self.host.db.STATUS_VALUES:
                raise ValueError("Invalid task status")
            if effective_date:
                date.fromisoformat(effective_date)
            row = self._updated(before, {"status": status}, update_schedule=False)
            self.records[source][row_uuid] = row
            return SimpleNamespace(completed=row["completed"], generated_task_uuids=(),
                                   event_uuid=None, event_type=None, history_available=False)

    def toggle_task_completed(self, row_uuid, *, effective_date=None):
        return self._toggle_completed("task", row_uuid, effective_date=effective_date)

    def toggle_recurring_completed(self, row_uuid, *, effective_date=None):
        return self._toggle_completed("recurring", row_uuid, effective_date=effective_date)

    def _toggle_completed(self, source, row_uuid, *, effective_date=None):
        with self.lock:
            before = self._require_task(row_uuid, source)
            status = "Not Started" if before["completed"] else "Completed"
            return self._set_status(source, row_uuid, status, effective_date=effective_date)

    def restore_task_row(self, table, snapshot, generated_task_uuids=(),
                         task_event_uuid=None, task_event_type=None):
        with self.lock:
            row_uuid = snapshot.get("uuid")
            source = {"tasks": "task", "recurring_tasks": "recurring"}.get(table)
            if (source is None or row_uuid not in self.records[source] or snapshot.get("source") != source
                    or generated_task_uuids or task_event_uuid or task_event_type):
                raise ValueError("Only existing synthetic task snapshots can be restored")
            self._assert_mutable(source, row_uuid)
            self.records[source][row_uuid] = {
                field: copy.deepcopy(value) for field, value in snapshot.items() if not field.startswith("_recurrence_")
            }

    def seed_discipline_completions(self, discipline, today, offsets):
        with self.lock:
            self.completions[discipline["task"]] = {
                day: [{"task": discipline["task"], "catagory": discipline["catagory"],
                       "completed_date": day, "logged_at": f"{day}T12:00:00"}]
                for offset in offsets
                if self._valid_completion(discipline["task"], day := (today - timedelta(days=offset)).isoformat())
            }

    def computed_discipline_streak(self, task):
        with self.lock:
            days = self.completions.get(task, set())
            current = self.host.clock.local_today()
            if current.isoformat() not in days:
                current -= timedelta(days=1)
            streak = 0
            while current.isoformat() in days:
                streak += 1
                current -= timedelta(days=1)
            return streak

    def list_disciplines(self, include_inactive=True):
        with self.lock:
            return [{**copy.deepcopy(row), "current_streak": self.computed_discipline_streak(row["task"])}
                    for row in self.disciplines if include_inactive or row.get("active")]

    def get_discipline(self, row_uuid):
        return next((row for row in self.list_disciplines() if row["uuid"] == row_uuid), None)

    def set_discipline_active(self, row_uuid, active):
        with self.lock:
            for row in self.disciplines:
                if row["uuid"] == row_uuid:
                    row["active"] = int(bool(active))
                    return self.get_discipline(row_uuid)["active"] == int(bool(active))
            return False

    def list_discipline_completions_between(self, start, end):
        with self.lock:
            return {task: {day for day in days if start.isoformat() <= day <= end.isoformat()}
                    for task, days in self.completions.items()}

    def list_discipline_history(self, task, start, end):
        if end < start or (end - start).days > 370:
            raise ValueError("History requires a bounded date range")
        with self.lock:
            rows = [row for title, days in self.completions.items() if title.strip().lower() == task.strip().lower()
                    for day, records in days.items() if start.isoformat() <= day <= end.isoformat()
                    for row in records]
            return copy.deepcopy(sorted(rows, key=lambda row: (str(row["completed_date"]), str(row["logged_at"] or "")), reverse=True))

    def change_discipline_history(self, row_uuid, day, expected_version, marked, *, restore=None):
        with self.lock:
            discipline = self.get_discipline(row_uuid)
            raw_day = day.isoformat()
            conflict = self.host.db.DisciplineHistoryConflict
            if discipline is None or not self._valid_completion(discipline["task"], raw_day):
                raise conflict("This date cannot be changed")
            if not discipline["active"] and day >= self.host.clock.local_today():
                raise conflict("Paused disciplines only allow past-date corrections")
            task = discipline["task"]
            identities = [row for row in self.disciplines if row["task"].strip().lower() == task.strip().lower()]
            if len(identities) != 1 or restore is not None and (restore["task"] != task or restore["day"] != raw_day):
                raise conflict("Discipline identity changed or is ambiguous")
            before = self.list_discipline_history(task, day, day)
            version = self.host.db.discipline_history_version
            if version(before) != expected_version:
                raise conflict("History changed; reload before saving")
            if restore is not None:
                desired = copy.deepcopy(restore["before"])
                if any(row["task"] != task or str(row["completed_date"])[:10] != raw_day for row in desired):
                    raise conflict("Only this synthetic completion date can be restored")
            elif marked:
                desired = before or [{"task": task, "catagory": discipline["catagory"],
                                      "completed_date": raw_day, "logged_at": self.host.db.now_iso()}]
            else:
                desired = []
            changed = version(desired) != expected_version
            if changed:
                if desired:
                    self.completions[task][raw_day] = copy.deepcopy(desired)
                else:
                    self.completions[task].pop(raw_day, None)
            after = self.list_discipline_history(task, day, day)
            if version(after) != version(desired):
                raise RuntimeError("History save could not be verified")
            return {"task": task, "day": raw_day, "before": before,
                    "after_version": version(after), "changed": changed}

    def completion_exists(self, task, day):
        with self.lock:
            return day in self.completions.get(task, set())

    def mark_completion(self, task, catagory, day):
        with self.lock:
            if not self._valid_completion(task, day):
                return False
            discipline = next(row for row in self.disciplines if row["task"] == task)
            self.completions[task].setdefault(day, [{
                "task": task, "catagory": discipline["catagory"],
                "completed_date": day, "logged_at": self.host.db.now_iso(),
            }])
            return self.completion_exists(task, day)

    def unmark_completion(self, task, day):
        with self.lock:
            if not self._valid_completion(task, day):
                return False
            self.completions[task].pop(day, None)
            return not self.completion_exists(task, day)

    def _valid_completion(self, task, day):
        try:
            completion_day = date.fromisoformat(day)
        except (TypeError, ValueError):
            return False
        return (task in self.completions and completion_day.isoformat() == day
                and self.discipline_start <= completion_day <= self.host.clock.local_today())

    def list_completion_tasks_for_day(self, day):
        with self.lock:
            return {task for task, days in self.completions.items() if day in days}

    def list_completions_for_year(self, year):
        with self.lock:
            return {task: {day for day in days if day.startswith(f"{year}-")}
                    for task, days in self.completions.items()}

    def list_open_tasks(self, limit=20):
        return [row for row in self.list_tasks() + self.list_recurring() if not row["completed"]][:limit]

    def list_overdue_tasks(self, limit=10):
        today = self.host.clock.local_today().isoformat()
        return [row for row in self.list_open_tasks(None) if row["due_date"] and row["due_date"] < today][:limit]

    def list_upcoming_tasks(self, days=7, limit=10):
        today = self.host.clock.local_today()
        end = (today + timedelta(days=days)).isoformat()
        return [row for row in self.list_open_tasks(None)
                if row["due_date"] and today.isoformat() <= row["due_date"] <= end][:limit]

    def list_recent_completions(self, limit=8):
        return sorted((row for row in self.list_tasks() + self.list_recurring() if row["completed"]),
                      key=lambda row: row["completed_time"] or "", reverse=True)[:limit]

    def list_calendar_rows(self, start, end):
        return [row for row in self.list_tasks() + self.list_recurring()
                if row["due_date"] and start.isoformat() <= row["due_date"] <= end.isoformat()]

    def list_project_rows(self, projects, include_recurring=True):
        return [row for row in self.list_open_tasks(None) if row["project"] in (projects or [])
            and (include_recurring or row["source"] == "task")]

    def list_projects_with_open_tasks(self, include_recurring=True):
        counts = Counter(row["project"] for row in self.list_open_tasks(None)
                         if row["project"] and (include_recurring or row["source"] == "task"))
        return [{"project": project, "count": count} for project, count in sorted(counts.items())]

    def _weekly_counts(self, anchor, count):
        anchor = anchor or self.host.clock.local_today()
        monday = anchor - timedelta(days=anchor.weekday())
        return [{"date": (monday + timedelta(days=offset)).isoformat(), "dow": label,
                 "count": count((monday + timedelta(days=offset)).isoformat())}
                for offset, label in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))]

    def weekly_discipline_counts(self, anchor=None):
        return self._weekly_counts(anchor, lambda day: len(self.list_completion_tasks_for_day(day)))

    def weekly_task_completion_counts(self, anchor=None):
        rows = self.list_recent_completions(None)
        return self._weekly_counts(anchor, lambda day: sum(str(row["completed_time"] or "").startswith(day) for row in rows))

    def weekly_review(self, anchor=None):
        today = anchor or self.host.clock.local_today()
        start, end = (today - timedelta(days=7)).isoformat(), (today - timedelta(days=1)).isoformat()
        completed = [row for row in self.list_recent_completions(None)
                     if start <= str(row["completed_time"] or "")[:10] <= end]
        counts = Counter(row["catagory"] or "Uncategorized" for row in completed)
        discipline_counts = [len(self.list_completion_tasks_for_day((today - timedelta(days=offset)).isoformat()))
                             for offset in range(1, 8)]
        return {"start_iso": start, "end_iso": end, "completed_total": len(completed),
                "top_categories": [{"name": name, "count": count} for name, count in counts.most_common(5)],
                "discipline_days": sum(bool(count) for count in discipline_counts),
                "discipline_total": sum(discipline_counts), "carried_over": len(self.list_overdue_tasks(None)),
                "upcoming_next_week": len(self.list_upcoming_tasks(limit=None))}

    def adapters(self):
        return {name: getattr(self, name) for name in (
            "list_tasks", "get_task", "create_task", "update_task", "set_task_status", "toggle_task_completed",
            "list_recurring", "get_recurring", "create_recurring", "update_recurring",
            "set_recurring_status", "toggle_recurring_completed",
            "restore_task_row", "list_disciplines", "get_discipline", "computed_discipline_streak",
            "set_discipline_active", "list_discipline_completions_between",
            "list_discipline_history", "change_discipline_history",
            "completion_exists", "mark_completion", "unmark_completion",
            "list_completion_tasks_for_day", "list_completions_for_year",
            "list_open_tasks", "list_overdue_tasks", "list_upcoming_tasks", "list_recent_completions",
            "list_calendar_rows", "list_project_rows", "list_projects_with_open_tasks",
            "weekly_discipline_counts", "weekly_task_completion_counts", "weekly_review",
        )} | {
            "list_disciplines_pending_today": lambda: [row for row in self.list_disciplines(False)
                if not self.completion_exists(row["task"], self.host.clock.local_today().isoformat())],
            "list_discipline_streaks": lambda limit=8: self.list_disciplines(False)[:limit],
            "find_tasks_by_name": lambda query, include_completed=False, limit=10: [row for row in self.list_tasks() + self.list_recurring()
                if query.casefold() in row["task"].casefold() and (include_completed or not row["completed"])][:limit],
            "search_disciplines": lambda query, limit=6: [row for row in self.list_disciplines(False)
                if query.casefold() in row["task"].casefold()][:limit],
        }


@contextmanager
def preview_context(occurrence_demo: bool = False, *, discipline_demo: bool = False):
    with tempfile.TemporaryDirectory(prefix="luigi-workspace-preview-") as temporary, ExitStack() as stack:
        directory = Path(temporary)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("LUIGI_WEB_")}
        environment.update({
            "LUIGI_WEB_UI_TOKEN": secrets.token_urlsafe(32),
            "LUIGI_WEB_FINANCE_TOKEN": secrets.token_urlsafe(32),
            "LUIGI_WEB_DATA_DIR": str(directory),
            "LUIGI_WEB_MODULES_FILE": str(directory / "modules.json"),
            "LUIGI_WEB_LLM_PROVIDER": "disabled",
            "LUIGI_WEB_CARDS_REFRESH_HOURS": "0",
            "LUIGI_WEB_SECURE_COOKIES": "0",
            "LUIGI_WEB_TIMEZONE": "UTC",
            "LUIGI_WEB_RECURRENCE_OWNER": "web",
        })
        for key, filename in (
            ("LUIGI_WEB_CARDS_DB", "cards.db"), ("LUIGI_WEB_RPG_DB", "characters.db"),
            ("LUIGI_WEB_OPERATIONS_DB", "operations.db"), ("LUIGI_WEB_REVIEW_DB", "review.db"),
            ("LUIGI_WEB_FEEDBACK_DB", "feedback.db"), ("LUIGI_WEB_MAINTAINER_DB", "maintainer.db"),
            ("LUIGI_WEB_FINANCE_DB", "finance.db"), ("LUIGI_WEB_TASK_METADATA_FILE", "tasks.json"),
        ):
            environment[key] = str(directory / filename)
        stack.enter_context(patch.dict(os.environ, environment, clear=True))

        from luigi_web import paths
        from luigi_web.core import module_repositories

        stack.enter_context(patch.object(paths, "DATA_DIR", directory))
        stack.enter_context(patch.object(module_repositories, "DATA_DIR", directory))
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        from luigi_web import application, rpg
        from luigi_web.modules.discipline import history
        from scripts.preview_cards import seed_cards

        stack.enter_context(patch.object(application.db, "get_engine", side_effect=RuntimeError("Shared storage disabled in preview")))
        stack.enter_context(patch.object(application, "_UNDO_QUEUE", {}))
        stack.enter_context(patch.object(history, "_undo", OrderedDict()))
        seed_cards()
        rpg.init_db()
        application.operations.init_db()
        application.review.init_db()
        application.app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
        today = application.clock.local_today()
        tasks = [
            {
                "uuid": f"preview-task-{index}", "task": label, "priority": priority,
                "status": status, "completed": int(status == "Completed"), "catagory": category,
                "task_group": "Example project", "sub_group": "", "project_name": "Example project",
                "task_creation": (today - timedelta(days=5)).isoformat(),
                "start_time": (today - timedelta(days=2)).isoformat(),
                "due_date": (today + timedelta(days=offset)).isoformat(), "source": "task",
                "recurring": 0, "description": "", "relevant_link": "", "archived": 0,
            }
            for index, (label, status, priority, category, offset) in enumerate((
                ("Sketch a workspace layout", "In Progress", 3, "Creative", 0),
                ("Review the module contract", "Not Started", 2, "Work", 1),
                ("Plan the next release", "Not Started", 1, "Work", 3),
                ("Organize the example collection", "In Progress", 2, "Personal", 2),
                ("Check keyboard navigation", "Completed", 1, "Creative", -1),
            ), start=1)
        ]
        disciplines = [{
            "uuid": "preview-habit", "task": "Practice a skill", "catagory": "Learning",
            "frequency_per_week": 4, "current_streak": 3, "active": 1,
        }]
        if discipline_demo:
            disciplines.extend([
                {"uuid": "preview-habit-reading", "task": "Example reading practice", "catagory": "Learning",
                 "frequency_per_week": 3, "current_streak": 0, "active": 1},
                {"uuid": "preview-habit-daily", "task": "Example daily stretch", "catagory": "Wellbeing",
                 "frequency_per_week": 7, "current_streak": 0, "active": 1},
                {"uuid": "preview-habit-paused", "task": "Example sketch practice", "catagory": "Creative",
                 "frequency_per_week": 4, "current_streak": 0, "active": 0},
            ])
        recurring = [{
            "uuid": "preview-recurring-1", "task": "Example weekly workspace review", "priority": 2,
            "status": "Not Started", "completed": 0, "recurring": 1, "recurring_interval": 7,
            "due_date": (today + timedelta(days=7)).isoformat(), "catagory": "Example category",
            "task_creation": today.isoformat(), "archived": 0,
        }]
        if occurrence_demo:
            recurring.append({
                "uuid": "preview-recurring-past", "task": "Example recurring occurrence", "priority": 2,
                "status": "Completed", "completed": 1, "recurring": 1, "recurring_interval": 1,
                "due_date": (today - timedelta(days=1)).isoformat(), "catagory": "Example category",
                "task_creation": (today - timedelta(days=3)).isoformat(),
                "start_time": (today - timedelta(days=2)).isoformat(), "archived": 0,
            })
        store = _PreviewTasks(application, tasks, disciplines, today, recurring=recurring)
        if discipline_demo:
            for discipline, offsets in zip(disciplines, (
                (1, 2, 3, 35, 63, 95), (0, 3, 8, 38, 68, 98),
                (1, 2, 3, 4, 5, 6, 40, 70, 100), (2, 5, 12, 42, 72, 102),
            ), strict=True):
                store.seed_discipline_completions(discipline, today, offsets)
        adapters = store.adapters()
        values = {
            "list_follow_ups_preview": [], "list_follow_ups": [],
            "list_recent_activity": [], "list_disciplines_at_risk": [],
            "project_grouping_enabled": True,
        }
        for name, value in values.items():
            stack.enter_context(patch.object(application.db, name, side_effect=lambda *args, value=value, **kwargs: copy.deepcopy(value)))
        for name, adapter in adapters.items():
            stack.enter_context(patch.object(application.db, name, side_effect=adapter))
        for name in ("list_task_completion_events", "list_calendar_activity_events", "list_activity_timeline"):
            stack.enter_context(patch.object(application.db, name, return_value=(application.task_events.Capability(False, "Synthetic preview"), [])))
        stack.enter_context(patch.object(application.db, "has_web_column", return_value=True))
        stack.enter_context(patch.object(application, "_require_v2"))
        stack.enter_context(patch.object(application, "_reactivate_recurring", side_effect=store.generate_due))
        years = sorted({today.year} | {int(day[:4]) for days in store.completions.values() for day in days})
        stack.enter_context(patch.object(application, "_available_years", return_value=years if discipline_demo else [today.year]))
        if occurrence_demo:
            store.generate_due(today=today)

        for row in tasks[:2]:
            application.operations.set_today_selection(row["uuid"], "task", True, day=today)

        configure_preview_security(
            application.app, environment["LUIGI_WEB_UI_TOKEN"], enable_task_actions=True,
            discipline_history_uuids=tuple(row["uuid"] for row in disciplines),
        )

        yield application.app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--occurrence-demo", action="store_true", help="Include synthetic recurring occurrence history")
    parser.add_argument("--discipline-demo", action="store_true", help="Include synthetic weekly, daily, and paused habits")
    arguments = parser.parse_args()
    preview_options = {"discipline_demo": True} if arguments.discipline_demo else {}
    with preview_context(occurrence_demo=arguments.occurrence_demo, **preview_options) as app:
        if arguments.check:
            from fastapi.testclient import TestClient

            client = TestClient(app)
            endpoints = ("/modules", "/home", "/home/layout", "/home/preview", "/home/data", "/chat/panel", "/tasks", "/tasks/preview", "/calendar", "/projects", "/discipline", "/discipline/progress", "/discipline/history-preview", "/cards/mtg/decks", "/characters", "/finance/unlock")
            try:
                client.get("/")
                for path in endpoints:
                    response = client.get(path)
                    if response.status_code != 200:
                        raise RuntimeError(f"Synthetic preview check failed: {path} ({response.status_code})")
            finally:
                client.close()
            print(f"Validated {len(endpoints)} synthetic workspace endpoints without external services.")
            return
        import uvicorn

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", arguments.port))
            port = listener.getsockname()[1]
            print(f"Synthetic workspace preview: http://127.0.0.1:{port}/", flush=True)
            uvicorn.Server(uvicorn.Config(
                app, host="127.0.0.1", port=port, lifespan="off", access_log=False, log_level="warning",
            )).run(sockets=[listener])


if __name__ == "__main__":
    main()