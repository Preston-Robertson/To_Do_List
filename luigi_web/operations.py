"""App-owned task dependencies and in-app reminder notifications."""
from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from . import clock
from .paths import OPERATIONS_DB_PATH

SOURCES = ("task", "recurring")
RULE_KINDS = ("due_soon", "custom")


def db_path() -> Path:
    configured = os.environ.get("LUIGI_WEB_OPERATIONS_DB", "").strip()
    return Path(configured).expanduser().resolve() if configured else OPERATIONS_DB_PATH


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS task_dependencies (
                uuid TEXT PRIMARY KEY,
                dependent_uuid TEXT NOT NULL,
                dependent_source TEXT NOT NULL,
                dependent_label TEXT NOT NULL,
                blocker_uuid TEXT NOT NULL,
                blocker_source TEXT NOT NULL,
                blocker_label TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(dependent_uuid, dependent_source, blocker_uuid, blocker_source)
            );
            CREATE INDEX IF NOT EXISTS idx_dependencies_dependent
                ON task_dependencies(dependent_source, dependent_uuid);
            CREATE TABLE IF NOT EXISTS reminder_rules (
                uuid TEXT PRIMARY KEY,
                task_uuid TEXT NOT NULL,
                task_source TEXT NOT NULL,
                task_label TEXT NOT NULL,
                kind TEXT NOT NULL,
                lead_days INTEGER NOT NULL DEFAULT 1,
                remind_on TEXT,
                message TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reminder_notifications (
                uuid TEXT PRIMARY KEY,
                rule_uuid TEXT,
                task_uuid TEXT NOT NULL,
                task_source TEXT NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                fired_at TEXT NOT NULL,
                dismissed_at TEXT,
                snoozed_until TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_notifications_open
                ON reminder_notifications(dismissed_at, snoozed_until, fired_at DESC);
        """)


def _source(value: Any) -> str:
    source = str(value or "").strip()
    if source not in SOURCES:
        raise ValueError("task source must be task or recurring")
    return source


def _label(value: Any, field: str) -> str:
    label = str(value or "").strip()
    if not label or len(label) > 2000 or "\x00" in label:
        raise ValueError(f"{field} is invalid")
    return label


def _key(source: str, row_uuid: str) -> tuple[str, str]:
    return (_source(source), str(row_uuid or "").strip())


def list_dependencies() -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM task_dependencies ORDER BY dependent_label, blocker_label"
        ).fetchall()
    return [dict(row) for row in rows]


def add_dependency(
    *,
    dependent_uuid: str,
    dependent_source: str,
    dependent_label: str,
    blocker_uuid: str,
    blocker_source: str,
    blocker_label: str,
) -> str:
    init_db()
    dependent = _key(dependent_source, dependent_uuid)
    blocker = _key(blocker_source, blocker_uuid)
    if not dependent[1] or not blocker[1] or dependent == blocker:
        raise ValueError("a task cannot depend on itself")
    # A node may have multiple blockers, so use the complete adjacency list.
    adjacency: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for row in list_dependencies():
        adjacency.setdefault(
            (row["dependent_source"], row["dependent_uuid"]), set()
        ).add((row["blocker_source"], row["blocker_uuid"]))
    stack = [blocker]
    seen: set[tuple[str, str]] = set()
    while stack:
        current = stack.pop()
        if current == dependent:
            raise ValueError("dependency would create a cycle")
        if current in seen:
            continue
        seen.add(current)
        stack.extend(adjacency.get(current, ()))
    row_uuid = str(uuid.uuid4())
    now = clock.local_now().isoformat(timespec="seconds")
    with _connect() as conn:
        try:
            conn.execute("""
                INSERT INTO task_dependencies (
                    uuid, dependent_uuid, dependent_source, dependent_label,
                    blocker_uuid, blocker_source, blocker_label, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (row_uuid, dependent[1], dependent[0],
                  _label(dependent_label, "dependent label"), blocker[1], blocker[0],
                  _label(blocker_label, "blocker label"), now))
        except sqlite3.IntegrityError as exc:
            raise ValueError("dependency already exists") from exc
    return row_uuid


def delete_dependency(row_uuid: str) -> bool:
    init_db()
    with _connect() as conn:
        result = conn.execute(
            "DELETE FROM task_dependencies WHERE uuid = ?", (str(row_uuid),)
        )
    return bool(result.rowcount)


def blockers_for(task_uuid: str, source: str) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("""
            SELECT * FROM task_dependencies
            WHERE dependent_uuid = ? AND dependent_source = ?
            ORDER BY blocker_label
        """, (str(task_uuid), _source(source))).fetchall()
    return [dict(row) for row in rows]


def assert_unblocked(
    task_uuid: str,
    source: str,
    resolver: Callable[[str, str], dict[str, Any] | None],
) -> None:
    pending: list[str] = []
    for edge in blockers_for(task_uuid, source):
        blocker = resolver(edge["blocker_source"], edge["blocker_uuid"])
        if not blocker or not (
            int(blocker.get("completed") or 0) == 1
            or blocker.get("status") == "Completed"
        ):
            pending.append(edge["blocker_label"])
    if pending:
        raise ValueError("Blocked by: " + ", ".join(pending))


def create_reminder_rule(data: dict[str, Any]) -> str:
    init_db()
    source = _source(data.get("task_source"))
    task_uuid = str(data.get("task_uuid") or "").strip()
    if not task_uuid:
        raise ValueError("reminder task is required")
    kind = str(data.get("kind") or "due_soon")
    if kind not in RULE_KINDS:
        raise ValueError("invalid reminder kind")
    try:
        lead_days = min(max(int(data.get("lead_days") or 1), 0), 365)
    except (TypeError, ValueError) as exc:
        raise ValueError("lead days must be an integer") from exc
    remind_on = str(data.get("remind_on") or "").strip() or None
    if remind_on:
        try:
            remind_on = date.fromisoformat(remind_on).isoformat()
        except ValueError as exc:
            raise ValueError("reminder date must be YYYY-MM-DD") from exc
    if kind == "custom" and not remind_on:
        raise ValueError("custom reminder date is required")
    message = str(data.get("message") or "").strip()[:1000] or None
    row_uuid = str(uuid.uuid4())
    now = clock.local_now().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute("""
            INSERT INTO reminder_rules (
                uuid, task_uuid, task_source, task_label, kind, lead_days,
                remind_on, message, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """, (row_uuid, task_uuid, source, _label(data.get("task_label"), "task label"),
              kind, lead_days, remind_on, message, now, now))
    return row_uuid


def list_reminder_rules() -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reminder_rules ORDER BY task_label, created_at"
        ).fetchall()
    return [dict(row) for row in rows]


def delete_reminder_rule(row_uuid: str) -> bool:
    init_db()
    with _connect() as conn:
        conn.execute("""
            UPDATE reminder_notifications SET dismissed_at = ?
            WHERE rule_uuid = ? AND dismissed_at IS NULL
        """, (clock.local_now().isoformat(timespec="seconds"), str(row_uuid)))
        result = conn.execute(
            "DELETE FROM reminder_rules WHERE uuid = ?", (str(row_uuid),)
        )
    return bool(result.rowcount)


def _notification(
    conn: sqlite3.Connection,
    *,
    rule_uuid: str | None,
    task_uuid: str,
    task_source: str,
    fingerprint: str,
    level: str,
    message: str,
) -> None:
    conn.execute("""
        INSERT INTO reminder_notifications (
            uuid, rule_uuid, task_uuid, task_source, fingerprint, level,
            message, fired_at, dismissed_at, snoozed_until
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
        ON CONFLICT(fingerprint) DO NOTHING
    """, (str(uuid.uuid4()), rule_uuid, task_uuid, task_source, fingerprint,
          level, message, clock.local_now().isoformat(timespec="seconds")))


def evaluate_notifications(rows: list[dict[str, Any]], *, today: date | None = None) -> int:
    init_db()
    today = today or clock.local_today()
    task_map = {
        (str(row.get("source") or "task"), str(row.get("uuid") or "")): row
        for row in rows
    }
    rules = list_reminder_rules()
    open_keys = {
        key for key, row in task_map.items()
        if not (int(row.get("completed") or 0) == 1 or row.get("status") == "Completed")
    }
    active_fingerprints: set[str] = set()
    with _connect() as conn:
        for key, row in task_map.items():
            source, task_uuid = key
            if key not in open_keys:
                conn.execute("""
                    UPDATE reminder_notifications SET dismissed_at = ?
                    WHERE task_source = ? AND task_uuid = ? AND dismissed_at IS NULL
                """, (clock.local_now().isoformat(timespec="seconds"), source, task_uuid))
                continue
            label = str(row.get("task") or "Task")
            due = None
            if row.get("due_date"):
                try:
                    due = date.fromisoformat(str(row["due_date"])[:10])
                except ValueError:
                    due = None
            if due and due < today:
                fingerprint = f"overdue:{source}:{task_uuid}:{due.isoformat()}"
                active_fingerprints.add(fingerprint)
                _notification(
                    conn, rule_uuid=None, task_uuid=task_uuid, task_source=source,
                    fingerprint=fingerprint,
                    level="danger", message=f"{label} is overdue since {due.isoformat()}",
                )
            elif due and due <= today + timedelta(days=1):
                fingerprint = f"due-soon:{source}:{task_uuid}:{due.isoformat()}"
                active_fingerprints.add(fingerprint)
                _notification(
                    conn, rule_uuid=None, task_uuid=task_uuid, task_source=source,
                    fingerprint=fingerprint,
                    level="warning", message=f"{label} is due {due.isoformat()}",
                )
            blockers = blockers_for(task_uuid, source)
            pending = []
            for edge in blockers:
                blocker = task_map.get((edge["blocker_source"], edge["blocker_uuid"]))
                if not blocker or not (
                    int(blocker.get("completed") or 0) == 1
                    or blocker.get("status") == "Completed"
                ):
                    pending.append(edge["blocker_label"])
            if pending:
                fingerprint = f"blocked:{source}:{task_uuid}:{'|'.join(sorted(pending))}"
                active_fingerprints.add(fingerprint)
                _notification(
                    conn, rule_uuid=None, task_uuid=task_uuid, task_source=source,
                    fingerprint=fingerprint,
                    level="info", message=f"{label} is blocked by {', '.join(pending)}",
                )

        for rule in rules:
            if not int(rule["active"] or 0):
                continue
            key = (rule["task_source"], rule["task_uuid"])
            row = task_map.get(key)
            if not row or key not in open_keys:
                continue
            due = None
            if row.get("due_date"):
                try:
                    due = date.fromisoformat(str(row["due_date"])[:10])
                except ValueError:
                    due = None
            fires = (
                rule["kind"] == "custom" and rule["remind_on"] <= today.isoformat()
            ) or (
                rule["kind"] == "due_soon" and due is not None
                and due <= today + timedelta(days=int(rule["lead_days"] or 0))
            )
            if fires:
                trigger = rule["remind_on"] if rule["kind"] == "custom" else due.isoformat()
                fingerprint = f"rule:{rule['uuid']}:{trigger}"
                active_fingerprints.add(fingerprint)
                _notification(
                    conn, rule_uuid=rule["uuid"], task_uuid=rule["task_uuid"],
                    task_source=rule["task_source"],
                    fingerprint=fingerprint, level="warning",
                    message=rule["message"] or f"Reminder: {rule['task_label']}",
                )
        now = clock.local_now().isoformat(timespec="seconds")
        if active_fingerprints:
            placeholders = ",".join("?" for _ in active_fingerprints)
            conn.execute(f"""
                UPDATE reminder_notifications SET dismissed_at = ?
                WHERE dismissed_at IS NULL AND fingerprint NOT IN ({placeholders})
            """, (now, *sorted(active_fingerprints)))
        else:
            conn.execute("""
                UPDATE reminder_notifications SET dismissed_at = ?
                WHERE dismissed_at IS NULL
            """, (now,))
        count = conn.execute("""
            SELECT COUNT(*) FROM reminder_notifications
            WHERE dismissed_at IS NULL
              AND (snoozed_until IS NULL OR snoozed_until <= ?)
        """, (now,)).fetchone()[0]
    return int(count)


def list_notifications() -> list[dict[str, Any]]:
    init_db()
    now = clock.local_now().isoformat(timespec="seconds")
    with _connect() as conn:
        rows = conn.execute("""
            SELECT * FROM reminder_notifications
            WHERE dismissed_at IS NULL
              AND (snoozed_until IS NULL OR snoozed_until <= ?)
            ORDER BY CASE level WHEN 'danger' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                     fired_at DESC
        """, (now,)).fetchall()
    return [dict(row) for row in rows]


def dismiss_notification(row_uuid: str) -> bool:
    init_db()
    with _connect() as conn:
        result = conn.execute("""
            UPDATE reminder_notifications SET dismissed_at = ? WHERE uuid = ?
        """, (clock.local_now().isoformat(timespec="seconds"), str(row_uuid)))
    return bool(result.rowcount)


def snooze_notification(row_uuid: str, days: int) -> bool:
    init_db()
    until = (clock.local_now() + timedelta(days=min(max(int(days), 1), 30))).isoformat(
        timespec="seconds"
    )
    with _connect() as conn:
        result = conn.execute("""
            UPDATE reminder_notifications
            SET snoozed_until = ?, dismissed_at = NULL WHERE uuid = ?
        """, (until, str(row_uuid)))
    return bool(result.rowcount)


def storage_health() -> str:
    init_db()
    with _connect() as conn:
        conn.execute("SELECT 1 FROM task_dependencies LIMIT 1").fetchone()
        conn.execute("SELECT 1 FROM reminder_rules LIMIT 1").fetchone()
        conn.execute("SELECT 1 FROM reminder_notifications LIMIT 1").fetchone()
    return "dependencies and reminders database ready"