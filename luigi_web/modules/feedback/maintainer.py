"""Durable, privacy-screened queue for autonomous maintenance jobs."""
from __future__ import annotations

import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from ... import clock
from . import repository as feedback

POLICY_VERSION = 1
JOB_STATUSES = (
    "Queued", "Running", "Needs attention", "Draft PR", "No change", "Failed",
    "Cancelled",
)

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_ -]?key)\s*[:=]\s*[^\s,;]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)[?#][^\s]*", re.IGNORECASE)
_PERSONAL_NUMBER_RE = re.compile(r"(?<!\d)(?:\+?\d[\s().-]*){9,}(?!\d)")


def db_path() -> Path:
    configured = os.environ.get("LUIGI_WEB_MAINTAINER_DB", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return feedback.db_path().with_name("maintainer.db")


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
            CREATE TABLE IF NOT EXISTS maintainer_jobs (
                uuid TEXT PRIMARY KEY,
                feedback_uuid TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                request_text TEXT NOT NULL,
                page_path TEXT,
                acceptance_criteria TEXT NOT NULL,
                policy_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                branch_name TEXT,
                base_commit TEXT,
                head_commit TEXT,
                pr_url TEXT,
                result_summary TEXT,
                attention_question TEXT,
                error_detail TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_maintainer_jobs_status_created "
            "ON maintainer_jobs(status, created_at)"
        )
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(maintainer_jobs)")
        }
        migrations = {
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "branch_name": "TEXT",
            "base_commit": "TEXT",
            "head_commit": "TEXT",
            "pr_url": "TEXT",
            "result_summary": "TEXT",
            "attention_question": "TEXT",
            "error_detail": "TEXT",
            "started_at": "TEXT",
            "finished_at": "TEXT",
        }
        for column, declaration in migrations.items():
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE maintainer_jobs ADD COLUMN {column} {declaration}"
                )


def _screen_text(value: Any, *, field: str, limit: int = 5000) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise ValueError(f"{field} must be 1-{limit} characters")
    return _redact_text(text)


def _redact_text(text: str) -> str:
    text = "".join(char for char in text if char in "\n\t" or ord(char) >= 32)
    text = _EMAIL_RE.sub("[redacted email]", text)
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _URL_QUERY_RE.sub(r"\1?[redacted query]", text)
    return _PERSONAL_NUMBER_RE.sub("[redacted numeric identifier]", text)


def sanitize_output(value: Any, *, limit: int = 2000) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _redact_text(text)[:limit]


def enqueue_feedback(item: dict[str, Any], acceptance_criteria: Any) -> str:
    """Copy one Feedback item into the worker-visible sanitized queue."""
    init_db()
    feedback_uuid = str(item.get("uuid") or "").strip()
    category = str(item.get("category") or "").strip()
    page_path = str(item.get("page_path") or "").strip()
    if not feedback_uuid:
        raise ValueError("feedback UUID is required")
    if category not in feedback.CATEGORIES:
        raise ValueError("invalid feedback category")
    if page_path and (not page_path.startswith("/") or "?" in page_path or "#" in page_path):
        raise ValueError("feedback page path must be a local path without query data")

    row_uuid = str(uuid.uuid4())
    now = clock.local_now().isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            conn.execute("""
                INSERT INTO maintainer_jobs (
                    uuid, feedback_uuid, category, request_text, page_path,
                    acceptance_criteria, policy_version, status, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'Queued', ?, ?)
            """, (
                row_uuid, feedback_uuid, category,
                _screen_text(item.get("message"), field="feedback message"),
                page_path or None,
                _screen_text(acceptance_criteria, field="acceptance criteria"),
                POLICY_VERSION, now, now,
            ))
    except sqlite3.IntegrityError as exc:
        raise ValueError("feedback item is already queued") from exc
    return row_uuid


def job_for_feedback(feedback_uuid: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM maintainer_jobs WHERE feedback_uuid = ?",
            (feedback_uuid,),
        ).fetchone()
    return dict(row) if row else None


