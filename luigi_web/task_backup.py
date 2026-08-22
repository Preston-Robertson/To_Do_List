"""Validated preview/commit workflow for shared task backup restoration."""
from __future__ import annotations

import json
import secrets
import threading
import time
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import text

from . import db

FORMAT = "luigi-task-backup-v1"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
PREVIEW_TTL_SECONDS = 15 * 60

_PREVIEW_LOCK = threading.Lock()
_PREVIEWS: dict[str, tuple[float, dict[str, Any], dict[str, Any]]] = {}

_COMPLETION_COLUMNS = (
    "task", "catagory", "completed_date", "logged_at",
)
_UUID_TABLE_COLUMNS = {
    "tasks": db._TASK_COLUMNS,
    "recurring_tasks": db._TASK_COLUMNS,
    "discipline_list": db._DISCIPLINE_COLUMNS,
    "follow_up_tasks": db._FOLLOWUP_COLUMNS,
}
_TABLES = (*_UUID_TABLE_COLUMNS, "discipline_completions")


class RestoreError(ValueError):
    pass


def _text(value: Any, field: str, *, required: bool = False,
          maximum: int = 5000) -> str | None:
    if value is None:
        if required:
            raise RestoreError(f"{field} is required")
        return None
    result = str(value)
    if required and not result.strip():
        raise RestoreError(f"{field} is required")
    if len(result) > maximum or "\x00" in result or "\r" in result or "\n" in result:
        raise RestoreError(f"{field} is invalid")
    return result


def _uuid(value: Any, field: str) -> str:
    raw = _text(value, field, required=True, maximum=64) or ""
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError) as exc:
        raise RestoreError(f"{field} must be a UUID") from exc


def _iso_date(value: Any, field: str, *, required: bool = False) -> str | None:
    raw = _text(value, field, required=required, maximum=40)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError as exc:
        raise RestoreError(f"{field} must be an ISO date") from exc


def _iso_datetime(value: Any, field: str) -> str | None:
    raw = _text(value, field, maximum=80)
    if not raw:
        return None
    try:
        datetime.fromisoformat(raw)
    except ValueError as exc:
        raise RestoreError(f"{field} must be an ISO timestamp") from exc
    return raw


def _number(value: Any, field: str, *, integer: bool = False) -> int | float | None:
    if value in (None, ""):
        return None
    try:
        return int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise RestoreError(f"{field} must be numeric") from exc


def _task_row(table: str, row: dict[str, Any], index: int) -> dict[str, Any]:
    prefix = f"{table}[{index}]"
    result = {
        key: row.get(key) for key in db._TASK_COLUMNS if key in row
    }
    result["uuid"] = _uuid(row.get("uuid"), f"{prefix}.uuid")
    result["task"] = _text(row.get("task"), f"{prefix}.task", required=True, maximum=2000)
    status = str(row.get("status") or "Not Started")
    if status not in db.STATUS_VALUES:
        raise RestoreError(f"{prefix}.status is invalid")
    result["status"] = status
    for field in ("priority", "completed", "recurring", "recurring_interval",
                  "recurring_month_ordinal", "recurring_month_weekday", "archived"):
        if field in result:
            result[field] = _number(result[field], f"{prefix}.{field}", integer=True)
    for field in ("estimated_time", "logged_hours"):
        if field in result:
            result[field] = _number(result[field], f"{prefix}.{field}")
    for field in ("due_date", "task_creation"):
        if field in result:
            result[field] = _iso_date(result[field], f"{prefix}.{field}")
    for field in ("start_time", "completed_time"):
        if field in result:
            result[field] = _iso_datetime(result[field], f"{prefix}.{field}")
    for field in ("relevant_link", "catagory", "task_group", "sub_group",
                  "recurring_days", "project"):
        if field in result:
            result[field] = _text(result[field], f"{prefix}.{field}", maximum=4000)
    return result


def _discipline_row(row: dict[str, Any], index: int) -> dict[str, Any]:
    prefix = f"discipline_list[{index}]"
    return {
        "uuid": _uuid(row.get("uuid"), f"{prefix}.uuid"),
        "task": _text(row.get("task"), f"{prefix}.task", required=True, maximum=2000),
        "catagory": _text(row.get("catagory"), f"{prefix}.catagory", maximum=1000),
        "frequency_per_week": db._discipline_frequency(row.get("frequency_per_week")),
        "active": db._to_int_bool(row.get("active")),
        "current_streak": int(_number(row.get("current_streak") or 0,
                                      f"{prefix}.current_streak", integer=True) or 0),
    }


