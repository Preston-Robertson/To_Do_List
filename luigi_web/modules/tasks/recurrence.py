"""Calendar calculations for recurring task schedules."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Any

MONTH_ORDINAL_OPTIONS = (
    (1, "First"),
    (2, "Second"),
    (3, "Third"),
    (4, "Fourth"),
    (-1, "Last"),
)
VALID_MONTH_ORDINALS = frozenset(value for value, _ in MONTH_ORDINAL_OPTIONS)


def parse_monthly_schedule(ordinal: Any, weekday: Any) -> tuple[int, int] | None:
    """Return a validated ``(ordinal, weekday)`` pair or ``None``."""
    try:
        parsed_ordinal = int(ordinal)
        parsed_weekday = int(weekday)
    except (TypeError, ValueError):
        return None
    if parsed_ordinal not in VALID_MONTH_ORDINALS or not 0 <= parsed_weekday <= 6:
        return None
    return parsed_ordinal, parsed_weekday


def nth_weekday_of_month(
    year: int,
    month: int,
    ordinal: int,
    weekday: int,
) -> date | None:
    """Calculate a calendar-position weekday such as the first Monday."""
    schedule = parse_monthly_schedule(ordinal, weekday)
    if schedule is None:
        return None
    ordinal, weekday = schedule
    last_day = monthrange(year, month)[1]
    if ordinal == -1:
        final = date(year, month, last_day)
        day = last_day - ((final.weekday() - weekday) % 7)
        return date(year, month, day)

    first = date(year, month, 1)
    day = 1 + ((weekday - first.weekday()) % 7) + ((ordinal - 1) * 7)
    if day > last_day:
        return None
    return date(year, month, day)


def next_monthly_occurrence(
    after: date,
    ordinal: Any,
    weekday: Any,
) -> date | None:
    """Return the first matching monthly occurrence strictly after ``after``."""
    schedule = parse_monthly_schedule(ordinal, weekday)
    if schedule is None:
        return None
    ordinal, weekday = schedule
    year, month = after.year, after.month
    for _ in range(24):
        candidate = nth_weekday_of_month(year, month, ordinal, weekday)
        if candidate is not None and candidate > after:
            return candidate
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return None


def _stored_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _stored_weekdays(value: Any) -> set[int]:
    if not isinstance(value, str):
        return set()
    weekdays: set[int] = set()
    for part in value.split(","):
        try:
            weekday = int(part.strip())
        except (TypeError, ValueError):
            continue
        if 0 <= weekday <= 6:
            weekdays.add(weekday)
    return weekdays


def calendar_occurrence_dates(
    row: dict[str, Any],
    start: date,
    end: date,
) -> list[date]:
    """Project a recurring row's schedule into an inclusive calendar range."""
    if not row.get("recurring") or end < start:
        return []

    due = _stored_date(row.get("due_date"))
    created = _stored_date(row.get("task_creation"))
    completed = _stored_date(row.get("completed_time")) if row.get("completed") else None
    anchor = completed + timedelta(days=1) if completed else (due or created or start)
    lower = max(start, anchor)

    monthly = parse_monthly_schedule(
        row.get("recurring_month_ordinal"), row.get("recurring_month_weekday")
    )
    if monthly:
        ordinal, weekday = monthly
        dates: list[date] = []
        year, month = lower.year, lower.month
        while date(year, month, 1) <= end:
            candidate = nth_weekday_of_month(year, month, ordinal, weekday)
            if candidate is not None and lower <= candidate <= end:
                dates.append(candidate)
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
        return dates

    weekdays = _stored_weekdays(row.get("recurring_days"))
    if weekdays:
        dates = []
        cursor = lower
        while cursor <= end:
            if cursor.weekday() in weekdays:
                dates.append(cursor)
            cursor += timedelta(days=1)
        return dates

    try:
        interval = int(row.get("recurring_interval") or 0)
    except (TypeError, ValueError):
        return []
    if interval < 1:
        return []
    first = (
        completed + timedelta(days=interval)
        if completed else (due or created)
    )
    if first is None:
        return []
    while first < start:
        first += timedelta(days=interval)
    dates = []
    while first <= end:
        dates.append(first)
        first += timedelta(days=interval)
    return dates