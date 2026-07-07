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
from datetime import datetime, date, timedelta
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

# Column display order for the Kanban board (row-major, 3 per row).
# Row 1: Not Started | In Progress | Completed
# Row 2: Blocked     | Hiatus      | Pending
STATUS_DISPLAY_ORDER = (
    "Not Started",
    "In Progress",
    "Completed",
    "Blocked",
    "Hiatus",
    "Pending",
)

OPEN_STATUSES = ("Not Started", "In Progress")


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


def restore_task_row(table: str, snapshot: dict[str, Any]) -> None:
    """Idempotent 'put back' for a task-like row snapshot.

    Used by the in-memory undo queue in ``app.py`` to reverse a delete /
    complete / snooze without the caller needing to know which of those
    operations produced the snapshot. If the uuid still exists we UPDATE
    every column back to what it was; if the row is gone we re-INSERT with
    the original uuid. Table is validated against the two known task-like
    tables so ``{table}`` interpolation is safe.
    """
    if table not in {"tasks", "recurring_tasks"}:
        raise ValueError(f"restore_task_row: table {table!r} not allowed")
    row_uuid = snapshot.get("uuid")
    if not row_uuid:
        raise ValueError("restore_task_row: snapshot is missing uuid")
    payload = {c: snapshot.get(c) for c in _TASK_COLUMNS}
    with get_engine().begin() as conn:
        exists = conn.execute(
            text(f"SELECT 1 FROM {table} WHERE uuid = :u"), {"u": row_uuid}
        ).first()
        if exists:
            set_clause = ", ".join(f"{c} = :{c}" for c in _TASK_COLUMNS if c != "uuid")
            payload["u"] = row_uuid
            conn.execute(
                text(f"UPDATE {table} SET {set_clause} WHERE uuid = :u"), payload
            )
        else:
            col_list = ", ".join(_TASK_COLUMNS)
            bind_list = ", ".join(f":{c}" for c in _TASK_COLUMNS)
            conn.execute(
                text(f"INSERT INTO {table} ({col_list}) VALUES ({bind_list})"), payload
            )


def _snooze_task_like(table: str, row_uuid: str, days: int) -> str | None:
    """Push ``due_date`` forward by ``days``.

    Base is the later of today or the current due_date, so snoozing an item
    that's already past its due date defers from today (not from the stale
    date). Returns the new ISO date, or ``None`` if the row is gone.
    """
    with get_engine().begin() as conn:
        cur = conn.execute(
            text(f"SELECT due_date FROM {table} WHERE uuid = :u"),
            {"u": row_uuid},
        ).first()
        if cur is None:
            return None
        today = date.today()
        base = today
        if cur.due_date:
            try:
                existing = date.fromisoformat(cur.due_date[:10])
                if existing > today:
                    base = existing
            except ValueError:
                pass
        new_due = (base + timedelta(days=int(days))).isoformat()
        conn.execute(
            text(f"UPDATE {table} SET due_date = :d WHERE uuid = :u"),
            {"d": new_due, "u": row_uuid},
        )
    return new_due


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


def snooze_task(row_uuid: str, days: int) -> str | None:
    return _snooze_task_like("tasks", row_uuid, days)


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


def snooze_recurring(row_uuid: str, days: int) -> str | None:
    return _snooze_task_like("recurring_tasks", row_uuid, days)


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


def delete_discipline(row_uuid: str) -> dict[str, Any] | None:
    """Hard-delete a discipline and all its completions. Returns a snapshot
    dict ``{"discipline": {...}, "completions": [{...}, ...]}`` suitable for
    ``restore_discipline_row``, or ``None`` if the row didn't exist.

    Completions are deleted by task-name (the FK the schema actually has),
    then the ``discipline_list`` row is removed. Both happen in one
    transaction so a partial failure leaves the DB consistent.
    """
    with get_engine().begin() as conn:
        row = conn.execute(
            text(f"SELECT {', '.join(_DISCIPLINE_COLUMNS)} FROM discipline_list WHERE uuid = :u"),
            {"u": row_uuid},
        ).first()
        if row is None:
            return None
        disc = dict(row._mapping)
        comps = conn.execute(
            text("""
                SELECT task, catagory, completed_date, logged_at
                FROM discipline_completions
                WHERE task = :t
            """),
            {"t": disc["task"]},
        ).all()
        snapshot = {
            "discipline": disc,
            "completions": [dict(c._mapping) for c in comps],
        }
        conn.execute(
            text("DELETE FROM discipline_completions WHERE task = :t"),
            {"t": disc["task"]},
        )
        conn.execute(
            text("DELETE FROM discipline_list WHERE uuid = :u"),
            {"u": row_uuid},
        )
    return snapshot


