"""History-preserving recurring occurrence construction."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Generator
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

TABLE_NAME = "luigi_web_recurring_occurrences"
STORAGE_MESSAGE = "Recurring occurrence storage is unavailable"
HISTORY_MESSAGE = "This occurrence has a successor and its history cannot be changed."
METADATA_FIELDS = (
    "project", "archived", "recurring_days", "recurring_month_ordinal",
    "recurring_month_weekday",
)


class OccurrenceStorageError(RuntimeError):
    """Occurrence storage is unavailable; no generation may be reported."""


def scheduler_owner() -> str:
    owner = os.environ.get("LUIGI_WEB_RECURRENCE_OWNER", "external")
    if owner not in {"external", "web"}:
        raise ValueError("Invalid recurrence scheduler owner")
    return owner


def scheduler_state() -> dict[str, Any]:
    """Describe policy without connecting to storage or probing another process."""
    owner = scheduler_owner()
    return {
        "owner": owner,
        "enabled": owner == "web",
        "message": (
            "Web recurrence requires the LuigiBot legacy reset scheduler to be disabled."
            if owner == "web" else "Web recurrence generation is disabled."
        ),
    }


@contextmanager
def transaction(engine: Engine) -> Generator[Connection, None, None]:
    """Serialize SQLite writers; PostgreSQL callers additionally lock source rows."""
    with engine.connect() as conn:
        with conn.begin():
            if conn.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            yield conn


def ensure_storage(conn: Connection) -> None:
    """Create only the web-owned ledger, within the generation transaction."""
    if scheduler_owner() != "web":
        raise OccurrenceStorageError(STORAGE_MESSAGE)
    if conn.dialect.name == "postgresql" and not inspect(conn).has_table(TABLE_NAME):
        conn.execute(text("SELECT pg_advisory_xact_lock(764912830125)"))
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            parent_uuid TEXT PRIMARY KEY,
            child_uuid TEXT NOT NULL UNIQUE,
            series_uuid TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            due_date TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            metadata_json TEXT
        )
    """))


def lineage(conn: Connection, row_uuids: list[str]) -> dict[str, dict[str, Any]] | None:
    """Read existing links in bulk without DDL, even after ownership changes."""
    try:
        if not inspect(conn).has_table(TABLE_NAME):
            return None
        links = {
            row_uuid: {
                "annotations": {
                    "_recurrence_parent_uuid": None,
                    "_recurrence_series_uuid": row_uuid,
                    "_recurrence_next_uuid": None,
                    "_recurrence_generated": False,
                },
                "metadata": {},
            }
            for row_uuid in row_uuids
        }
        statement = text(f"""
            SELECT parent_uuid, child_uuid, series_uuid, completed_at, metadata_json
            FROM {TABLE_NAME}
            WHERE parent_uuid IN :row_uuids OR child_uuid IN :row_uuids
        """).bindparams(bindparam("row_uuids", expanding=True))
        for offset in range(0, len(row_uuids), 400):
            records = conn.execute(statement, {"row_uuids": row_uuids[offset:offset + 400]})
            for record in records.mappings():
                parent = links.get(record["parent_uuid"])
                child = links.get(record["child_uuid"])
                metadata = json.loads(record["metadata_json"] or "{}")
                if not isinstance(metadata, dict):
                    raise OccurrenceStorageError(STORAGE_MESSAGE)
                if parent is not None:
                    parent["completed_at"] = record["completed_at"]
                    parent["snapshot"] = metadata.get("_parent_snapshot")
                    parent["annotations"].update(
                        _recurrence_series_uuid=record["series_uuid"],
                        _recurrence_next_uuid=record["child_uuid"],
                        _recurrence_generated=True,
                    )
                if child is not None:
                    child["annotations"].update(
                        _recurrence_parent_uuid=record["parent_uuid"],
                        _recurrence_series_uuid=record["series_uuid"],
                    )
                    child["metadata"] = metadata
        return links
    except (SQLAlchemyError, ValueError, TypeError):
        raise OccurrenceStorageError(STORAGE_MESSAGE) from None


def apply_metadata(
    row: dict[str, Any],
    links: dict[str, dict[str, Any]] | None,
    fallback: dict[str, Any],
    missing_fields: set[str],
) -> dict[str, Any]:
    link = (links or {}).get(str(row.get("uuid") or ""), {})
    row.update(link.get("annotations", {}))
    metadata = {**link.get("metadata", {}), **fallback}
    for field in METADATA_FIELDS:
        if field in missing_fields:
            row[field] = (
                int(bool(metadata.get(field))) if field == "archived"
                else metadata.get(field)
            )
    return row


_COPIED_FIELDS = (
    "task", "priority", "relevant_link", "catagory", "task_group", "sub_group",
    "estimated_time", "project", "recurring", "recurring_interval", "recurring_days",
    "recurring_month_ordinal", "recurring_month_weekday",
)


def occurrence_payload(source: dict[str, Any], due_date: str, created_at: str) -> dict[str, Any]:
    parent_uuid = str(source.get("uuid") or "")
    completed_at = str(source.get("completed_time") or "")
    if not parent_uuid or not completed_at or int(source.get("completed") or 0) != 1:
        raise ValueError("A completed recurring occurrence is required")
    if int(source.get("recurring") or 0) != 1 or source.get("archived"):
        raise ValueError("An active recurrence rule is required")
    if date.fromisoformat(due_date).isoformat() != due_date:
        raise ValueError("Invalid occurrence due date")
    datetime.fromisoformat(created_at)
    payload = {field: source.get(field) for field in _COPIED_FIELDS}
    payload.update(
        uuid=str(uuid5(NAMESPACE_URL, f"luigi-web/recurring/{parent_uuid}/{completed_at}")),
        status="Not Started", completed=0, completed_time=None,
        task_creation=created_at, start_time=None, due_date=due_date,
        logged_hours=0, archived=0,
    )
    return payload