"""Local-only feedback inbox repository."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .paths import FEEDBACK_DB_PATH

CATEGORIES = ("Bug", "Idea", "UX", "Other")
STATUSES = ("New", "Reviewed", "Planned", "Resolved", "Archived")


def db_path() -> Path:
    configured = os.environ.get("LUIGI_WEB_FEEDBACK_DB", "").strip()
    return Path(configured).expanduser().resolve() if configured else FEEDBACK_DB_PATH


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
            CREATE TABLE IF NOT EXISTS feedback_items (
                uuid TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                message TEXT NOT NULL,
                page_path TEXT,
                status TEXT NOT NULL,
                tags TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_status_created "
            "ON feedback_items(status, created_at DESC)"
        )


def _clean_category(value: Any) -> str:
    category = str(value or "Other").strip()
    if category not in CATEGORIES:
        raise ValueError("invalid feedback category")
    return category


def _clean_status(value: Any) -> str:
    status = str(value or "New").strip()
    if status not in STATUSES:
        raise ValueError("invalid feedback status")
    return status


def create_item(data: dict[str, Any]) -> str:
    init_db()
    message = str(data.get("message") or "").strip()
    if not message or len(message) > 5000:
        raise ValueError("feedback message must be 1-5000 characters")
    page_path = str(data.get("page_path") or "").strip()
    if page_path and (not page_path.startswith("/") or "?" in page_path or "#" in page_path):
        raise ValueError("feedback page path must be a local path without query data")
    row_uuid = str(uuid.uuid4())
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute("""
            INSERT INTO feedback_items (
                uuid, category, message, page_path, status, tags, notes,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'New', '', '', ?, ?)
        """, (row_uuid, _clean_category(data.get("category")), message,
              page_path or None, now, now))
    return row_uuid


def list_items(*, status: str = "", query: str = "") -> list[dict[str, Any]]:
    where: list[str] = []
    values: list[Any] = []
    if status:
        where.append("status = ?")
        values.append(_clean_status(status))
    if query.strip():
        where.append("LOWER(message || ' ' || COALESCE(tags, '') || ' ' || COALESCE(notes, '')) LIKE ?")
        values.append(f"%{query.strip().lower()}%")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM feedback_items {clause} ORDER BY created_at DESC",
            values,
        ).fetchall()
    return [dict(row) for row in rows]


def update_item(row_uuid: str, data: dict[str, Any]) -> bool:
    init_db()
    status = _clean_status(data.get("status"))
    tags = str(data.get("tags") or "").strip()[:500]
    notes = str(data.get("notes") or "").strip()[:5000]
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        result = conn.execute("""
            UPDATE feedback_items
            SET status = ?, tags = ?, notes = ?, updated_at = ?
            WHERE uuid = ?
        """, (status, tags, notes, now, row_uuid))
    return bool(result.rowcount)


def delete_item(row_uuid: str) -> bool:
    init_db()
    with _connect() as conn:
        result = conn.execute("DELETE FROM feedback_items WHERE uuid = ?", (row_uuid,))
    return bool(result.rowcount)


def export_payload() -> dict[str, Any]:
    return {
        "version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "items": list_items(),
    }


def export_markdown() -> str:
    lines = ["# Luigi Web Feedback Export", ""]
    for item in list_items():
        lines.extend([
            f"## {item['category']} · {item['status']}",
            "",
            item["message"],
            "",
            f"- Created: {item['created_at']}",
            f"- Page: {item['page_path'] or 'Not recorded'}",
            f"- Tags: {item['tags'] or 'None'}",
            f"- Notes: {item['notes'] or 'None'}",
            "",
        ])
    return "\n".join(lines)


def storage_health() -> str:
    init_db()
    with _connect() as conn:
        conn.execute("SELECT 1 FROM feedback_items LIMIT 1").fetchone()
    return "local feedback database ready"