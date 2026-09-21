"""Current local-week Discipline progress over the shared legacy adapter."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..tasks import repository as db
from .progress import weekly_progress


def normalize_title(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def discipline_progress(
    disciplines: list[dict[str, Any]],
    today: date,
    *,
    today_tasks: set[str] | None = None,
) -> dict[str, Any]:
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    completions = db.list_discipline_completions_between(week_start, week_end)
    completions_by_title: dict[str, set[str]] = {}
    for title, days in completions.items():
        completions_by_title.setdefault(normalize_title(title), set()).update(days)
    if today_tasks is None:
        today_tasks = db.list_completion_tasks_for_day(today.isoformat())
    today_by_title = {normalize_title(title) for title in today_tasks}
    progress = []
    for discipline in disciplines:
        title = str(discipline["task"])
        normalized_title = normalize_title(title)
        target = int(discipline["frequency_per_week"])
        progress.append({
            "uuid": discipline["uuid"],
            "active": bool(int(discipline.get("active") or 0)),
            "weekly": weekly_progress(completions_by_title.get(normalized_title, set()), target, today),
            "today_done": normalized_title in today_by_title,
            "daily_streak": db.computed_discipline_streak(title) if target == 7 else None,
        })
    return {
        "today": today.isoformat(),
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "disciplines": progress,
    }