def restore_discipline_row(snapshot: dict[str, Any]) -> None:
    """Reverse ``delete_discipline`` from its snapshot.

    Idempotent: uses UPDATE-or-INSERT on ``discipline_list`` and
    ``ON CONFLICT DO NOTHING`` on ``discipline_completions``, so calling this
    twice (or after a partial recovery) leaves the same end state.
    """
    disc = snapshot.get("discipline") or {}
    row_uuid = disc.get("uuid")
    if not row_uuid:
        raise ValueError("restore_discipline_row: snapshot is missing discipline.uuid")
    payload = {c: disc.get(c) for c in _DISCIPLINE_COLUMNS}
    with get_engine().begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM discipline_list WHERE uuid = :u"), {"u": row_uuid}
        ).first()
        if exists:
            set_clause = ", ".join(f"{c} = :{c}" for c in _DISCIPLINE_COLUMNS if c != "uuid")
            payload["u"] = row_uuid
            conn.execute(
                text(f"UPDATE discipline_list SET {set_clause} WHERE uuid = :u"),
                payload,
            )
        else:
            col_list = ", ".join(_DISCIPLINE_COLUMNS)
            bind_list = ", ".join(f":{c}" for c in _DISCIPLINE_COLUMNS)
            conn.execute(
                text(f"INSERT INTO discipline_list ({col_list}) VALUES ({bind_list})"),
                payload,
            )
        for comp in snapshot.get("completions") or []:
            conn.execute(
                text("""
                    INSERT INTO discipline_completions (task, catagory, completed_date, logged_at)
                    VALUES (:t, :c, :d, :ts)
                    ON CONFLICT (task, completed_date) DO NOTHING
                """),
                {
                    "t": comp.get("task"),
                    "c": comp.get("catagory"),
                    "d": comp.get("completed_date"),
                    "ts": comp.get("logged_at") or now_iso(),
                },
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


# --------------------------------------------------------------------------- #
# Home-page widget queries
# --------------------------------------------------------------------------- #

def list_open_tasks(limit: int | None = 20) -> list[dict[str, Any]]:
    """Tasks + recurring rows in Not Started / In Progress, most urgent first."""
    lim = "" if limit is None else f"LIMIT {int(limit)}"
    q = text(f"""
        SELECT uuid, task, priority, status, due_date, catagory,
               task_group, sub_group, estimated_time, 'task' AS source
        FROM tasks
        WHERE status IN ('Not Started', 'In Progress') AND completed = 0
        UNION ALL
        SELECT uuid, task, priority, status, due_date, catagory,
               task_group, sub_group, estimated_time, 'recurring' AS source
        FROM recurring_tasks
        WHERE status IN ('Not Started', 'In Progress') AND completed = 0
        ORDER BY priority DESC NULLS LAST, due_date ASC NULLS LAST, task ASC
        {lim}
    """)
    with get_engine().connect() as conn:
        return _rows(conn.execute(q))


def list_disciplines_pending_today() -> list[dict[str, Any]]:
    """Active disciplines that have NOT been marked done for today."""
    today = today_iso()
    q = text("""
        SELECT dl.uuid, dl.task, dl.catagory, dl.frequency_per_week
        FROM discipline_list dl
        WHERE dl.active = 1
          AND NOT EXISTS (
              SELECT 1 FROM discipline_completions dc
              WHERE dc.task = dl.task AND dc.completed_date = :today
          )
        ORDER BY dl.catagory ASC NULLS LAST, dl.task ASC
    """)
    with get_engine().connect() as conn:
        return _rows(conn.execute(q, {"today": today}))


def list_disciplines_at_risk() -> list[dict[str, Any]]:
    """Active disciplines whose current streak is about to break.

    Definition: ``current_streak > 0`` (something to lose), not yet marked
    done today, and the last completion is at least ``ceil(7 / freq)`` days
    ago — the "expected gap" between hits for the discipline's frequency.
    A daily discipline (freq=7) becomes at-risk 1 full day after the last
    hit; a 3x/week (freq=3) after ~3 days. Tightest breakers first.
    """
    today = date.today()
    q = text("""
        SELECT dl.uuid, dl.task, dl.catagory, dl.current_streak,
               dl.frequency_per_week,
               MAX(dc.completed_date) AS last_completed
        FROM discipline_list dl
        LEFT JOIN discipline_completions dc ON dc.task = dl.task
        WHERE dl.active = 1 AND dl.current_streak > 0
        GROUP BY dl.uuid, dl.task, dl.catagory, dl.current_streak,
                 dl.frequency_per_week
        HAVING MAX(dc.completed_date) IS NULL OR MAX(dc.completed_date) < :today
    """)
    with get_engine().connect() as conn:
        rows = _rows(conn.execute(q, {"today": today.isoformat()}))

    out: list[dict[str, Any]] = []
    for r in rows:
        freq = int(r.get("frequency_per_week") or 0)
        if freq <= 0:
            continue
        expected_gap = max(1, -(-7 // freq))  # ceil(7 / freq)
        last = r.get("last_completed")
        if last:
            try:
                days_since = (today - date.fromisoformat(str(last)[:10])) .days
            except ValueError:
                continue
        else:
            days_since = 999
        if days_since >= expected_gap:
            out.append({**r, "days_since": days_since, "expected_gap": expected_gap})
    out.sort(
        key=lambda r: (
            -(r["days_since"] / max(1, r["expected_gap"])),
            -int(r.get("current_streak") or 0),
        )
    )
    return out


def _week_bounds(anchor: date | None = None) -> tuple[date, list[date]]:
    """Return (monday_date, [mon..sun]) for the week containing ``anchor``."""
    if anchor is None:
        anchor = date.today()
    monday = anchor - timedelta(days=anchor.weekday())  # Mon=0..Sun=6
    days = [monday + timedelta(days=i) for i in range(7)]
    return monday, days


def weekly_discipline_counts(anchor: date | None = None) -> list[dict[str, Any]]:
    """List of 7 entries {date, dow, count} Mon..Sun for the given week."""
    _, days = _week_bounds(anchor)
    start, end = days[0].isoformat(), days[-1].isoformat()
    q = text("""
        SELECT completed_date, COUNT(*) AS n
        FROM discipline_completions
        WHERE completed_date >= :start AND completed_date <= :end
        GROUP BY completed_date
    """)
    counts: dict[str, int] = {}
    with get_engine().connect() as conn:
        for row in conn.execute(q, {"start": start, "end": end}):
            counts[row.completed_date] = int(row.n)
    labels = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    return [
        {"date": d.isoformat(), "dow": labels[i], "count": counts.get(d.isoformat(), 0)}
        for i, d in enumerate(days)
    ]


def weekly_task_completion_counts(anchor: date | None = None) -> list[dict[str, Any]]:
    """List of 7 entries {date, dow, count} for tasks completed each day this week.

    Counts both ``tasks`` and ``recurring_tasks`` where ``completed_time`` falls
    on the day (compared by ISO date prefix).
    """
    _, days = _week_bounds(anchor)
    start, end = days[0].isoformat(), days[-1].isoformat()
    # completed_time is a full ISO timestamp; compare by its first 10 chars.
    q = text("""
        SELECT SUBSTR(completed_time, 1, 10) AS d, COUNT(*) AS n
        FROM (
            SELECT completed_time FROM tasks
            WHERE completed = 1 AND completed_time IS NOT NULL
              AND SUBSTR(completed_time, 1, 10) BETWEEN :start AND :end
            UNION ALL
            SELECT completed_time FROM recurring_tasks
            WHERE completed = 1 AND completed_time IS NOT NULL
              AND SUBSTR(completed_time, 1, 10) BETWEEN :start AND :end
        ) x
        GROUP BY SUBSTR(completed_time, 1, 10)
    """)
    counts: dict[str, int] = {}
    with get_engine().connect() as conn:
        for row in conn.execute(q, {"start": start, "end": end}):
            counts[row.d] = int(row.n)
    labels = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    return [
        {"date": d.isoformat(), "dow": labels[i], "count": counts.get(d.isoformat(), 0)}
        for i, d in enumerate(days)
    ]


def weekly_review(anchor: date | None = None) -> dict[str, Any]:
    """One-shot rollup for the Home "Weekly review" widget.

    Reports on the most recent *complete* week (the 7 days ending yesterday)
    so the widget stays stable across a Monday-morning refresh. Returns:

    * ``start_iso`` / ``end_iso``     bounds of the reviewed window
    * ``completed_total``             total tasks + recurring completed in-window
    * ``top_categories``              [{name, count}] top 5 by completions
    * ``discipline_days``             number of days with >=1 discipline completion
    * ``discipline_total``            total discipline completions in-window
    * ``carried_over``                open tasks whose due_date is < today (still overdue)
    * ``upcoming_next_week``          open tasks due within the next 7 days
    """
    end = (anchor or date.today()) - timedelta(days=1)
    start = end - timedelta(days=6)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    with get_engine().connect() as conn:
        completed = conn.execute(text("""
            SELECT COALESCE(NULLIF(TRIM(catagory), ''), 'Uncategorized') AS cat,
                   COUNT(*) AS n
            FROM (
                SELECT catagory, completed_time FROM tasks
                WHERE completed = 1 AND completed_time IS NOT NULL
                  AND SUBSTR(completed_time, 1, 10) BETWEEN :s AND :e
                UNION ALL
                SELECT catagory, completed_time FROM recurring_tasks
                WHERE completed = 1 AND completed_time IS NOT NULL
                  AND SUBSTR(completed_time, 1, 10) BETWEEN :s AND :e
            ) x
            GROUP BY COALESCE(NULLIF(TRIM(catagory), ''), 'Uncategorized')
            ORDER BY n DESC
        """), {"s": start_iso, "e": end_iso}).fetchall()

        disc = conn.execute(text("""
            SELECT COUNT(*) AS total,
                   COUNT(DISTINCT completed_date) AS days
            FROM discipline_completions
            WHERE completed_date BETWEEN :s AND :e
        """), {"s": start_iso, "e": end_iso}).first()

        today = today_iso()
        carried = conn.execute(text("""
            SELECT COUNT(*) AS n FROM (
                SELECT uuid FROM tasks
                WHERE completed = 0 AND (status IS NULL OR status != 'Completed')
                  AND due_date IS NOT NULL AND SUBSTR(due_date, 1, 10) < :today
                UNION ALL
                SELECT uuid FROM recurring_tasks
                WHERE completed = 0 AND (status IS NULL OR status != 'Completed')
                  AND due_date IS NOT NULL AND SUBSTR(due_date, 1, 10) < :today
            ) x
        """), {"today": today}).scalar_one()

        soon = (date.today() + timedelta(days=7)).isoformat()
        upcoming = conn.execute(text("""
            SELECT COUNT(*) AS n FROM (
                SELECT uuid FROM tasks
                WHERE completed = 0 AND (status IS NULL OR status != 'Completed')
                  AND due_date IS NOT NULL
                  AND SUBSTR(due_date, 1, 10) BETWEEN :today AND :soon
                UNION ALL
                SELECT uuid FROM recurring_tasks
                WHERE completed = 0 AND (status IS NULL OR status != 'Completed')
                  AND due_date IS NOT NULL
                  AND SUBSTR(due_date, 1, 10) BETWEEN :today AND :soon
            ) x
        """), {"today": today, "soon": soon}).scalar_one()

    rows = [{"name": r.cat, "count": int(r.n)} for r in completed]
    return {
        "start_iso": start_iso,
        "end_iso": end_iso,
        "completed_total": sum(r["count"] for r in rows),
        "top_categories": rows[:5],
        "discipline_total": int(disc.total) if disc else 0,
        "discipline_days": int(disc.days) if disc else 0,
        "carried_over": int(carried or 0),
        "upcoming_next_week": int(upcoming or 0),
    }


def list_overdue_tasks(limit: int | None = 10) -> list[dict[str, Any]]:
    """Open tasks whose due_date is strictly before today, worst first.

    Filters on BOTH ``completed = 0`` and ``status != 'Completed'`` — some
    rows drift out of sync when a status is set through a path that skips
    the completed flag (e.g. an older LuigiBot write), so we belt-and-suspenders.
    """
    lim = "" if limit is None else f"LIMIT {int(limit)}"
    today = today_iso()
    q = text(f"""
        SELECT uuid, task, priority, status, due_date, catagory, 'task' AS source
        FROM tasks
        WHERE completed = 0
          AND (status IS NULL OR status != 'Completed')
          AND due_date IS NOT NULL AND due_date < :today
        UNION ALL
        SELECT uuid, task, priority, status, due_date, catagory, 'recurring' AS source
        FROM recurring_tasks
        WHERE completed = 0
          AND (status IS NULL OR status != 'Completed')
          AND due_date IS NOT NULL AND due_date < :today
        ORDER BY due_date ASC, priority DESC NULLS LAST
        {lim}
    """)
    with get_engine().connect() as conn:
        return _rows(conn.execute(q, {"today": today}))


def list_upcoming_tasks(days: int = 7, limit: int | None = 10) -> list[dict[str, Any]]:
    """Open tasks due today through today+``days`` (inclusive)."""
    lim = "" if limit is None else f"LIMIT {int(limit)}"
    today = date.today()
    end = (today + timedelta(days=int(days))).isoformat()
    q = text(f"""
        SELECT uuid, task, priority, status, due_date, catagory, 'task' AS source
        FROM tasks
        WHERE completed = 0
          AND (status IS NULL OR status != 'Completed')
          AND due_date IS NOT NULL
          AND due_date >= :today AND due_date <= :end
        UNION ALL
        SELECT uuid, task, priority, status, due_date, catagory, 'recurring' AS source
        FROM recurring_tasks
        WHERE completed = 0
          AND (status IS NULL OR status != 'Completed')
          AND due_date IS NOT NULL
          AND due_date >= :today AND due_date <= :end
        ORDER BY due_date ASC, priority DESC NULLS LAST
        {lim}
    """)
    with get_engine().connect() as conn:
        return _rows(conn.execute(q, {"today": today.isoformat(), "end": end}))


def list_recent_completions(limit: int = 8) -> list[dict[str, Any]]:
    """Most-recently completed tasks + recurring rows."""
    q = text(f"""
        SELECT uuid, task, priority, catagory, completed_time, 'task' AS source
        FROM tasks
        WHERE completed = 1 AND completed_time IS NOT NULL
        UNION ALL
        SELECT uuid, task, priority, catagory, completed_time, 'recurring' AS source
        FROM recurring_tasks
        WHERE completed = 1 AND completed_time IS NOT NULL
        ORDER BY completed_time DESC
        LIMIT {int(limit)}
    """)
    with get_engine().connect() as conn:
        return _rows(conn.execute(q))


def list_discipline_streaks(limit: int = 8) -> list[dict[str, Any]]:
    """Active disciplines sorted by current_streak DESC, then task ASC."""
    q = text(f"""
        SELECT uuid, task, catagory, frequency_per_week, current_streak
        FROM discipline_list
        WHERE active = 1
        ORDER BY current_streak DESC NULLS LAST, task ASC
        LIMIT {int(limit)}
    """)
    with get_engine().connect() as conn:
        return _rows(conn.execute(q))


def list_follow_ups_preview(limit: int = 8) -> list[dict[str, Any]]:
    """Highest-priority follow-ups for the Home dashboard."""
    q = text(f"""
        SELECT uuid, trigger_task, follow_up_task, catagory, priority,
               due_offset_days
        FROM follow_up_tasks
        ORDER BY priority DESC NULLS LAST, trigger_task ASC
        LIMIT {int(limit)}
    """)
    with get_engine().connect() as conn:
        return _rows(conn.execute(q))


def list_recent_activity(limit: int = 15, days: int = 14) -> list[dict[str, Any]]:
    """Reverse-chronological feed of writes across all luigi_todo tables.

    Captures writes from BOTH LuigiBot and luigi-web because it reads the
    existing timestamp columns (``task_creation``, ``completed_time``,
    ``logged_at``, ``created``) rather than a bespoke audit table — no DDL
    from the GUI (see README).

    ``task_creation`` is stored as ``YYYY-MM-DD`` while the others are full
    ISO timestamps; we pad date-only values with ``T00:00:00`` so lexical
    sort is chronologically correct.
    """
    cutoff_ts = (datetime.now() - timedelta(days=int(days))).isoformat(timespec="seconds")
    cutoff_day = (date.today() - timedelta(days=int(days))).isoformat()
    q = text(f"""
        SELECT * FROM (
            SELECT
                CASE WHEN LENGTH(task_creation) = 10 THEN task_creation || 'T00:00:00'
                     ELSE task_creation END AS when_ts,
                'created'::text AS kind,
                task            AS what,
                'task'::text    AS source,
                uuid, catagory
            FROM tasks
            WHERE task_creation IS NOT NULL AND task_creation >= :day
            UNION ALL
            SELECT
                CASE WHEN LENGTH(task_creation) = 10 THEN task_creation || 'T00:00:00'
                     ELSE task_creation END,
                'created', task, 'recurring', uuid, catagory
            FROM recurring_tasks
            WHERE task_creation IS NOT NULL AND task_creation >= :day
            UNION ALL
            SELECT completed_time, 'completed', task, 'task', uuid, catagory
            FROM tasks
            WHERE completed = 1 AND completed_time IS NOT NULL
              AND completed_time >= :ts
            UNION ALL
            SELECT completed_time, 'completed', task, 'recurring', uuid, catagory
            FROM recurring_tasks
            WHERE completed = 1 AND completed_time IS NOT NULL
              AND completed_time >= :ts
            UNION ALL
            SELECT logged_at, 'discipline', task, 'discipline',
                   NULL::text AS uuid, catagory
            FROM discipline_completions
            WHERE logged_at IS NOT NULL AND logged_at >= :ts
            UNION ALL
            SELECT created, 'created', follow_up_task, 'follow_up', uuid, catagory
            FROM follow_up_tasks
            WHERE created IS NOT NULL AND created >= :ts
        ) x
        ORDER BY when_ts DESC
        LIMIT {int(limit)}
    """)
    with get_engine().connect() as conn:
        return _rows(conn.execute(q, {"ts": cutoff_ts, "day": cutoff_day}))


def export_backup() -> dict[str, Any]:
    """Return a JSON-serializable snapshot of every luigi_todo table the GUI
    reads. Read-only; used by ``/admin/backup`` to produce a downloadable
    file. All rows are returned — this is a full dump, not a delta.
    """
    tables = (
        "tasks",
        "recurring_tasks",
        "discipline_list",
        "discipline_completions",
        "follow_up_tasks",
    )
    out: dict[str, Any] = {
        "generated_at": now_iso(),
        "schema_version": check_schema_version(),
        "tables": {},
    }
    with get_engine().connect() as conn:
        for t in tables:
            rows = _rows(conn.execute(text(f"SELECT * FROM {t}")))
            out["tables"][t] = rows
    out["counts"] = {t: len(rows) for t, rows in out["tables"].items()}
    return out


# --------------------------------------------------------------------------- #
# Projects / Gantt
# --------------------------------------------------------------------------- #

def list_categories_with_open_tasks(include_recurring: bool = True) -> list[dict[str, Any]]:
    """Distinct ``catagory`` values that own at least one open task.

    Powers the multi-select on ``/projects`` — the page only offers
    categories that would actually have something to plot. ``include_recurring``
    mirrors the page toggle so a category populated only by recurring items
    doesn't appear when recurring is excluded.
    """
    sources = ["""
        SELECT catagory, COUNT(*) AS n FROM tasks
        WHERE completed = 0 AND (status IS NULL OR status != 'Completed')
          AND catagory IS NOT NULL AND catagory != ''
        GROUP BY catagory
    """]
    if include_recurring:
        sources.append("""
            SELECT catagory, COUNT(*) AS n FROM recurring_tasks
            WHERE completed = 0 AND (status IS NULL OR status != 'Completed')
              AND catagory IS NOT NULL AND catagory != ''
            GROUP BY catagory
        """)
    q = text(f"""
        SELECT catagory, SUM(n)::int AS n
        FROM ({' UNION ALL '.join(sources)}) x
        GROUP BY catagory
        ORDER BY catagory ASC
    """)
    with get_engine().connect() as conn:
        return _rows(conn.execute(q))


def list_project_rows(
    categories: list[str] | None,
    include_recurring: bool = True,
) -> list[dict[str, Any]]:
    """Open items (tasks + optionally recurring) restricted to ``categories``.

    Returns [] when ``categories`` is falsy so the /projects page renders
    an empty state instead of dumping every open item. All bar/timeline
    math happens in ``app._build_gantt`` — this stays a pure SQL fetch.
    """
    if not categories:
        return []
    cats = [c for c in categories if c]
    if not cats:
        return []
    fields = """
        uuid, task, priority, status, catagory, task_group, sub_group,
        task_creation, start_time, due_date, estimated_time
    """
    if include_recurring:
        sql = f"""
            SELECT {fields}, 'task' AS source FROM tasks
            WHERE completed = 0 AND (status IS NULL OR status != 'Completed')
              AND catagory IN :cats
            UNION ALL
            SELECT {fields}, 'recurring' AS source FROM recurring_tasks
            WHERE completed = 0 AND (status IS NULL OR status != 'Completed')
              AND catagory IN :cats
            ORDER BY catagory ASC, due_date ASC NULLS LAST, priority DESC
        """
    else:
        sql = f"""
            SELECT {fields}, 'task' AS source FROM tasks
            WHERE completed = 0 AND (status IS NULL OR status != 'Completed')
              AND catagory IN :cats
            ORDER BY catagory ASC, due_date ASC NULLS LAST, priority DESC
        """
    from sqlalchemy import bindparam
    q = text(sql).bindparams(bindparam("cats", expanding=True))
    with get_engine().connect() as conn:
        return _rows(conn.execute(q, {"cats": cats}))


# --------------------------------------------------------------------------- #
# Chat / LLM helpers
# --------------------------------------------------------------------------- #

def find_tasks_by_name(
    query: str,
    include_completed: bool = False,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Case-insensitive substring match over ``tasks`` + ``recurring_tasks``.

    Used by the chat agent to resolve a natural-language task reference to a
    ``uuid`` before performing a mutation. Returns the shape the caller needs
    to disambiguate: uuid, task, status, due_date, source.
    """
    pattern = f"%{query.strip().lower()}%"
    where_completed = "" if include_completed else "AND completed = 0"
    q = text(f"""
        SELECT uuid, task, status, due_date, catagory, 'task' AS source
        FROM tasks
        WHERE LOWER(task) LIKE :p {where_completed}
        UNION ALL
        SELECT uuid, task, status, due_date, catagory, 'recurring' AS source
        FROM recurring_tasks
        WHERE LOWER(task) LIKE :p {where_completed}
        ORDER BY task ASC
        LIMIT {int(limit)}
    """)
    with get_engine().connect() as conn:
        return _rows(conn.execute(q, {"p": pattern}))


def find_discipline_by_name(query: str) -> dict[str, Any] | None:
    """Case-insensitive lookup of an active discipline by name.

    Returns the row if exactly one active discipline matches; otherwise None.
    The chat tool interprets ``None`` as 'ask the user to disambiguate'.
    """
    pattern = f"%{query.strip().lower()}%"
    q = text("""
        SELECT uuid, task, catagory, frequency_per_week, active, current_streak
        FROM discipline_list
        WHERE active = 1 AND LOWER(task) LIKE :p
        ORDER BY task ASC
        LIMIT 5
    """)
    with get_engine().connect() as conn:
        rows = _rows(conn.execute(q, {"p": pattern}))
    if len(rows) == 1:
        return rows[0]
    # Exact match wins even if multiple substring matches exist.
    exact = [r for r in rows if r["task"].lower() == query.strip().lower()]
    return exact[0] if len(exact) == 1 else None


def suggest_task_defaults(name: str, limit: int = 25) -> dict[str, Any]:
    """Look at past tasks with similar names and suggest field values a user
    would most likely want when creating a new task of that kind.

    Strategy: case-insensitive substring match against both ``tasks`` and
    ``recurring_tasks`` (including completed rows — history is the point).
    Rows are ordered ``task_creation DESC`` so the most recent matches come
    first. Suggestions are drawn from a *bounded, most-recent window* per
    field so an ancient outlier can't outvote what the user picked last
    time.

    Returns a dict with ``matches`` (int, how many past rows were considered)
    plus each suggestion under its column name. Fields with no signal are
    omitted so the caller can distinguish "no suggestion" from a real value.
    """
    from collections import Counter
    from statistics import median

    pattern = f"%{name.strip().lower()}%"
    q = text("""
        SELECT priority, catagory, task_group, sub_group,
               relevant_link, estimated_time, task_creation
        FROM (
            SELECT priority, catagory, task_group, sub_group,
                   relevant_link, estimated_time, task_creation
            FROM tasks
            WHERE LOWER(task) LIKE :p
            UNION ALL
            SELECT priority, catagory, task_group, sub_group,
                   relevant_link, estimated_time, task_creation
            FROM recurring_tasks
            WHERE LOWER(task) LIKE :p
        ) s
        ORDER BY task_creation DESC NULLS LAST
        LIMIT :lim
    """)
    with get_engine().connect() as conn:
        rows = _rows(conn.execute(q, {"p": pattern, "lim": int(limit)}))

    out: dict[str, Any] = {"matches": len(rows)}
    if not rows:
        return out

    # Window sizes chosen so recent behavior dominates: category-style fields
    # look back ~10 matches (people don't reshuffle those often), priority
    # looks at just the last ~5 (it drifts more), estimated_time uses ~8.
    def _recent_mode(field: str, window: int) -> Any | None:
        values = [r[field] for r in rows[:window] if r.get(field)]
        if not values:
            return None
        top_val, top_count = Counter(values).most_common(1)[0]
        # Require the mode to appear at least twice OR in the sole row —
        # keeps one-off outliers from polluting suggestions.
        if top_count >= 2 or len(values) == 1:
            return top_val
        return None

    for field in ("catagory", "task_group", "sub_group", "relevant_link"):
        v = _recent_mode(field, window=10)
        if v is not None:
            out[field] = v

    recent_priorities = [
        int(r["priority"]) for r in rows[:5] if r.get("priority") is not None
    ]
    if recent_priorities:
        top_p, top_pc = Counter(recent_priorities).most_common(1)[0]
        if top_pc >= 2 or len(recent_priorities) == 1:
            out["priority"] = top_p

    # Median of the newest ~8 non-null estimates. Median (not mean) resists
    # the occasional 40-hour outlier; capping at 8 keeps ancient one-offs
    # from anchoring today's suggestion.
    recent_est = [
        float(r["estimated_time"])
        for r in rows[:8]
        if r.get("estimated_time") is not None
    ]
    if recent_est:
        out["estimated_time"] = round(float(median(recent_est)), 2)

    return out


_CATEGORICAL_FIELDS: frozenset[str] = frozenset({"catagory", "task_group", "sub_group"})


def find_existing_categorical(field: str, value: str) -> str | None:
    """Return the DB's canonical spelling of ``value`` for the given
    categorical column, or ``None`` if no case-insensitive match exists.

    Used by the chat agent so that saying "add task, category boat" reuses
    an existing ``Boat`` category rather than creating a case-duplicate
    ``boat``. Only whitelisted fields are accepted — the field name is
    interpolated into the SQL, so the allow-list is the security boundary.
    Preference goes to the most-recently-used spelling when multiple
    case-variants exist in history.
    """
    if field not in _CATEGORICAL_FIELDS or not value or not value.strip():
        return None
    q = text(f"""
        SELECT {field} AS v
        FROM (
            SELECT {field}, task_creation FROM tasks
              WHERE {field} IS NOT NULL AND {field} != ''
            UNION ALL
            SELECT {field}, task_creation FROM recurring_tasks
              WHERE {field} IS NOT NULL AND {field} != ''
        ) s
        WHERE LOWER({field}) = LOWER(:v)
        GROUP BY {field}
        ORDER BY MAX(task_creation) DESC NULLS LAST
        LIMIT 1
    """)
    with get_engine().connect() as conn:
        row = conn.execute(q, {"v": value.strip()}).first()
        return row[0] if row else None


