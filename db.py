"""Database layer for luigi-web.

Owns the SQLAlchemy engine plus small, typed helpers for the five tables the
GUI touches (`tasks`, `recurring_tasks`, `discipline_list`,
`discipline_completions`, `follow_up_tasks`).

Non-obvious rules (from the LuigiBot schema, v2):

* ``id`` is ``GENERATED ALWAYS AS IDENTITY`` — never insert it explicitly.
* ``uuid`` is the durable row handle. All UPDATE/DELETE are scoped by it.
* Booleans are stored as ``INTEGER 0/1``.
* Dates/datetimes are stored as ISO-8601 ``TEXT``.
* Column spellings from the bot: ``catagory`` (sic), tasks use ``sub_group``,
  follow-ups use ``subgroup``.
"""
from __future__ import annotations

import os
import uuid as _uuid
from datetime import datetime, date
from typing import Any, Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

_engine: Engine | None = None


def _dsn() -> str:
    host = os.environ["LUIGI_WEB_PG_HOST"]
    port = os.environ.get("LUIGI_WEB_PG_PORT", "5432")
    db = os.environ["LUIGI_WEB_PG_DB"]
    user = os.environ["LUIGI_WEB_PG_USER"]
    pw = os.environ["LUIGI_WEB_PG_PASSWORD"]
    return f"postgresql+psycopg://{user}:{pw}@{host}:{port}/{db}"


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(_dsn(), pool_pre_ping=True, future=True)
    return _engine


def check_schema_version() -> int:
    """Return the schema version. Raises if the row is missing."""
    with get_engine().connect() as conn:
        row = conn.execute(text("SELECT version FROM schema_version")).first()
    if row is None:
        raise RuntimeError("schema_version table is empty — DB is not v2-migrated")
    return int(row[0])


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #

STATUS_VALUES = (
    "Not Started",
    "In Progress",
    "Pending",
    "Blocked",
    "Hiatus",
    "Completed",
)


def new_uuid() -> str:
    return str(_uuid.uuid4())


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today_iso() -> str:
    return date.today().isoformat()


def _to_int_bool(v: Any) -> int:
    """Coerce common form/JSON truthy values to the 0/1 the schema expects."""
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if int(v) != 0 else 0
    if isinstance(v, str):
        return 1 if v.strip().lower() in ("1", "true", "yes", "on") else 0
    return 0


def _rows(result) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in result]


# --------------------------------------------------------------------------- #
# tasks / recurring_tasks (identical shape)
# --------------------------------------------------------------------------- #

_TASK_COLUMNS = (
    "uuid", "task", "priority", "status", "due_date", "relevant_link",
    "catagory", "task_group", "sub_group", "task_creation", "start_time",
    "estimated_time", "logged_hours", "completed", "completed_time",
    "recurring", "recurring_interval",
)

# fields the edit form is allowed to change (mirrors bot's edit_task)
_TASK_EDITABLE = (
    "task", "priority", "due_date", "catagory", "task_group", "sub_group",
    "relevant_link", "status", "estimated_time",
)


def _list_task_like(table: str) -> list[dict[str, Any]]:
    q = text(f"""
        SELECT {", ".join(_TASK_COLUMNS)}
        FROM {table}
        ORDER BY completed ASC, priority DESC NULLS LAST, due_date ASC NULLS LAST, task ASC
    """)
    with get_engine().connect() as conn:
        return _rows(conn.execute(q))


def _get_task_like(table: str, row_uuid: str) -> dict[str, Any] | None:
    q = text(f"SELECT {', '.join(_TASK_COLUMNS)} FROM {table} WHERE uuid = :u")
    with get_engine().connect() as conn:
        row = conn.execute(q, {"u": row_uuid}).first()
    return dict(row._mapping) if row else None


