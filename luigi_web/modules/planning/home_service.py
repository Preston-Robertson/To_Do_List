"""Live Home projection of task references and today's discipline completions."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ... import clock
from ..tasks import operations, repository as db

SELECTION_ERROR = "Today selections are unavailable. Please try again."


def due_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not value:
        return ""
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(str(value)).date().isoformat()
        except ValueError:
            return ""


def _done(row: dict[str, Any]) -> bool:
    return int(row.get("completed") or 0) == 1 or str(row.get("status") or "").casefold() == "completed"


def _normalized_title(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def load_home_state(*, today: date | None = None) -> dict[str, Any]:
    today = today or clock.local_today()
    task_rows = db.list_tasks()
    recurring_rows = db.list_recurring()
    habits = db.list_disciplines(False)
    completed_habits = {
        _normalized_title(title)
        for title in db.list_completion_tasks_for_day(today.isoformat())
    }
    selection_available = True
    try:
        selections = operations.list_today_selections(today)
    except Exception:
        selections = set()
        selection_available = False
    try:
        dependencies = operations.list_dependencies()
    except Exception:
        if selection_available:
            raise
        dependencies = []

    tasks: dict[tuple[str, str], dict[str, Any]] = {}
    for source, rows in (("task", task_rows), ("recurring", recurring_rows)):
        for row in rows:
            row_uuid = str(row.get("uuid") or "")
            reference = (source, row_uuid)
            if not row_uuid or row.get("archived") or reference in tasks:
                continue
            tasks[reference] = {
                "id": f"{source}:{row_uuid}",
                "uuid": row_uuid,
                "source": source,
                "title": str(row.get("task") or ""),
                "notes": str(row.get("description") or ""),
                "due": due_iso(row.get("due_date")),
                "today": reference in selections,
                "priority": int(row.get("priority") or 0),
                "done": _done(row),
                "status": str(row.get("status") or "Not Started"),
                "category": str(row.get("catagory") or ""),
                "blockers": [],
                "endpoint": "/tasks" if source == "task" else "/recurring",
            }
            if source == "recurring":
                if row.get("_recurrence_generated"):
                    tasks[reference]["history_locked"] = True
                if "_recurrence_generated" in row and _done(row) and row.get("completed_time"):
                    try:
                        tasks[reference]["completed_at"] = datetime.fromisoformat(
                            str(row["completed_time"])
                        ).isoformat()
                    except ValueError:
                        pass
    for edge in dependencies:
        dependent = tasks.get((edge["dependent_source"], str(edge["dependent_uuid"])))
        blocker = tasks.get((edge["blocker_source"], str(edge["blocker_uuid"])))
        if dependent is not None and blocker is not None and not blocker["done"]:
            if blocker["title"] not in dependent["blockers"]:
                dependent["blockers"].append(blocker["title"])

    return {
        "today": today.isoformat(),
        "tasks": list(tasks.values()),
        "habits": [
            {
                "id": str(row["uuid"]),
                "title": str(row.get("task") or ""),
                "done": _normalized_title(row.get("task")) in completed_habits,
                "frequency_per_week": int(row.get("frequency_per_week") or 0),
                "streak": int(row.get("current_streak") or 0),
            }
            for row in habits
            if row.get("uuid") and row.get("active", 1)
        ],
        "shortcuts": [],
        "selection_available": selection_available,
        "selection_error": None if selection_available else SELECTION_ERROR,
    }