def _follow_up_row(row: dict[str, Any], index: int) -> dict[str, Any]:
    prefix = f"follow_up_tasks[{index}]"
    result = {key: row.get(key) for key in db._FOLLOWUP_COLUMNS if key in row}
    result["uuid"] = _uuid(row.get("uuid"), f"{prefix}.uuid")
    result["trigger_task"] = _text(row.get("trigger_task"), f"{prefix}.trigger_task",
                                    required=True, maximum=2000)
    result["follow_up_task"] = _text(row.get("follow_up_task"),
                                      f"{prefix}.follow_up_task", required=True,
                                      maximum=2000)
    for field in ("priority", "due_offset_days"):
        if field in result:
            result[field] = _number(result[field], f"{prefix}.{field}", integer=True)
    if "estimated_time" in result:
        result["estimated_time"] = _number(result["estimated_time"],
                                             f"{prefix}.estimated_time")
    for field in ("catagory", "task_group", "subgroup", "relevant_link"):
        if field in result:
            result[field] = _text(result[field], f"{prefix}.{field}", maximum=4000)
    if "created" in result:
        result["created"] = _iso_datetime(result["created"], f"{prefix}.created")
    return result


def _completion_row(row: dict[str, Any], index: int) -> dict[str, Any]:
    prefix = f"discipline_completions[{index}]"
    return {
        "task": _text(row.get("task"), f"{prefix}.task", required=True, maximum=2000),
        "catagory": _text(row.get("catagory"), f"{prefix}.catagory", maximum=1000),
        "completed_date": _iso_date(row.get("completed_date"),
                                     f"{prefix}.completed_date", required=True),
        "logged_at": _iso_datetime(row.get("logged_at"), f"{prefix}.logged_at"),
    }