def _create_task_like(table: str, data: dict[str, Any], recurring_default: int) -> str:
    row_uuid = new_uuid()
    payload = {
        "uuid": row_uuid,
        "task": data.get("task", "").strip(),
        "priority": int(data.get("priority") or 0),
        "status": data.get("status") or "Not Started",
        "due_date": data.get("due_date") or None,
        "relevant_link": data.get("relevant_link") or None,
        "catagory": data.get("catagory") or None,
        "task_group": data.get("task_group") or None,
        "sub_group": data.get("sub_group") or None,
        "task_creation": today_iso(),
        "start_time": None,
        "estimated_time": float(data["estimated_time"]) if data.get("estimated_time") not in (None, "") else None,
        "logged_hours": 0.0,
        "completed": 0,
        "completed_time": None,
        "recurring": recurring_default,
        "recurring_interval": int(data["recurring_interval"]) if data.get("recurring_interval") not in (None, "") else None,
    }
    cols = ", ".join(payload.keys())
    binds = ", ".join(f":{k}" for k in payload)
    q = text(f"INSERT INTO {table} ({cols}) VALUES ({binds})")
    with get_engine().begin() as conn:
        conn.execute(q, payload)
    return row_uuid


def _update_task_like(table: str, row_uuid: str, data: dict[str, Any]) -> None:
    updates: dict[str, Any] = {}
    for field in _TASK_EDITABLE:
        if field not in data:
            continue
        val = data[field]
        if field == "priority":
            val = int(val or 0)
        elif field == "estimated_time":
            val = float(val) if val not in (None, "") else None
        elif isinstance(val, str) and val == "":
            val = None
        updates[field] = val
    if not updates:
        return
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["u"] = row_uuid
    q = text(f"UPDATE {table} SET {set_clause} WHERE uuid = :u")
    with get_engine().begin() as conn:
        conn.execute(q, updates)


def _set_task_like_status(table: str, row_uuid: str, status: str) -> None:
    if status not in STATUS_VALUES:
        raise ValueError(f"invalid status: {status}")
    # Dragging to/from the Completed column toggles the completed flag too so
    # LuigiBot's `!L to_do_list` view stays in sync.
    completed = 1 if status == "Completed" else 0
    completed_time = now_iso() if completed else None
    q = text(f"""
        UPDATE {table}
        SET status = :s, completed = :c, completed_time = :ct
        WHERE uuid = :u
    """)
    with get_engine().begin() as conn:
        conn.execute(q, {"s": status, "c": completed, "ct": completed_time, "u": row_uuid})


def _toggle_task_like_completed(table: str, row_uuid: str) -> int:
    """Flip the ``completed`` flag; return the new value."""
    with get_engine().begin() as conn:
        cur = conn.execute(
            text(f"SELECT completed FROM {table} WHERE uuid = :u"),
            {"u": row_uuid},
        ).scalar_one()
        new_val = 0 if int(cur or 0) == 1 else 1
        conn.execute(
            text(f"""
                UPDATE {table}
                SET completed = :c,
                    completed_time = :ct,
                    status = CASE WHEN :c = 1 THEN 'Completed' ELSE status END
                WHERE uuid = :u
            """),
            {"c": new_val, "ct": now_iso() if new_val else None, "u": row_uuid},
        )
    return new_val


def _delete_task_like(table: str, row_uuid: str) -> None:
    q = text(f"DELETE FROM {table} WHERE uuid = :u")
    with get_engine().begin() as conn:
        conn.execute(q, {"u": row_uuid})


# public: tasks
def list_tasks() -> list[dict[str, Any]]:
    return _list_task_like("tasks")


def get_task(row_uuid: str):
    return _get_task_like("tasks", row_uuid)


def create_task(data: dict[str, Any]) -> str:
    return _create_task_like("tasks", data, recurring_default=0)


def update_task(row_uuid: str, data: dict[str, Any]) -> None:
    _update_task_like("tasks", row_uuid, data)


def set_task_status(row_uuid: str, status: str) -> None:
    _set_task_like_status("tasks", row_uuid, status)


def toggle_task_completed(row_uuid: str) -> int:
    return _toggle_task_like_completed("tasks", row_uuid)


def delete_task(row_uuid: str) -> None:
    _delete_task_like("tasks", row_uuid)


# public: recurring_tasks
def list_recurring() -> list[dict[str, Any]]:
    return _list_task_like("recurring_tasks")


def get_recurring(row_uuid: str):
    return _get_task_like("recurring_tasks", row_uuid)


def create_recurring(data: dict[str, Any]) -> str:
    return _create_task_like("recurring_tasks", data, recurring_default=1)