def list_jobs(*, status: str = "") -> list[dict[str, Any]]:
    init_db()
    values: list[Any] = []
    clause = ""
    if status:
        if status not in JOB_STATUSES:
            raise ValueError("invalid maintainer job status")
        clause = "WHERE status = ?"
        values.append(status)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM maintainer_jobs {clause} ORDER BY created_at, rowid",
            values,
        ).fetchall()
    return [dict(row) for row in rows]


def jobs_by_feedback() -> dict[str, dict[str, Any]]:
    return {job["feedback_uuid"]: job for job in list_jobs()}


def get_job(row_uuid: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM maintainer_jobs WHERE uuid = ?", (row_uuid,),
        ).fetchone()
    return dict(row) if row else None


def recover_stale_jobs(*, hours: int = 6) -> int:
    init_db()
    cutoff = (clock.local_now() - timedelta(hours=max(1, hours))).isoformat(
        timespec="seconds"
    )
    now = clock.local_now().isoformat(timespec="seconds")
    with _connect() as conn:
        result = conn.execute("""
            UPDATE maintainer_jobs
            SET status = 'Failed', error_detail = 'worker run expired',
                finished_at = ?, updated_at = ?
            WHERE status = 'Running' AND started_at < ?
        """, (now, now, cutoff))
    return int(result.rowcount)


def claim_next_job() -> dict[str, Any] | None:
    """Atomically claim at most one queued job for a worker invocation."""
    init_db()
    now = clock.local_now().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT uuid FROM maintainer_jobs
            WHERE status = 'Queued'
            ORDER BY created_at, rowid
            LIMIT 1
        """).fetchone()
        if not row:
            return None
        conn.execute("""
            UPDATE maintainer_jobs
            SET status = 'Running', attempt_count = attempt_count + 1,
                started_at = ?, finished_at = NULL, error_detail = NULL,
                updated_at = ?
            WHERE uuid = ? AND status = 'Queued'
        """, (now, now, row["uuid"]))
        claimed = conn.execute(
            "SELECT * FROM maintainer_jobs WHERE uuid = ?", (row["uuid"],),
        ).fetchone()
    return dict(claimed) if claimed else None


def set_run_context(row_uuid: str, *, branch_name: str, base_commit: str) -> None:
    now = clock.local_now().isoformat(timespec="seconds")
    with _connect() as conn:
        result = conn.execute("""
            UPDATE maintainer_jobs
            SET branch_name = ?, base_commit = ?, updated_at = ?
            WHERE uuid = ? AND status = 'Running'
        """, (branch_name[:200], base_commit[:100], now, row_uuid))
    if not result.rowcount:
        raise ValueError("maintainer job is not running")


def finish_job(
    row_uuid: str,
    *,
    status: str,
    summary: str = "",
    question: str = "",
    error: str = "",
    head_commit: str = "",
    pr_url: str = "",
) -> None:
    if status not in JOB_STATUSES or status in {"Queued", "Running"}:
        raise ValueError("invalid terminal maintainer job status")
    if pr_url:
        parsed = urlsplit(pr_url)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
        ):
            raise ValueError("maintainer pull request URL must be credential-free HTTPS")
    now = clock.local_now().isoformat(timespec="seconds")
    with _connect() as conn:
        result = conn.execute("""
            UPDATE maintainer_jobs
            SET status = ?, result_summary = ?, attention_question = ?,
                error_detail = ?, head_commit = ?, pr_url = ?,
                finished_at = ?, updated_at = ?
            WHERE uuid = ? AND status = 'Running'
        """, (
            status, summary.strip()[:2000], question.strip()[:2000],
            error.strip()[:2000], head_commit.strip()[:100], pr_url.strip()[:1000],
            now, now, row_uuid,
        ))
    if not result.rowcount:
        raise ValueError("maintainer job is not running")