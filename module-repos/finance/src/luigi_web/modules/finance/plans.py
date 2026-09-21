"""Finance-only scenario and income storage; importing this module performs no I/O.

BACKUP_COLUMNS is the exact export/restore column allow-list. Initialize these
tables before opening a parent restore transaction, validate backup rows before
writing, and bind all values in that single transaction. Public plan records
expose decoded ``config`` instead of the storage-only ``payload_json``.

``validate_backup(plans, streams)`` returns two normalized SQL-row lists without
I/O. Merge-style restores must call ``validate_restore_capacity`` inside their
write transaction before inserting/updating these rows, so existing records are
included in the limits. The parent owns atomic restore and backup integration.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import date, datetime
from decimal import DecimalException
from typing import Any

from . import repository as finance


MAX_MONEY = 100_000_000_000_000
MAX_PLANS = 30
MAX_INCOME_STREAMS = 100
SCHEMA_VERSION = 1
BACKUP_COLUMNS = {
    "finance_plans": (
        "id", "kind", "name", "payload_json", "version", "created_at", "updated_at",
    ),
    "finance_income_streams": (
        "id", "name", "amount_minor", "cadence", "start_date", "end_date",
        "annual_growth_bps", "active", "currency", "version", "created_at", "updated_at",
    ),
}
BACKUP_TABLES = tuple(BACKUP_COLUMNS)
_MAX_VERSION = 2**63 - 1
_CASHFLOW_BOUNDS = {
    "months": (1, 60), "years": (1, 50),
    "opening_cash_minor": (-MAX_MONEY, MAX_MONEY),
    "net_income_minor": (0, MAX_MONEY),
    "annual_income_growth_bps": (0, 3000),
    "variable_expense_minor": (0, MAX_MONEY),
    "annual_expense_growth_bps": (0, 3000),
    "monthly_contribution_minor": (0, MAX_MONEY),
    "reserve_minor": (0, MAX_MONEY),
    "annual_return_bps": (-5000, 3000), "inflation_bps": (0, 2000),
    "opening_investments_minor": (0, MAX_MONEY),
}
_HOUSING_BOUNDS = {
    "rent_minor": (0, MAX_MONEY), "rent_growth_bps": (0, 3000),
    "home_price_minor": (1, MAX_MONEY), "down_payment_minor": (0, MAX_MONEY),
    "mortgage_rate_bps": (0, 3000), "term_years": (1, 50),
    "annual_property_tax_bps": (0, 2000),
    "annual_insurance_minor": (0, MAX_MONEY),
    "annual_maintenance_bps": (0, 2000), "hoa_minor": (0, MAX_MONEY),
    "closing_cost_minor": (0, MAX_MONEY), "appreciation_bps": (-5000, 3000),
    "years": (1, 50), "investment_return_bps": (-5000, 3000),
}
_INCOME_FIELDS = {
    "name", "amount", "amount_minor", "cadence", "start_date", "end_date",
    "annual_growth_bps", "active", "currency",
}
_CADENCES = ("weekly", "biweekly", "monthly", "quarterly", "yearly")


class PlanConflict(RuntimeError):
    """A missing or stale record must be reloaded before a mutation."""

    def __init__(self) -> None:
        super().__init__("Finance record changed; reload and try again")


def init_db() -> None:
    """Lazily add app-owned tables to the existing isolated Finance database."""
    with finance._DB_LOCK:
        finance.init_db()
        with finance.connect(write=True) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS finance_plans (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('cashflow', 'housing')),
                    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 60),
                    payload_json TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(typeof(version) = 'integer' AND version > 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS finance_income_streams (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 60),
                    amount_minor INTEGER NOT NULL CHECK(
                        typeof(amount_minor) = 'integer' AND amount_minor BETWEEN 1 AND 100000000000000),
                    cadence TEXT NOT NULL CHECK(cadence IN (
                        'weekly', 'biweekly', 'monthly', 'quarterly', 'yearly')),
                    start_date TEXT NOT NULL,
                    end_date TEXT,
                    annual_growth_bps INTEGER NOT NULL CHECK(
                        typeof(annual_growth_bps) = 'integer' AND annual_growth_bps BETWEEN 0 AND 3000),
                    active INTEGER NOT NULL CHECK(typeof(active) = 'integer' AND active IN (0, 1)),
                    currency TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(typeof(version) = 'integer' AND version > 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)


def _integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Finance integer is invalid or outside the allowed range")
    return value


def _kind(value: Any) -> str:
    if not isinstance(value, str) or value not in ("cashflow", "housing"):
        raise ValueError("Invalid finance plan kind")
    return value


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value
    ):
        raise ValueError("Invalid finance record ID")
    return str(uuid.UUID(value))


def _alias(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Finance alias must be text")
    return finance.safe_label(value, "Finance alias", max_length=60)


def _currency(value: Any) -> str:
    if not isinstance(value, str) or value != finance.base_currency():
        raise ValueError("Currency must match the Finance base currency")
    return value


def _fields(value: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unsupported finance fields")
    return value


def normalize_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    """Accept only canonical integer-minor-unit/basis-point configuration keys."""
    kind = _kind(kind)
    bounds = _CASHFLOW_BOUNDS if kind == "cashflow" else _HOUSING_BOUNDS
    allowed = set(bounds) | {"schema_version", "currency"}
    if kind == "cashflow":
        allowed |= {"start_month", "income_mode"}
    config = _fields(config, allowed)
    normalized: dict[str, Any] = {
        "schema_version": _integer(config.get("schema_version", SCHEMA_VERSION), 1, 1),
        "currency": _currency(config.get("currency", finance.base_currency())),
    }
    defaults = {"months": 12, "years": 1, "term_years": 30}
    for field, (minimum, maximum) in bounds.items():
        if field == "investment_return_bps" and field not in config:
            continue
        normalized[field] = _integer(config.get(field, defaults.get(field, 0)), minimum, maximum)
    if kind == "cashflow":
        start_month = config.get("start_month")
        if start_month is None:
            today = finance.clock.local_today()
            year = today.year + (today.month == 12)
            month = today.month % 12 + 1
            start_month = f"{year:04d}-{month:02d}"
        if not isinstance(start_month, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", start_month):
            raise ValueError("Start month must be YYYY-MM")
        normalized["start_month"] = finance.month_value(start_month)
        income_mode = config.get("income_mode", "recurring")
        if not isinstance(income_mode, str) or income_mode not in ("recurring", "fixed", "streams"):
            raise ValueError("Invalid income mode")
        normalized["income_mode"] = income_mode
    elif normalized["down_payment_minor"] > normalized["home_price_minor"]:
        raise ValueError("Down payment cannot exceed home price")
    return normalized


def _read_record(connection: sqlite3.Connection, table: str, record_id: str) -> dict[str, Any] | None:
    row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
    return dict(row) if row is not None else None


def _save_record(table: str, values: dict[str, Any], record_id: str | None,
                 expected_version: int | None, limit: int, entity: str) -> dict[str, Any]:
    if record_id is not None:
        record_id = _identifier(record_id)
        if expected_version is None:
            raise PlanConflict()
    elif expected_version is not None:
        raise PlanConflict()
    if expected_version is not None:
        expected_version = _integer(expected_version, 1, _MAX_VERSION - 1)
    init_db()
    with finance.connect(write=True) as connection:
        timestamp = finance.now_iso()
        if record_id is None:
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count >= limit:
                raise ValueError("Finance record limit reached")
            record = dict(values, id=finance.new_id(), version=1,
                          created_at=timestamp, updated_at=timestamp)
            columns = BACKUP_COLUMNS[table]
            placeholders = ",".join("?" for _ in columns)
            connection.execute(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                tuple(record[column] for column in columns),
            )
            action = "create"
        else:
            previous = _read_record(connection, table, record_id)
            if previous is None or previous["version"] != expected_version:
                raise PlanConflict()
            if table == "finance_plans" and previous["kind"] != values["kind"]:
                raise PlanConflict()
            record = dict(values, id=record_id, version=previous["version"] + 1,
                          created_at=previous["created_at"], updated_at=timestamp)
            columns = tuple(column for column in BACKUP_COLUMNS[table] if column not in ("id", "created_at"))
            assignments = ",".join(f"{column} = ?" for column in columns)
            cursor = connection.execute(
                f"UPDATE {table} SET {assignments} WHERE id = ? AND version = ?",
                (*[record[column] for column in columns], record_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise PlanConflict()
            action = "update"
        finance._audit(connection, action, entity, record["id"], "Saved finance planning record")
        verified = _read_record(connection, table, record["id"])
        if verified is None or verified != record:
            raise RuntimeError("Finance write verification failed")
    return verified


def _delete_record(table: str, record_id: str, expected_version: int, entity: str) -> bool:
    record_id = _identifier(record_id)
    expected_version = _integer(expected_version, 1, _MAX_VERSION)
    init_db()
    with finance.connect(write=True) as connection:
        cursor = connection.execute(
            f"DELETE FROM {table} WHERE id = ? AND version = ?", (record_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise PlanConflict()
        finance._audit(connection, "delete", entity, record_id, "Deleted finance planning record")
        if _read_record(connection, table, record_id) is not None:
            raise RuntimeError("Finance write verification failed")
    return True


def _plan_result(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    result["config"] = json.loads(result.pop("payload_json"))
    return result


def save_plan(kind: str, name: str, config: dict[str, Any], plan_id: str | None = None,
              expected_version: int | None = None) -> dict[str, Any]:
    normalized = normalize_config(kind, config)
    values = {
        "kind": _kind(kind), "name": _alias(name),
        "payload_json": json.dumps(normalized, sort_keys=True, separators=(",", ":")),
    }
    return _plan_result(_save_record("finance_plans", values, plan_id, expected_version, MAX_PLANS, "plan"))


def list_plans(kind: str | None = None) -> list[dict[str, Any]]:
    if kind is not None:
        kind = _kind(kind)
    init_db()
    with finance.connect() as connection:
        where = " WHERE kind = ?" if kind is not None else ""
        rows = connection.execute(
            f"SELECT * FROM finance_plans{where} ORDER BY created_at, id",
            (kind,) if kind is not None else (),
        ).fetchall()
    return [_plan_result(dict(row)) for row in rows]


def get_plan(plan_id: str) -> dict[str, Any] | None:
    plan_id = _identifier(plan_id)
    init_db()
    with finance.connect() as connection:
        record = _read_record(connection, "finance_plans", plan_id)
    return _plan_result(record) if record is not None else None


def delete_plan(plan_id: str, expected_version: int) -> bool:
    return _delete_record("finance_plans", plan_id, expected_version, "plan")


def _date(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Finance date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("Finance date must be YYYY-MM-DD") from None


def _active(value: Any) -> int:
    if type(value) is bool:
        return int(value)
    if type(value) is int and value in (0, 1):
        return value
    if isinstance(value, str):
        if value.lower() in ("true", "on", "1"):
            return 1
        if value.lower() in ("false", "off", "0"):
            return 0
    raise ValueError("Invalid finance active flag")


def _income_values(data: dict[str, Any]) -> dict[str, Any]:
    data = _fields(data, _INCOME_FIELDS)
    if ("amount" in data) == ("amount_minor" in data):
        raise ValueError("Provide exactly one finance amount field")
    if "amount" in data:
        amount = data["amount"]
        if not isinstance(amount, str) or not amount or len(amount) > 128:
            raise ValueError("Finance amount must be a decimal string")
        try:
            amount_minor = finance.to_minor(amount)
        except (ValueError, DecimalException, OverflowError):
            raise ValueError("Invalid finance amount") from None
    else:
        amount_minor = data["amount_minor"]
    amount_minor = _integer(amount_minor, 1, MAX_MONEY)
    cadence = data.get("cadence", "monthly")
    if not isinstance(cadence, str) or cadence not in _CADENCES:
        raise ValueError("Invalid finance income cadence")
    start_date = _date(data.get("start_date"))
    end_date = data.get("end_date")
    if end_date is not None and end_date != "":
        end_date = _date(end_date)
        if end_date < start_date:
            raise ValueError("Finance end date cannot precede start date")
    else:
        end_date = None
    return {
        "name": _alias(data.get("name")), "amount_minor": amount_minor,
        "cadence": cadence, "start_date": start_date, "end_date": end_date,
        "annual_growth_bps": _integer(data.get("annual_growth_bps", 0), 0, 3000),
        "active": _active(data.get("active", True)),
        "currency": _currency(data.get("currency", finance.base_currency())),
    }


def _income_result(record: dict[str, Any]) -> dict[str, Any]:
    return dict(record, active=bool(record["active"]))


def save_income(data: dict[str, Any]) -> dict[str, Any]:
    """Save a full stream; updates require data.id and integer expected_version.

    Supply either ``amount`` as a decimal form string or ``amount_minor`` as an
    integer, never both. All other numeric inputs are canonical integers.
    """
    data = _fields(data, _INCOME_FIELDS | {"id", "expected_version"})
    values = _income_values({key: value for key, value in data.items() if key in _INCOME_FIELDS})
    record = _save_record(
        "finance_income_streams", values, data.get("id"), data.get("expected_version"),
        MAX_INCOME_STREAMS, "income_stream",
    )
    return _income_result(record)


def list_income(active_only: bool = False) -> list[dict[str, Any]]:
    if type(active_only) is not bool:
        raise ValueError("Invalid finance active filter")
    init_db()
    with finance.connect() as connection:
        where = " WHERE active = 1" if active_only else ""
        rows = connection.execute(
            f"SELECT * FROM finance_income_streams{where} ORDER BY created_at, id"
        ).fetchall()
    return [_income_result(dict(row)) for row in rows]


def delete_income(income_id: str, expected_version: int) -> bool:
    return _delete_record("finance_income_streams", income_id, expected_version, "income_stream")


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 32:
        raise ValueError("Invalid finance backup timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("Invalid finance backup timestamp") from None
    if parsed.isoformat(timespec="seconds") != value:
        raise ValueError("Invalid finance backup timestamp")
    return value


def _backup_metadata(row: dict[str, Any]) -> dict[str, Any]:
    created_at = _timestamp(row["created_at"])
    updated_at = _timestamp(row["updated_at"])
    try:
        valid_order = datetime.fromisoformat(updated_at) >= datetime.fromisoformat(created_at)
    except TypeError:
        valid_order = False
    if not valid_order:
        raise ValueError("Invalid finance backup timestamp order")
    return {
        "id": _identifier(row["id"]), "version": _integer(row["version"], 1, _MAX_VERSION),
        "created_at": created_at, "updated_at": updated_at,
    }


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate finance configuration field")
        result[key] = value
    return result


def _backup_config(kind: str, payload: Any) -> str:
    if not isinstance(payload, str) or len(payload) > 8192:
        raise ValueError("Invalid finance plan configuration")
    try:
        config = json.loads(payload, object_pairs_hook=_json_object)
    except (ValueError, RecursionError):
        raise ValueError("Invalid finance plan configuration") from None
    if not isinstance(config, dict) or not {"schema_version", "currency"} <= set(config):
        raise ValueError("Finance backup configuration needs schema version and currency")
    if kind == "cashflow" and config.get("start_month") is None:
        raise ValueError("Finance backup configuration needs a start month")
    normalized = normalize_config(kind, config)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def validate_backup(plans: list[dict[str, Any]], streams: list[dict[str, Any]]) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]]
]:
    """Validate exact SQL rows, returning (plan_rows, stream_rows) without I/O.

    Currency and schema version must be explicit in imported plan JSON. Stream
    active flags are exported SQL integers (0/1), not loosely parsed form values.
    Missing table arrays from older backups should be passed as empty lists.
    """
    if not isinstance(plans, list) or not isinstance(streams, list):
        raise ValueError("Finance backup records must be lists")
    if len(plans) > MAX_PLANS or len(streams) > MAX_INCOME_STREAMS:
        raise ValueError("Finance backup record limit reached")
    normalized_plans: list[dict[str, Any]] = []
    normalized_streams: list[dict[str, Any]] = []
    seen: set[str] = set()
    for table, rows, target in (
        ("finance_plans", plans, normalized_plans),
        ("finance_income_streams", streams, normalized_streams),
    ):
        for row in rows:
            columns = BACKUP_COLUMNS[table]
            if not isinstance(row, dict) or set(row) != set(columns):
                raise ValueError("Unsupported finance backup fields")
            metadata = _backup_metadata(row)
            if metadata["id"] in seen:
                raise ValueError("Duplicate finance backup record ID")
            seen.add(metadata["id"])
            if table == "finance_plans":
                kind = _kind(row["kind"])
                normalized = dict(metadata, kind=kind, name=_alias(row["name"]),
                                  payload_json=_backup_config(kind, row["payload_json"]))
            else:
                _integer(row["active"], 0, 1)
                values = _income_values({key: row[key] for key in _INCOME_FIELDS if key in row})
                normalized = dict(values, **metadata)
            target.append({column: normalized[column] for column in columns})
    return normalized_plans, normalized_streams


def validate_restore_capacity(connection: sqlite3.Connection, plans: list[dict[str, Any]],
                              streams: list[dict[str, Any]]) -> None:
    """Check merged counts inside the parent's write transaction before restore.

    Inputs must be the output of validate_backup. This helper neither initializes
    tables nor commits; the parent must initialize before BEGIN and bind inserts.
    """
    if not connection.in_transaction:
        raise RuntimeError("Finance restore requires a write transaction")
    for table, rows, limit in (
        ("finance_plans", plans, MAX_PLANS),
        ("finance_income_streams", streams, MAX_INCOME_STREAMS),
    ):
        identifiers = {row["id"] for row in rows}
        existing = {row[0] for row in connection.execute(f"SELECT id FROM {table}")}
        if len(existing | identifiers) > limit:
            raise ValueError("Finance restore record limit reached")