def update_recurring(row_uuid: str, data: dict[str, Any]) -> None:
    _update_task_like("recurring_tasks", row_uuid, data)


def set_recurring_status(row_uuid: str, status: str) -> None:
    _set_task_like_status("recurring_tasks", row_uuid, status)


def toggle_recurring_completed(row_uuid: str) -> int:
    return _toggle_task_like_completed("recurring_tasks", row_uuid)


def delete_recurring(row_uuid: str) -> None:
    _delete_task_like("recurring_tasks", row_uuid)


# --------------------------------------------------------------------------- #
# discipline_list + discipline_completions
# --------------------------------------------------------------------------- #

_DISCIPLINE_COLUMNS = (
    "uuid", "task", "catagory", "frequency_per_week", "active", "current_streak",
)


def list_disciplines(include_inactive: bool = True) -> list[dict[str, Any]]:
    where = "" if include_inactive else "WHERE active = 1"
    q = text(f"""
        SELECT {", ".join(_DISCIPLINE_COLUMNS)}
        FROM discipline_list
        {where}
        ORDER BY active DESC, catagory ASC, task ASC
    """)
    with get_engine().connect() as conn:
        return _rows(conn.execute(q))


def get_discipline(row_uuid: str):
    q = text(f"SELECT {', '.join(_DISCIPLINE_COLUMNS)} FROM discipline_list WHERE uuid = :u")
    with get_engine().connect() as conn:
        row = conn.execute(q, {"u": row_uuid}).first()
    return dict(row._mapping) if row else None


def create_discipline(data: dict[str, Any]) -> str:
    row_uuid = new_uuid()
    payload = {
        "uuid": row_uuid,
        "task": data["task"].strip(),
        "catagory": data.get("catagory") or None,
        "frequency_per_week": int(data.get("frequency_per_week") or 0),
        "active": _to_int_bool(data.get("active", 1)),
        "current_streak": 0,
    }
    cols = ", ".join(payload.keys())
    binds = ", ".join(f":{k}" for k in payload)
    q = text(f"INSERT INTO discipline_list ({cols}) VALUES ({binds})")
    with get_engine().begin() as conn:
        conn.execute(q, payload)
    return row_uuid


def update_discipline(row_uuid: str, data: dict[str, Any]) -> None:
    editable = ("task", "catagory", "frequency_per_week", "active")
    updates: dict[str, Any] = {}
    for field in editable:
        if field not in data:
            continue
        val = data[field]
        if field == "frequency_per_week":
            val = int(val or 0)
        elif field == "active":
            val = _to_int_bool(val)
        elif isinstance(val, str) and val == "":
            val = None
        updates[field] = val
    if not updates:
        return
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["u"] = row_uuid
    q = text(f"UPDATE discipline_list SET {set_clause} WHERE uuid = :u")
    with get_engine().begin() as conn:
        conn.execute(q, updates)


def deactivate_discipline(row_uuid: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE discipline_list SET active = 0 WHERE uuid = :u"),
            {"u": row_uuid},
        )


def list_completions_for_year(year: int) -> dict[str, set[str]]:
    """Return ``{task_name: {"YYYY-MM-DD", ...}}`` for the given year."""
    q = text("""
        SELECT task, completed_date
        FROM discipline_completions
        WHERE completed_date >= :start AND completed_date <= :end
    """)
    start = f"{year:04d}-01-01"
    end = f"{year:04d}-12-31"
    result: dict[str, set[str]] = {}
    with get_engine().connect() as conn:
        for row in conn.execute(q, {"start": start, "end": end}):
            result.setdefault(row.task, set()).add(row.completed_date)
    return result


def mark_completion(task: str, catagory: str | None, day: str) -> None:
    """Insert (task, day). Uses ON CONFLICT DO NOTHING so this is idempotent."""
    q = text("""
        INSERT INTO discipline_completions (task, catagory, completed_date, logged_at)
        VALUES (:t, :c, :d, :ts)
        ON CONFLICT (task, completed_date) DO NOTHING
    """)
    with get_engine().begin() as conn:
        conn.execute(q, {"t": task, "c": catagory, "d": day, "ts": now_iso()})