def parse_backup(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise RestoreError("backup file must be between 1 byte and 20 MB")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestoreError("backup must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise RestoreError("backup root must be an object")
    if payload.get("format") not in (None, FORMAT):
        raise RestoreError("unsupported task backup format")
    if int(payload.get("schema_version") or 0) != 2:
        raise RestoreError("backup schema version must be 2")
    tables = payload.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(_TABLES):
        raise RestoreError("backup must contain exactly the supported task tables")

    normalized: dict[str, list[dict[str, Any]]] = {}
    for table in _TABLES:
        source_rows = tables[table]
        if not isinstance(source_rows, list) or len(source_rows) > 100000:
            raise RestoreError(f"{table} must be a bounded row list")
        rows: list[dict[str, Any]] = []
        seen: set[Any] = set()
        for index, raw_row in enumerate(source_rows):
            if not isinstance(raw_row, dict):
                raise RestoreError(f"{table}[{index}] must be an object")
            if table in {"tasks", "recurring_tasks"}:
                row = _task_row(table, raw_row, index)
                key = row["uuid"]
            elif table == "discipline_list":
                row = _discipline_row(raw_row, index)
                key = row["uuid"]
            elif table == "follow_up_tasks":
                row = _follow_up_row(raw_row, index)
                key = row["uuid"]
            else:
                row = _completion_row(raw_row, index)
                key = (row["task"], row["completed_date"])
            if key in seen:
                raise RestoreError(f"{table} contains duplicate key {key}")
            seen.add(key)
            rows.append(row)
        normalized[table] = rows

    metadata = payload.get("web_metadata") or {}
    if not isinstance(metadata, dict):
        raise RestoreError("web_metadata must be an object")
    safe_metadata: dict[str, dict[str, dict[str, Any]]] = {}
    for table in ("tasks", "recurring_tasks"):
        table_data = metadata.get(table) or {}
        if not isinstance(table_data, dict):
            raise RestoreError(f"web_metadata.{table} must be an object")
        safe_metadata[table] = {}
        for raw_uuid, values in table_data.items():
            row_uuid = _uuid(raw_uuid, f"web_metadata.{table}.uuid")
            if not isinstance(values, dict):
                raise RestoreError(f"web_metadata.{table}.{row_uuid} must be an object")
            safe_metadata[table][row_uuid] = {
                key: values[key] for key in ("project", "archived") if key in values
            }
    return {
        "format": FORMAT,
        "schema_version": 2,
        "tables": normalized,
        "web_metadata": safe_metadata,
    }


def _existing_keys(conn, table: str) -> set[Any]:
    if table == "discipline_completions":
        return {
            (str(row.task), str(row.completed_date)[:10])
            for row in conn.execute(text(
                "SELECT task, completed_date FROM discipline_completions"
            ))
        }
    return {
        str(row[0]) for row in conn.execute(text(f"SELECT uuid FROM {table}"))
    }


def preview_restore(payload: dict[str, Any]) -> dict[str, Any]:
    with db.get_engine().connect() as conn:
        tables: dict[str, dict[str, int]] = {}
        for table, rows in payload["tables"].items():
            existing = _existing_keys(conn, table)
            keys = [
                (row["task"], row["completed_date"])
                if table == "discipline_completions" else row["uuid"]
                for row in rows
            ]
            updates = sum(1 for key in keys if key in existing)
            tables[table] = {"insert": len(keys) - updates, "update": updates}
    metadata_count = sum(len(rows) for rows in payload["web_metadata"].values())
    return {
        "tables": tables,
        "metadata_rows": metadata_count,
        "total_rows": sum(sum(counts.values()) for counts in tables.values()),
    }


def _upsert_uuid_row(conn, table: str, row: dict[str, Any]) -> str:
    columns = (
        db._cols_for(table) if table in {"tasks", "recurring_tasks"}
        else _UUID_TABLE_COLUMNS[table]
    )
    payload = {column: row.get(column) for column in columns if column in row}
    exists = conn.execute(
        text(f"SELECT 1 FROM {table} WHERE uuid = :uuid"),
        {"uuid": row["uuid"]},
    ).first()
    if exists:
        mutable = [column for column in payload if column != "uuid"]
        if mutable:
            conn.execute(
                text(f"UPDATE {table} SET " + ", ".join(
                    f"{column} = :{column}" for column in mutable
                ) + " WHERE uuid = :uuid"),
                payload,
            )
        return "update"
    conn.execute(
        text(f"INSERT INTO {table} ({', '.join(payload)}) VALUES (" +
             ", ".join(f":{column}" for column in payload) + ")"),
        payload,
    )
    return "insert"


def restore_backup(payload: dict[str, Any]) -> dict[str, dict[str, int]]:
    old_metadata = db._read_web_metadata()
    merged_metadata = json.loads(json.dumps(old_metadata))
    for table, rows in payload["web_metadata"].items():
        merged_metadata.setdefault(table, {}).update(rows)

    connection = db.get_engine().connect()
    transaction = connection.begin()
    metadata_written = False
    result = {
        table: {"insert": 0, "update": 0} for table in _TABLES
    }
    try:
        for table in _UUID_TABLE_COLUMNS:
            for row in payload["tables"][table]:
                outcome = _upsert_uuid_row(connection, table, row)
                result[table][outcome] += 1
        for row in payload["tables"]["discipline_completions"]:
            exists = connection.execute(text("""
                SELECT 1 FROM discipline_completions
                WHERE task = :task AND completed_date = :completed_date
            """), row).first()
            if exists:
                connection.execute(text("""
                    UPDATE discipline_completions
                    SET catagory = :catagory, logged_at = :logged_at
                    WHERE task = :task AND completed_date = :completed_date
                """), row)
                result["discipline_completions"]["update"] += 1
            else:
                connection.execute(text("""
                    INSERT INTO discipline_completions
                        (task, catagory, completed_date, logged_at)
                    VALUES (:task, :catagory, :completed_date, :logged_at)
                """), row)
                result["discipline_completions"]["insert"] += 1

        for table, rows in payload["tables"].items():
            expected = {
                (row["task"], row["completed_date"])
                if table == "discipline_completions" else row["uuid"]
                for row in rows
            }
            if not expected.issubset(_existing_keys(connection, table)):
                raise RuntimeError(f"{table} restore verification failed")

        with db._WEB_META_LOCK:
            db._write_web_metadata(merged_metadata)
            metadata_written = True
        transaction.commit()
    except Exception:
        transaction.rollback()
        if metadata_written:
            with db._WEB_META_LOCK:
                db._write_web_metadata(old_metadata)
        raise
    finally:
        connection.close()
    return result


def prepare(raw: bytes) -> tuple[str, dict[str, Any]]:
    payload = parse_backup(raw)
    plan = preview_restore(payload)
    token = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _PREVIEW_LOCK:
        expired = [key for key, value in _PREVIEWS.items() if value[0] <= now]
        for key in expired:
            _PREVIEWS.pop(key, None)
        _PREVIEWS[token] = (now + PREVIEW_TTL_SECONDS, payload, plan)
    return token, plan


def commit(token: str) -> dict[str, dict[str, int]]:
    with _PREVIEW_LOCK:
        entry = _PREVIEWS.pop(str(token or ""), None)
    if entry is None or entry[0] <= time.monotonic():
        raise RestoreError("restore preview expired or was already used")
    return restore_backup(entry[1])