"""Current-week progress without assuming fixed completion weekdays."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable


def weekly_progress(completion_dates: Iterable[str], target: int, today: date) -> dict[str, object]:
    if type(target) is not int or not 1 <= target <= 7:
        raise ValueError("Weekly target must be between 1 and 7")
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    completed = set()
    for value in completion_dates:
        try:
            day = date.fromisoformat(value)
        except (TypeError, ValueError):
            continue
        if monday <= day <= today:
            completed.add(day)
    count = len(completed)
    return {
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "count": count,
        "target": target,
        "remaining": max(0, target - count),
        "target_met": count >= target,
    }