def unmark_completion(task: str, day: str) -> None:
    q = text("DELETE FROM discipline_completions WHERE task = :t AND completed_date = :d")
    with get_engine().begin() as conn:
        conn.execute(q, {"t": task, "d": day})


def compute_streak(dates: Iterable[str]) -> int:
    """Consecutive days ending today or yesterday. Zero otherwise.

    Yesterday is allowed so a streak isn't shown as broken before the user has
    logged today yet.
    """
    day_set = set(dates)
    if not day_set:
        return 0
    today = date.today()
    from datetime import timedelta
    anchor = today if today.isoformat() in day_set else today - timedelta(days=1)
    if anchor.isoformat() not in day_set:
        return 0
    streak = 0
    cur = anchor
    while cur.isoformat() in day_set:
        streak += 1
        cur -= timedelta(days=1)
    return streak


# --------------------------------------------------------------------------- #
# follow_up_tasks
# --------------------------------------------------------------------------- #
# NOTE: follow_up_tasks uses ``subgroup`` (no underscore), unlike tasks.

_FOLLOWUP_COLUMNS = (
    "uuid", "trigger_task", "follow_up_task", "catagory", "task_group",
    "subgroup", "relevant_link", "priority", "estimated_time",
    "due_offset_days", "created",
)

_FOLLOWUP_EDITABLE = (
    "trigger_task", "follow_up_task", "catagory", "task_group", "subgroup",
    "relevant_link", "priority", "estimated_time", "due_offset_days",
)


def list_follow_ups() -> list[dict[str, Any]]:
    q = text(f"""
        SELECT {", ".join(_FOLLOWUP_COLUMNS)}
        FROM follow_up_tasks
        ORDER BY trigger_task ASC, priority DESC NULLS LAST, follow_up_task ASC
    """)
    with get_engine().connect() as conn:
        return _rows(conn.execute(q))


def get_follow_up(row_uuid: str):
    q = text(f"SELECT {', '.join(_FOLLOWUP_COLUMNS)} FROM follow_up_tasks WHERE uuid = :u")
    with get_engine().connect() as conn:
        row = conn.execute(q, {"u": row_uuid}).first()
    return dict(row._mapping) if row else None


def create_follow_up(data: dict[str, Any]) -> str:
    row_uuid = new_uuid()
    payload = {
        "uuid": row_uuid,
        "trigger_task": data.get("trigger_task", "").strip(),
        "follow_up_task": data.get("follow_up_task", "").strip(),
        "catagory": data.get("catagory") or None,
        "task_group": data.get("task_group") or None,
        "subgroup": data.get("subgroup") or None,
        "relevant_link": data.get("relevant_link") or None,
        "priority": int(data.get("priority") or 0),
        "estimated_time": float(data["estimated_time"]) if data.get("estimated_time") not in (None, "") else None,
        "due_offset_days": int(data["due_offset_days"]) if data.get("due_offset_days") not in (None, "") else None,
        "created": now_iso(),
    }
    cols = ", ".join(payload.keys())
    binds = ", ".join(f":{k}" for k in payload)
    q = text(f"INSERT INTO follow_up_tasks ({cols}) VALUES ({binds})")
    with get_engine().begin() as conn:
        conn.execute(q, payload)
    return row_uuid


def update_follow_up(row_uuid: str, data: dict[str, Any]) -> None:
    updates: dict[str, Any] = {}
    for field in _FOLLOWUP_EDITABLE:
        if field not in data:
            continue
        val = data[field]
        if field == "priority":
            val = int(val or 0)
        elif field == "estimated_time":
            val = float(val) if val not in (None, "") else None
        elif field == "due_offset_days":
            val = int(val) if val not in (None, "") else None
        elif isinstance(val, str) and val == "":
            val = None
        updates[field] = val
    if not updates:
        return
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["u"] = row_uuid
    q = text(f"UPDATE follow_up_tasks SET {set_clause} WHERE uuid = :u")
    with get_engine().begin() as conn:
        conn.execute(q, updates)


def delete_follow_up(row_uuid: str) -> None:
    q = text("DELETE FROM follow_up_tasks WHERE uuid = :u")
    with get_engine().begin() as conn:
        conn.execute(q, {"u": row_uuid})
