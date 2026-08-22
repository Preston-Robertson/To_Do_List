"""Guided Daily/Weekly Review aggregation and local persistence."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterator

from . import clock, db
from .paths import REVIEW_DB_PATH

SCOPES = ("daily", "weekly")
DAILY_STEPS = (
    "triage_overdue", "check_upcoming", "protect_streaks", "choose_focus",
)
WEEKLY_STEPS = (
    "review_completions", "review_carryover", "plan_next_week", "review_feedback",
)


def db_path() -> Path:
    configured = os.environ.get("LUIGI_WEB_REVIEW_DB", "").strip()
    return Path(configured).expanduser().resolve() if configured else REVIEW_DB_PATH


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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_sessions (
                scope TEXT NOT NULL,
                review_date TEXT NOT NULL,
                completed_steps TEXT NOT NULL,
                notes TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope, review_date)
            )
        """)


def _scope(value: str) -> str:
    scope = str(value or "daily").strip().lower()
    if scope not in SCOPES:
        raise ValueError("review scope must be daily or weekly")
    return scope


def review_date(scope: str, anchor: date | None = None) -> date:
    scope = _scope(scope)
    anchor = anchor or clock.local_today()
    return anchor if scope == "daily" else anchor - timedelta(days=1)


def get_session(scope: str, anchor: date | None = None) -> dict[str, Any]:
    init_db()
    scope = _scope(scope)
    day = review_date(scope, anchor).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM review_sessions WHERE scope = ? AND review_date = ?",
            (scope, day),
        ).fetchone()
    if row is None:
        return {"scope": scope, "review_date": day, "completed_steps": [], "notes": ""}
    result = dict(row)
    try:
        result["completed_steps"] = json.loads(result["completed_steps"])
    except (TypeError, json.JSONDecodeError):
        result["completed_steps"] = []
    return result


def save_session(
    scope: str,
    *,
    completed_steps: list[str],
    notes: str,
    anchor: date | None = None,
) -> dict[str, Any]:
    init_db()
    scope = _scope(scope)
    allowed = set(DAILY_STEPS if scope == "daily" else WEEKLY_STEPS)
    steps = sorted({str(step) for step in completed_steps if str(step) in allowed})
    notes = str(notes or "").strip()
    if len(notes) > 10000:
        raise ValueError("review notes must be 10000 characters or fewer")
    day = review_date(scope, anchor).isoformat()
    now = clock.local_now().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute("""
            INSERT INTO review_sessions (
                scope, review_date, completed_steps, notes, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope, review_date) DO UPDATE SET
                completed_steps = excluded.completed_steps,
                notes = excluded.notes,
                updated_at = excluded.updated_at
        """, (scope, day, json.dumps(steps), notes, now))
    return get_session(scope, anchor)


def build(scope: str, anchor: date | None = None) -> dict[str, Any]:
    scope = _scope(scope)
    anchor = anchor or clock.local_today()
    common = {
        "scope": scope,
        "anchor": anchor.isoformat(),
        "session": get_session(scope, anchor),
        "overdue": db.list_overdue_tasks(limit=50),
        "at_risk": db.list_disciplines_at_risk(),
        "recent_completions": db.list_recent_completions(limit=25),
    }
    if scope == "daily":
        common.update({
            "steps": DAILY_STEPS,
            "upcoming": db.list_upcoming_tasks(days=1, limit=50),
            "pending_disciplines": db.list_disciplines_pending_today(),
            "weekly": None,
        })
    else:
        common.update({
            "steps": WEEKLY_STEPS,
            "upcoming": db.list_upcoming_tasks(days=7, limit=50),
            "pending_disciplines": [],
            "weekly": db.weekly_review(anchor),
        })
    return common