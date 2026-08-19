"""Adapter for the LuigiBot-owned shared ``task_events`` contract.

This module never creates or alters the table. LuigiBot owns shared-schema
migrations; Luigi Web only detects the negotiated contract and writes events
inside the caller's transaction when it is available.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.exc import NoSuchTableError

from . import clock

TABLE_NAME = "task_events"
COMPLETED = "completed"
COMPLETION_REVERSED = "completion_reversed"
DEFAULT_DAY_CUTOFF = "04:00"

REQUIRED_COLUMNS = frozenset({
    "event_uuid",
    "operation_uuid",
    "event_type",
    "source_task_uuid",
    "source_table",
    "task_snapshot",
    "catagory_snapshot",
    "occurred_at",
    "effective_date",
    "due_date_snapshot",
    "actor_source",
    "related_event_uuid",
    "details_json",
})


@dataclass(frozen=True)
class Capability:
    available: bool
    reason: str


def parse_day_cutoff(value: str | None = None) -> clock_time:
    raw = (value if value is not None else os.environ.get(
        "LUIGI_WEB_DAY_CUTOFF", DEFAULT_DAY_CUTOFF
    )).strip()
    try:
        parsed = datetime.strptime(raw, "%H:%M").time()
    except (TypeError, ValueError) as exc:
        raise ValueError("LUIGI_WEB_DAY_CUTOFF must be HH:MM (00:00-23:59)") from exc
    return parsed


def effective_date_for(
    occurred_at: datetime,
    *,
    cutoff: str | None = None,
    override: str | date | None = None,
) -> str:
    """Calculate the server-local effective date for one completion."""
    if occurred_at.tzinfo is None:
        occurred_at = clock.parse_timestamp_local(occurred_at.isoformat())
    else:
        occurred_at = occurred_at.astimezone(clock.user_timezone())
    if override not in (None, ""):
        try:
            selected = (
                override if isinstance(override, date)
                else date.fromisoformat(str(override))
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("effective completion date must be YYYY-MM-DD") from exc
        return selected.isoformat()
    effective = occurred_at.date()
    if occurred_at.time() < parse_day_cutoff(cutoff):
        effective -= timedelta(days=1)
    return effective.isoformat()


def effective_date_for_iso(
    occurred_at: str,
    *,
    cutoff: str | None = None,
    override: str | date | None = None,
) -> str:
    parsed = clock.parse_timestamp_local(occurred_at)
    return effective_date_for(parsed, cutoff=cutoff, override=override)


def server_time_policy() -> str:
    cutoff = parse_day_cutoff().strftime("%H:%M")
    return f"before {cutoff} counts toward the previous day ({clock.timezone_name()})"


def capability(bind: Any) -> Capability:
    """Inspect the shared table without mutating schema or data."""
    try:
        present = {
            str(column["name"])
            for column in inspect(bind).get_columns(TABLE_NAME)
        }
    except NoSuchTableError:
        return Capability(False, "task_events table is not installed")
    except Exception as exc:  # noqa: BLE001 - surfaced by Admin health
        return Capability(
            False,
            f"could not inspect task_events ({type(exc).__name__})",
        )
    missing = sorted(REQUIRED_COLUMNS - present)
    if missing:
        return Capability(
            False,
            f"task_events is missing columns: {', '.join(missing)}",
        )
    return Capability(True, "task_events contract available")


def append_event(
    conn: Any,
    *,
    event_type: str,
    source_task_uuid: str,
    source_table: str,
    task_snapshot: str,
    occurred_at: str,
    actor_source: str,
    operation_uuid: str | None = None,
    effective_date: str | None = None,
    due_date_snapshot: str | None = None,
    catagory_snapshot: str | None = None,
    related_event_uuid: str | None = None,
    details: dict[str, Any] | None = None,
) -> str | None:
    """Append one retry-safe event, returning its durable UUID.

    ``None`` means the shared contract is not installed. SQL/permission errors
    propagate so the caller's task mutation cannot commit without its event
    once the shared table exists.
    """
    if not capability(conn).available:
        return None
    operation_uuid = operation_uuid or str(uuid.uuid4())
    event_uuid = str(uuid.uuid4())
    payload = {
        "event_uuid": event_uuid,
        "operation_uuid": operation_uuid,
        "event_type": event_type,
        "source_task_uuid": source_task_uuid,
        "source_table": source_table,
        "task_snapshot": task_snapshot,
        "catagory_snapshot": catagory_snapshot,
        "occurred_at": occurred_at,
        "effective_date": effective_date,
        "due_date_snapshot": due_date_snapshot,
        "actor_source": actor_source,
        "related_event_uuid": related_event_uuid,
        "details_json": json.dumps(details or {}, sort_keys=True),
    }
    conn.execute(text("""
        INSERT INTO task_events (
            event_uuid, operation_uuid, event_type, source_task_uuid,
            source_table, task_snapshot, catagory_snapshot, occurred_at,
            effective_date, due_date_snapshot, actor_source,
            related_event_uuid, details_json
        )
        VALUES (
            :event_uuid, :operation_uuid, :event_type, :source_task_uuid,
            :source_table, :task_snapshot, :catagory_snapshot, :occurred_at,
            :effective_date, :due_date_snapshot, :actor_source,
            :related_event_uuid, :details_json
        )
        ON CONFLICT (operation_uuid) DO NOTHING
    """), payload)
    row = conn.execute(
        text("SELECT event_uuid FROM task_events WHERE operation_uuid = :operation_uuid"),
        {"operation_uuid": operation_uuid},
    ).first()
    if row is None:
        raise RuntimeError("task event write was not visible in its transaction")
    return str(row[0])


def append_reversal(
    conn: Any,
    *,
    completion_event_uuid: str,
    source_task_uuid: str,
    source_table: str,
    task_snapshot: str,
    occurred_at: str,
    actor_source: str,
    operation_uuid: str | None = None,
    catagory_snapshot: str | None = None,
) -> str | None:
    return append_event(
        conn,
        event_type=COMPLETION_REVERSED,
        source_task_uuid=source_task_uuid,
        source_table=source_table,
        task_snapshot=task_snapshot,
        catagory_snapshot=catagory_snapshot,
        occurred_at=occurred_at,
        actor_source=actor_source,
        operation_uuid=operation_uuid,
        related_event_uuid=completion_event_uuid,
    )


def latest_active_completion(
    conn: Any,
    *,
    source_task_uuid: str,
    source_table: str,
) -> str | None:
    """Return the newest completion that has not been reversed."""
    if not capability(conn).available:
        return None
    row = conn.execute(text("""
        SELECT completed.event_uuid
        FROM task_events AS completed
        WHERE completed.event_type = :completed_type
          AND completed.source_task_uuid = :source_task_uuid
          AND completed.source_table = :source_table
          AND NOT EXISTS (
              SELECT 1
              FROM task_events AS reversed
              WHERE reversed.event_type = :reversed_type
                AND reversed.related_event_uuid = completed.event_uuid
          )
        ORDER BY completed.occurred_at DESC, completed.event_uuid DESC
        LIMIT 1
    """), {
        "completed_type": COMPLETED,
        "reversed_type": COMPLETION_REVERSED,
        "source_task_uuid": source_task_uuid,
        "source_table": source_table,
    }).first()
    return str(row[0]) if row is not None else None


def latest_active_completion_effective_date(
    conn: Any,
    *,
    source_task_uuid: str,
    source_table: str,
) -> str | None:
    """Return the effective date for the newest unreversed completion."""
    if not capability(conn).available:
        return None
    row = conn.execute(text("""
        SELECT completed.effective_date
        FROM task_events AS completed
        WHERE completed.event_type = :completed_type
          AND completed.source_task_uuid = :source_task_uuid
          AND completed.source_table = :source_table
          AND NOT EXISTS (
              SELECT 1 FROM task_events AS reversed
              WHERE reversed.event_type = :reversed_type
                AND reversed.related_event_uuid = completed.event_uuid
          )
        ORDER BY completed.occurred_at DESC, completed.event_uuid DESC
        LIMIT 1
    """), {
        "completed_type": COMPLETED,
        "reversed_type": COMPLETION_REVERSED,
        "source_task_uuid": source_task_uuid,
        "source_table": source_table,
    }).first()
    return str(row[0]) if row is not None and row[0] else None


def completion_reversed_by(
    conn: Any,
    reversal_event_uuid: str,
) -> dict[str, Any] | None:
    """Return the completion snapshot referenced by one reversal event."""
    if not capability(conn).available:
        return None
    row = conn.execute(text("""
        SELECT original.event_uuid, original.effective_date,
               original.due_date_snapshot, original.occurred_at
        FROM task_events AS reversal
        JOIN task_events AS original
          ON original.event_uuid = reversal.related_event_uuid
        WHERE reversal.event_uuid = :event_uuid
          AND reversal.event_type = :reversed_type
          AND original.event_type = :completed_type
    """), {
        "event_uuid": reversal_event_uuid,
        "reversed_type": COMPLETION_REVERSED,
        "completed_type": COMPLETED,
    }).mappings().first()
    return dict(row) if row is not None else None


def list_active_completions(
    conn: Any,
    *,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """Return active completion snapshots in an inclusive effective-date range."""
    if not capability(conn).available:
        return []
    rows = conn.execute(text("""
        SELECT
            completed.event_uuid,
            completed.source_task_uuid,
            completed.source_table,
            completed.task_snapshot,
            completed.catagory_snapshot,
            completed.occurred_at,
            completed.effective_date,
            completed.due_date_snapshot,
            completed.actor_source,
            CASE
                WHEN completed.source_table = 'tasks' THEN EXISTS (
                    SELECT 1 FROM tasks WHERE uuid = completed.source_task_uuid
                )
                WHEN completed.source_table = 'recurring_tasks' THEN EXISTS (
                    SELECT 1 FROM recurring_tasks WHERE uuid = completed.source_task_uuid
                )
                ELSE 0
            END AS source_exists
        FROM task_events AS completed
        WHERE completed.event_type = :completed_type
          AND completed.effective_date BETWEEN :start_date AND :end_date
          AND NOT EXISTS (
              SELECT 1
              FROM task_events AS reversed
              WHERE reversed.event_type = :reversed_type
                AND reversed.related_event_uuid = completed.event_uuid
          )
        ORDER BY completed.effective_date, completed.occurred_at,
                 completed.task_snapshot
    """), {
        "completed_type": COMPLETED,
        "reversed_type": COMPLETION_REVERSED,
        "start_date": start_date,
        "end_date": end_date,
    }).mappings().all()
    return [dict(row) for row in rows]