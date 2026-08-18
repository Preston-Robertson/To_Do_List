"""App-owned personal finance repository.

Finance is intentionally isolated from LuigiBot's PostgreSQL schema and from the
LLM tool registry. Persisted money uses integer minor units. User-facing labels
are aliases only; probable PII is rejected before storage.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator

from .paths import DATA_DIR

_DB_LOCK = threading.RLock()
_IMPORT_LOCK = threading.Lock()
_IMPORT_TTL_SECONDS = 15 * 60
_IMPORT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

_ACCOUNT_TYPES = {"checking", "savings", "cash", "credit", "investment", "other"}
_CADENCES = {"weekly", "monthly", "quarterly", "yearly"}
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_SSN_RE = re.compile(r"\b\d{3}[- ]?\d{2}[- ]?\d{4}\b")
_LONG_DIGITS_RE = re.compile(r"\d{8,}")


def db_path() -> Path:
    configured = os.environ.get("LUIGI_WEB_FINANCE_DB", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (DATA_DIR / "finance.db").resolve()


def base_currency() -> str:
    return normalize_currency(os.environ.get("LUIGI_WEB_FINANCE_BASE_CURRENCY", "USD"))


def normalize_currency(value: Any) -> str:
    currency = str(value or "USD").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency must be a three-letter ISO code")
    return currency


def account_currency(value: Any) -> str:
    currency = normalize_currency(value or base_currency())
    if currency != base_currency():
        raise ValueError(
            "account currency must match the Finance base currency until "
            "exchange-rate support is available"
        )
    return currency


def safe_label(value: Any, field: str, *, required: bool = True, max_length: int = 120) -> str:
    label = " ".join(str(value or "").strip().split())
    if required and not label:
        raise ValueError(f"{field} is required")
    if not label:
        return ""
    if len(label) > max_length:
        raise ValueError(f"{field} must be {max_length} characters or fewer")
    compact_digits = "".join(character for character in label if character.isdigit())
    if _EMAIL_RE.search(label) or _SSN_RE.search(label) or _LONG_DIGITS_RE.search(label):
        raise ValueError(f"{field} appears to contain personal identifying information")
    if len(compact_digits) >= 10:
        raise ValueError(f"{field} appears to contain a phone or account identifier")
    return label


def iso_date(value: Any, field: str = "date") -> str:
    raw = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def month_value(value: Any | None = None) -> str:
    raw = str(value or date.today().strftime("%Y-%m")).strip()
    if not re.fullmatch(r"\d{4}-\d{2}", raw):
        raise ValueError("month must be YYYY-MM")
    try:
        date.fromisoformat(f"{raw}-01")
    except ValueError as exc:
        raise ValueError("month must be YYYY-MM") from exc
    return raw


def to_minor(value: Any, field: str = "amount") -> int:
    raw = str(value or "").strip()
    negative_parentheses = raw.startswith("(") and raw.endswith(")")
    cleaned = raw.replace(",", "").replace("$", "").replace("€", "").replace("£", "")
    if negative_parentheses:
        cleaned = f"-{cleaned[1:-1]}"
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a valid monetary amount") from exc
    if not amount.is_finite():
        raise ValueError(f"{field} must be finite")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor(value: int | None) -> str:
    return f"{Decimal(int(value or 0)) / Decimal(100):.2f}"


def quantity_to_micro(value: Any) -> int:
    try:
        quantity = Decimal(str(value or "0"))
    except InvalidOperation as exc:
        raise ValueError("quantity must be a number") from exc
    if not quantity.is_finite():
        raise ValueError("quantity must be finite")
    if quantity < 0:
        raise ValueError("quantity cannot be negative")
    return int((quantity * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def quantity_from_micro(value: int | None) -> str:
    quantity = Decimal(int(value or 0)) / Decimal(1_000_000)
    return format(quantity.normalize(), "f")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


@contextmanager
def connect(*, write: bool = False) -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _DB_LOCK if write else _DB_LOCK
    with lock:
        connection = sqlite3.connect(path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()


def init_db() -> None:
    with connect(write=True) as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS finance_accounts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                account_type TEXT NOT NULL,
                currency TEXT NOT NULL,
                opening_balance_minor INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS finance_transactions (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES finance_accounts(id),
                transaction_date TEXT NOT NULL,
                amount_minor INTEGER NOT NULL,
                category TEXT NOT NULL,
                memo TEXT,
                import_hash TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_finance_transactions_date
                ON finance_transactions(transaction_date);
            CREATE INDEX IF NOT EXISTS idx_finance_transactions_account
                ON finance_transactions(account_id);
            CREATE TABLE IF NOT EXISTS finance_budgets (
                id TEXT PRIMARY KEY,
                month TEXT NOT NULL,
                category TEXT NOT NULL,
                limit_minor INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(month, category)
            );
            CREATE TABLE IF NOT EXISTS finance_holdings (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES finance_accounts(id),
                symbol TEXT NOT NULL,
                asset_name TEXT,
                quantity_micro INTEGER NOT NULL DEFAULT 0,
                cost_basis_minor INTEGER NOT NULL DEFAULT 0,
                market_value_minor INTEGER NOT NULL DEFAULT 0,
                as_of_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(account_id, symbol)
            );
            CREATE TABLE IF NOT EXISTS finance_recurring_items (
                id TEXT PRIMARY KEY,
                account_id TEXT REFERENCES finance_accounts(id),
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                amount_minor INTEGER NOT NULL,
                cadence TEXT NOT NULL,
                next_due_date TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS finance_net_worth_snapshots (
                id TEXT PRIMARY KEY,
                snapshot_date TEXT NOT NULL UNIQUE,
                total_minor INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS finance_saved_reports (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                report_type TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS finance_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT,
                summary TEXT NOT NULL
            );
        """)


def storage_health() -> str:
    init_db()
    with connect() as connection:
        result = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    if result != "ok":
        raise RuntimeError("Finance SQLite integrity check failed")
    return f"SQLite storage reachable; base currency {base_currency()}"


def _audit(connection: sqlite3.Connection, action: str, entity_type: str,
           entity_id: str | None, summary: str) -> None:
    connection.execute(
        "INSERT INTO finance_audit_events "
        "(occurred_at, action, entity_type, entity_id, summary) VALUES (?, ?, ?, ?, ?)",
        (now_iso(), action, entity_type, entity_id, safe_label(summary, "audit summary")),
    )


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def list_accounts(*, active_only: bool = True) -> list[dict[str, Any]]:
    init_db()
    where = "WHERE a.active = 1" if active_only else ""
    with connect() as connection:
        rows = _rows(connection.execute(f"""
            SELECT a.*,
                   a.opening_balance_minor + COALESCE(SUM(t.amount_minor), 0) AS balance_minor
            FROM finance_accounts a
            LEFT JOIN finance_transactions t ON t.account_id = a.id
            {where}
            GROUP BY a.id
            ORDER BY a.active DESC, a.name COLLATE NOCASE
        """))
    for row in rows:
        row["balance"] = from_minor(row["balance_minor"])
    return rows


def get_account(account_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM finance_accounts WHERE id = ?", (account_id,)
        ).fetchone()
    return dict(row) if row else None


def create_account(data: dict[str, Any]) -> str:
    init_db()
    account_id = new_id()
    name = safe_label(data.get("name"), "account alias", max_length=60)
    account_type = str(data.get("account_type") or "checking").strip().lower()
    if account_type not in _ACCOUNT_TYPES:
        raise ValueError("invalid account type")
    currency = account_currency(data.get("currency"))
    opening = to_minor(data.get("opening_balance") or "0", "opening balance")
    timestamp = now_iso()
    with connect(write=True) as connection:
        connection.execute(
            "INSERT INTO finance_accounts "
            "(id, name, account_type, currency, opening_balance_minor, active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (account_id, name, account_type, currency, opening, timestamp, timestamp),
        )
        _audit(connection, "create", "account", account_id, "Created account alias")
    return account_id


def update_account(account_id: str, data: dict[str, Any]) -> bool:
    account = get_account(account_id)
    if not account:
        return False
    name = safe_label(data.get("name", account["name"]), "account alias", max_length=60)
    account_type = str(data.get("account_type", account["account_type"])).strip().lower()
    if account_type not in _ACCOUNT_TYPES:
        raise ValueError("invalid account type")
    currency = account_currency(data.get("currency", account["currency"]))
    opening = to_minor(
        data.get("opening_balance", from_minor(account["opening_balance_minor"])),
        "opening balance",
    )
    active = 1 if str(data.get("active", account["active"])).lower() in {"1", "true", "on", "yes"} else 0
    with connect(write=True) as connection:
        connection.execute(
            "UPDATE finance_accounts SET name=?, account_type=?, currency=?, "
            "opening_balance_minor=?, active=?, updated_at=? WHERE id=?",
            (name, account_type, currency, opening, active, now_iso(), account_id),
        )
        _audit(connection, "update", "account", account_id, "Updated account alias")
    return True


def list_transactions(*, month: str | None = None, account_id: str | None = None,
                      limit: int | None = 100) -> list[dict[str, Any]]:
    init_db()
    clauses: list[str] = []
    params: list[Any] = []
    if month:
        clauses.append("substr(t.transaction_date, 1, 7) = ?")
        params.append(month_value(month))
    if account_id:
        clauses.append("t.account_id = ?")
        params.append(account_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_clause = ""
    if limit is not None:
        params.append(max(1, min(int(limit), 1000)))
        limit_clause = "LIMIT ?"
    with connect() as connection:
        rows = _rows(connection.execute(f"""
            SELECT t.*, a.name AS account_name, a.currency
            FROM finance_transactions t
            JOIN finance_accounts a ON a.id = t.account_id
            {where}
            ORDER BY t.transaction_date DESC, t.created_at DESC
            {limit_clause}
        """, params))
    for row in rows:
        row["amount"] = from_minor(row["amount_minor"])
    return rows


def _transaction_hash(account_id: str, transaction_date: str, amount_minor: int,
                      category: str, memo: str) -> str:
    material = "\x1f".join((account_id, transaction_date, str(amount_minor), category.casefold(), memo.casefold()))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def create_transaction(data: dict[str, Any], *, imported: bool = False) -> str:
    init_db()
    transaction_id = new_id()
    account_id = str(data.get("account_id") or "").strip()
    if not get_account(account_id):
        raise ValueError("account not found")
    transaction_date = iso_date(data.get("transaction_date"))
    amount_minor = to_minor(data.get("amount"), "amount")
    if amount_minor == 0:
        raise ValueError("amount cannot be zero")
    category = safe_label(data.get("category"), "category", max_length=60)
    memo = safe_label(data.get("memo"), "memo", required=False, max_length=120)
    import_hash = _transaction_hash(account_id, transaction_date, amount_minor, category, memo)
    timestamp = now_iso()
    with connect(write=True) as connection:
        try:
            connection.execute(
                "INSERT INTO finance_transactions "
                "(id, account_id, transaction_date, amount_minor, category, memo, import_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (transaction_id, account_id, transaction_date, amount_minor, category,
                 memo or None, import_hash, timestamp, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            if "import_hash" in str(exc):
                raise ValueError("duplicate transaction") from exc
            raise
        _audit(
            connection, "import" if imported else "create", "transaction",
            transaction_id, "Imported transaction" if imported else "Created transaction",
        )
    return transaction_id


def delete_transaction(transaction_id: str) -> bool:
    init_db()
    with connect(write=True) as connection:
        result = connection.execute(
            "DELETE FROM finance_transactions WHERE id = ?", (transaction_id,)
        )
        if result.rowcount:
            _audit(connection, "delete", "transaction", transaction_id, "Deleted transaction")
        return bool(result.rowcount)


def list_budgets(month: str) -> list[dict[str, Any]]:
    month = month_value(month)
    init_db()
    with connect() as connection:
        rows = _rows(connection.execute("""
            SELECT b.*,
                   COALESCE(-SUM(CASE WHEN t.amount_minor < 0 THEN t.amount_minor ELSE 0 END), 0) AS spent_minor
            FROM finance_budgets b
            LEFT JOIN finance_transactions t
              ON lower(t.category) = lower(b.category)
             AND substr(t.transaction_date, 1, 7) = b.month
            WHERE b.month = ?
            GROUP BY b.id
            ORDER BY b.category COLLATE NOCASE
        """, (month,)))
    for row in rows:
        limit_minor = int(row["limit_minor"] or 0)
        spent_minor = int(row["spent_minor"] or 0)
        row["limit"] = from_minor(limit_minor)
        row["spent"] = from_minor(spent_minor)
        row["percent"] = round(spent_minor / limit_minor * 100) if limit_minor > 0 else 0
    return rows


def upsert_budget(data: dict[str, Any]) -> str:
    init_db()
    budget_id = str(data.get("id") or new_id())
    month = month_value(data.get("month"))
    category = safe_label(data.get("category"), "category", max_length=60)
    limit_minor = to_minor(data.get("limit"), "budget limit")
    if limit_minor <= 0:
        raise ValueError("budget limit must be positive")
    timestamp = now_iso()
    with connect(write=True) as connection:
        existing = connection.execute(
            "SELECT id FROM finance_budgets WHERE month=? AND lower(category)=lower(?)",
            (month, category),
        ).fetchone()
        if existing:
            target_id = str(existing["id"])
            connection.execute(
                "UPDATE finance_budgets SET category=?, limit_minor=?, updated_at=? WHERE id=?",
                (category, limit_minor, timestamp, target_id),
            )
        else:
            target_id = budget_id
            connection.execute(
                "INSERT INTO finance_budgets "
                "(id, month, category, limit_minor, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (target_id, month, category, limit_minor, timestamp, timestamp),
            )
        _audit(connection, "upsert", "budget", target_id, "Saved monthly budget")
    return target_id


def delete_budget(budget_id: str) -> bool:
    with connect(write=True) as connection:
        result = connection.execute("DELETE FROM finance_budgets WHERE id=?", (budget_id,))
        if result.rowcount:
            _audit(connection, "delete", "budget", budget_id, "Deleted budget")
        return bool(result.rowcount)


def list_holdings() -> list[dict[str, Any]]:
    init_db()
    with connect() as connection:
        rows = _rows(connection.execute("""
            SELECT h.*, a.name AS account_name, a.currency
            FROM finance_holdings h
            JOIN finance_accounts a ON a.id = h.account_id
            ORDER BY h.market_value_minor DESC, h.symbol
        """))
    for row in rows:
        row["quantity"] = quantity_from_micro(row["quantity_micro"])
        row["cost_basis"] = from_minor(row["cost_basis_minor"])
        row["market_value"] = from_minor(row["market_value_minor"])
        row["gain_minor"] = int(row["market_value_minor"]) - int(row["cost_basis_minor"])
        row["gain"] = from_minor(row["gain_minor"])
    return rows


def upsert_holding(data: dict[str, Any]) -> str:
    init_db()
    holding_id = str(data.get("id") or new_id())
    account_id = str(data.get("account_id") or "").strip()
    account = get_account(account_id)
    if not account:
        raise ValueError("account not found")
    symbol = safe_label(data.get("symbol"), "symbol", max_length=16).upper()
    if not re.fullmatch(r"[A-Z0-9._-]{1,16}", symbol):
        raise ValueError("symbol contains unsupported characters")
    asset_name = safe_label(data.get("asset_name"), "asset alias", required=False, max_length=80)
    quantity_micro = quantity_to_micro(data.get("quantity"))
    cost_basis_minor = to_minor(data.get("cost_basis") or "0", "cost basis")
    market_value_minor = to_minor(data.get("market_value") or "0", "market value")
    if cost_basis_minor < 0 or market_value_minor < 0:
        raise ValueError("holding values cannot be negative")
    as_of = iso_date(data.get("as_of_date") or date.today().isoformat(), "as-of date")
    timestamp = now_iso()
    with connect(write=True) as connection:
        existing = connection.execute(
            "SELECT id FROM finance_holdings WHERE account_id=? AND symbol=?",
            (account_id, symbol),
        ).fetchone()
        target_id = str(existing["id"]) if existing else holding_id
        connection.execute("""
            INSERT INTO finance_holdings
                (id, account_id, symbol, asset_name, quantity_micro,
                 cost_basis_minor, market_value_minor, as_of_date, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, symbol) DO UPDATE SET
                asset_name=excluded.asset_name,
                quantity_micro=excluded.quantity_micro,
                cost_basis_minor=excluded.cost_basis_minor,
                market_value_minor=excluded.market_value_minor,
                as_of_date=excluded.as_of_date,
                updated_at=excluded.updated_at
        """, (target_id, account_id, symbol, asset_name or None, quantity_micro,
              cost_basis_minor, market_value_minor, as_of, timestamp, timestamp))
        _audit(connection, "upsert", "holding", target_id, "Saved investment holding")
    return target_id


def delete_holding(holding_id: str) -> bool:
    with connect(write=True) as connection:
        result = connection.execute("DELETE FROM finance_holdings WHERE id=?", (holding_id,))
        if result.rowcount:
            _audit(connection, "delete", "holding", holding_id, "Deleted investment holding")
        return bool(result.rowcount)


def list_recurring_items(*, active_only: bool = True) -> list[dict[str, Any]]:
    init_db()
    where = "WHERE r.active = 1" if active_only else ""
    with connect() as connection:
        rows = _rows(connection.execute(f"""
            SELECT r.*, a.name AS account_name, COALESCE(a.currency, ?) AS currency
            FROM finance_recurring_items r
            LEFT JOIN finance_accounts a ON a.id = r.account_id
            {where}
            ORDER BY r.next_due_date, r.name COLLATE NOCASE
        """, (base_currency(),)))
    for row in rows:
        row["amount"] = from_minor(row["amount_minor"])
    return rows


def upsert_recurring_item(data: dict[str, Any]) -> str:
    init_db()
    item_id = str(data.get("id") or new_id())
    account_id = str(data.get("account_id") or "").strip() or None
    if account_id and not get_account(account_id):
        raise ValueError("account not found")
    name = safe_label(data.get("name"), "recurring item alias", max_length=80)
    category = safe_label(data.get("category"), "category", max_length=60)
    amount_minor = to_minor(data.get("amount"), "amount")
    if amount_minor == 0:
        raise ValueError("amount cannot be zero")
    cadence = str(data.get("cadence") or "monthly").strip().lower()
    if cadence not in _CADENCES:
        raise ValueError("invalid cadence")
    next_due = iso_date(data.get("next_due_date"), "next due date")
    active = 1 if str(data.get("active", "1")).lower() in {"1", "true", "on", "yes"} else 0
    timestamp = now_iso()
    with connect(write=True) as connection:
        connection.execute("""
            INSERT INTO finance_recurring_items
                (id, account_id, name, category, amount_minor, cadence,
                 next_due_date, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                account_id=excluded.account_id, name=excluded.name,
                category=excluded.category, amount_minor=excluded.amount_minor,
                cadence=excluded.cadence, next_due_date=excluded.next_due_date,
                active=excluded.active, updated_at=excluded.updated_at
        """, (item_id, account_id, name, category, amount_minor, cadence,
              next_due, active, timestamp, timestamp))
        _audit(connection, "upsert", "recurring_item", item_id, "Saved recurring finance item")
    return item_id


def delete_recurring_item(item_id: str) -> bool:
    with connect(write=True) as connection:
        result = connection.execute("DELETE FROM finance_recurring_items WHERE id=?", (item_id,))
        if result.rowcount:
            _audit(connection, "delete", "recurring_item", item_id, "Deleted recurring finance item")
        return bool(result.rowcount)


def dashboard(month: str | None = None) -> dict[str, Any]:
    selected_month = month_value(month)
    accounts = list_accounts(active_only=False)
    transactions = list_transactions(month=selected_month, limit=50)
    budgets = list_budgets(selected_month)
    holdings = list_holdings()
    recurring = list_recurring_items()
    with connect() as connection:
        aggregate = connection.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN amount_minor > 0 THEN amount_minor ELSE 0 END), 0),
                COALESCE(-SUM(CASE WHEN amount_minor < 0 THEN amount_minor ELSE 0 END), 0)
            FROM finance_transactions
            WHERE substr(transaction_date, 1, 7) = ?
        """, (selected_month,)).fetchone()
    income_minor = int(aggregate[0])
    expense_minor = int(aggregate[1])
    cash_minor = sum(
        int(row["balance_minor"]) for row in accounts if int(row["active"])
    )
    holdings_minor = sum(int(row["market_value_minor"]) for row in holdings)
    net_worth_minor = cash_minor + holdings_minor
    return {
        "month": selected_month,
        "currency": base_currency(),
        "accounts": accounts,
        "transactions": transactions,
        "budgets": budgets,
        "holdings": holdings,
        "recurring_items": recurring,
        "income_minor": income_minor,
        "income": from_minor(income_minor),
        "expense_minor": expense_minor,
        "expense": from_minor(expense_minor),
        "cash_minor": cash_minor,
        "cash": from_minor(cash_minor),
        "holdings_minor": holdings_minor,
        "holdings_value": from_minor(holdings_minor),
        "net_worth_minor": net_worth_minor,
        "net_worth": from_minor(net_worth_minor),
        "alerts": finance_alerts(budgets=budgets, recurring=recurring, holdings=holdings),
    }


def finance_alerts(*, budgets: list[dict[str, Any]] | None = None,
                   recurring: list[dict[str, Any]] | None = None,
                   holdings: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    budgets = budgets if budgets is not None else list_budgets(month_value())
    recurring = recurring if recurring is not None else list_recurring_items()
    holdings = holdings if holdings is not None else list_holdings()
    alerts: list[dict[str, str]] = []
    for budget in budgets:
        percent = int(budget.get("percent") or 0)
        if percent >= 100:
            alerts.append({"level": "danger", "title": "Budget exceeded", "detail": budget["category"]})
        elif percent >= 80:
            alerts.append({"level": "warning", "title": "Budget nearing limit", "detail": budget["category"]})
    today = date.today()
    soon = today + timedelta(days=7)
    for item in recurring:
        due = date.fromisoformat(str(item["next_due_date"])[:10])
        if due <= soon:
            alerts.append({
                "level": "warning" if due >= today else "danger",
                "title": "Recurring item due" if due >= today else "Recurring item overdue",
                "detail": item["name"],
            })
    stale_before = today - timedelta(days=7)
    for holding in holdings:
        if date.fromisoformat(str(holding["as_of_date"])[:10]) < stale_before:
            alerts.append({"level": "info", "title": "Holding value is stale", "detail": holding["symbol"]})
    return alerts[:12]


def save_net_worth_snapshot(snapshot_date: str | None = None) -> str:
    state = dashboard()
    snapshot_id = new_id()
    day = iso_date(snapshot_date or date.today().isoformat(), "snapshot date")
    with connect(write=True) as connection:
        connection.execute("""
            INSERT INTO finance_net_worth_snapshots (id, snapshot_date, total_minor, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(snapshot_date) DO UPDATE SET total_minor=excluded.total_minor
        """, (snapshot_id, day, state["net_worth_minor"], now_iso()))
        _audit(connection, "snapshot", "net_worth", snapshot_id, "Saved net worth snapshot")
    return snapshot_id


def list_net_worth_snapshots(limit: int = 24) -> list[dict[str, Any]]:
    init_db()
    with connect() as connection:
        rows = _rows(connection.execute(
            "SELECT * FROM finance_net_worth_snapshots ORDER BY snapshot_date DESC LIMIT ?",
            (max(1, min(int(limit), 120)),),
        ))
    for row in rows:
        row["total"] = from_minor(row["total_minor"])
    return rows


def list_audit_events(limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    with connect() as connection:
        return _rows(connection.execute(
            "SELECT occurred_at, action, entity_type, summary "
            "FROM finance_audit_events ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ))


def save_report(data: dict[str, Any]) -> str:
    report_id = new_id()
    name = safe_label(data.get("name"), "report name", max_length=60)
    report_type = str(data.get("report_type") or "monthly").strip().lower()
    if report_type not in {"monthly", "budgets", "investments", "net_worth"}:
        raise ValueError("invalid report type")
    parameters = {"month": month_value(data.get("month"))} if report_type in {"monthly", "budgets"} else {}
    with connect(write=True) as connection:
        connection.execute(
            "INSERT INTO finance_saved_reports (id, name, report_type, parameters_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (report_id, name, report_type, json.dumps(parameters, sort_keys=True), now_iso()),
        )
        _audit(connection, "create", "saved_report", report_id, "Saved finance report")
    return report_id


def list_saved_reports() -> list[dict[str, Any]]:
    init_db()
    with connect() as connection:
        rows = _rows(connection.execute(
            "SELECT * FROM finance_saved_reports ORDER BY name COLLATE NOCASE"
        ))
    for row in rows:
        row["parameters"] = json.loads(row.pop("parameters_json"))
    return rows


def delete_saved_report(report_id: str) -> bool:
    with connect(write=True) as connection:
        result = connection.execute("DELETE FROM finance_saved_reports WHERE id=?", (report_id,))
        if result.rowcount:
            _audit(connection, "delete", "saved_report", report_id, "Deleted saved report")
        return bool(result.rowcount)


def _sweep_import_cache() -> None:
    cutoff = time.monotonic() - _IMPORT_TTL_SECONDS
    for token, (created, _) in list(_IMPORT_CACHE.items()):
        if created < cutoff:
            _IMPORT_CACHE.pop(token, None)


def prepare_csv_import(account_id: str, content: bytes) -> dict[str, Any]:
    if not get_account(account_id):
        raise ValueError("account not found")
    if len(content) > 2_000_000:
        raise ValueError("CSV must be 2 MB or smaller")
    try:
        text_content = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must use UTF-8 encoding") from exc
    reader = csv.DictReader(io.StringIO(text_content))
    normalized_headers = {str(header or "").strip().lower(): header for header in (reader.fieldnames or [])}
    required = {"date", "amount", "category"}
    if not required.issubset(normalized_headers):
        raise ValueError("CSV requires date, amount, and category columns; memo is optional")
    rows: list[dict[str, Any]] = []
    duplicate_count = 0
    with connect() as connection:
        existing_hashes = {
            str(row[0]) for row in connection.execute(
                "SELECT import_hash FROM finance_transactions WHERE import_hash IS NOT NULL"
            ).fetchall()
        }
    for index, raw in enumerate(reader, start=2):
        if len(rows) >= 1000:
            raise ValueError("CSV may contain at most 1,000 transactions")
        transaction_date = iso_date(raw.get(normalized_headers["date"]), f"row {index} date")
        amount_minor = to_minor(raw.get(normalized_headers["amount"]), f"row {index} amount")
        category = safe_label(raw.get(normalized_headers["category"]), f"row {index} category", max_length=60)
        memo_header = normalized_headers.get("memo")
        memo = safe_label(raw.get(memo_header) if memo_header else "", f"row {index} memo", required=False, max_length=120)
        digest = _transaction_hash(account_id, transaction_date, amount_minor, category, memo)
        duplicate = digest in existing_hashes or any(row["import_hash"] == digest for row in rows)
        if duplicate:
            duplicate_count += 1
        rows.append({
            "transaction_date": transaction_date,
            "amount_minor": amount_minor,
            "amount": from_minor(amount_minor),
            "category": category,
            "memo": memo,
            "import_hash": digest,
            "duplicate": duplicate,
        })
    if not rows:
        raise ValueError("CSV contains no transaction rows")
    token = new_id()
    payload = {"account_id": account_id, "rows": rows}
    with _IMPORT_LOCK:
        _sweep_import_cache()
        _IMPORT_CACHE[token] = (time.monotonic(), payload)
    return {"token": token, "rows": rows[:100], "total": len(rows), "duplicates": duplicate_count}


def commit_csv_import(token: str) -> dict[str, int]:
    with _IMPORT_LOCK:
        _sweep_import_cache()
        entry = _IMPORT_CACHE.pop(token, None)
    if not entry:
        raise ValueError("import preview expired; upload the CSV again")
    payload = entry[1]
    imported = 0
    skipped = 0
    for row in payload["rows"]:
        if row["duplicate"]:
            skipped += 1
            continue
        try:
            create_transaction({
                "account_id": payload["account_id"],
                "transaction_date": row["transaction_date"],
                "amount": row["amount"],
                "category": row["category"],
                "memo": row["memo"],
            }, imported=True)
            imported += 1
        except ValueError as exc:
            if "duplicate" in str(exc):
                skipped += 1
            else:
                raise
    return {"imported": imported, "skipped": skipped}


def transactions_csv(month: str | None = None) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["date", "account_alias", "amount", "currency", "category", "memo"])
    for row in list_transactions(month=month, limit=None):
        writer.writerow([
            row["transaction_date"], row["account_name"], row["amount"],
            row["currency"], row["category"], row.get("memo") or "",
        ])
    return output.getvalue()


def backup_payload() -> dict[str, Any]:
    init_db()
    tables = (
        "finance_accounts", "finance_transactions", "finance_budgets",
        "finance_holdings", "finance_recurring_items",
        "finance_net_worth_snapshots", "finance_saved_reports",
        "finance_audit_events",
    )
    with connect() as connection:
        data = {table: _rows(connection.execute(f"SELECT * FROM {table}")) for table in tables}
    return {
        "format": "luigi-finance-backup-v1",
        "generated_at": now_iso(),
        "currency": base_currency(),
        "tables": data,
    }


def _restore_uuid(value: Any, field: str, *, optional: bool = False) -> str | None:
    raw = str(value or "").strip()
    if optional and not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"backup contains an invalid {field}") from exc


def _restore_timestamp(value: Any, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise ValueError(f"backup contains an invalid {field}") from exc
    return parsed.isoformat(timespec="seconds")


def _restore_integer(value: Any, field: str) -> int:
    raw = str(value).strip()
    if not re.fullmatch(r"-?\d+", raw):
        raise ValueError(f"backup contains an invalid {field}")
    return int(raw)


def _validated_restore_row(table: str, row: dict[str, Any]) -> dict[str, Any]:
    restored = dict(row)
    restored["id"] = _restore_uuid(restored.get("id"), "row ID")
    for field in ("created_at", "updated_at"):
        if field in restored:
            restored[field] = _restore_timestamp(restored.get(field), field)

    label_fields = {
        "finance_accounts": (("name", "account alias", True, 60),),
        "finance_transactions": (
            ("category", "category", True, 60),
            ("memo", "memo", False, 120),
        ),
        "finance_budgets": (("category", "category", True, 60),),
        "finance_holdings": (("asset_name", "asset alias", False, 80),),
        "finance_recurring_items": (
            ("name", "recurring item alias", True, 80),
            ("category", "category", True, 60),
        ),
        "finance_saved_reports": (("name", "report name", True, 60),),
    }
    for field, display_name, required, max_length in label_fields.get(table, ()):
        restored[field] = safe_label(
            restored.get(field), display_name, required=required, max_length=max_length
        ) or None

    if table == "finance_accounts":
        if str(restored.get("account_type")) not in _ACCOUNT_TYPES:
            raise ValueError("backup contains an invalid account type")
        restored["currency"] = account_currency(restored.get("currency"))
        restored["opening_balance_minor"] = _restore_integer(
            restored.get("opening_balance_minor"), "opening balance"
        )
        restored["active"] = _restore_integer(restored.get("active"), "active flag")
        if restored["active"] not in {0, 1}:
            raise ValueError("backup contains an invalid active flag")
    elif table == "finance_transactions":
        restored["account_id"] = _restore_uuid(restored.get("account_id"), "account ID")
        restored["transaction_date"] = iso_date(restored.get("transaction_date"))
        restored["amount_minor"] = _restore_integer(restored.get("amount_minor"), "amount")
        if restored["amount_minor"] == 0:
            raise ValueError("backup contains a zero transaction")
        import_hash = str(restored.get("import_hash") or "").strip().lower()
        if import_hash and not re.fullmatch(r"[a-f0-9]{64}", import_hash):
            raise ValueError("backup contains an invalid import hash")
        restored["import_hash"] = import_hash or None
    elif table == "finance_budgets":
        restored["month"] = month_value(restored.get("month"))
        restored["limit_minor"] = _restore_integer(restored.get("limit_minor"), "budget limit")
        if restored["limit_minor"] <= 0:
            raise ValueError("backup contains a non-positive budget limit")
    elif table == "finance_holdings":
        restored["account_id"] = _restore_uuid(restored.get("account_id"), "account ID")
        symbol = safe_label(restored.get("symbol"), "symbol", max_length=16).upper()
        if not re.fullmatch(r"[A-Z0-9._-]{1,16}", symbol):
            raise ValueError("backup contains an invalid holding symbol")
        restored["symbol"] = symbol
        restored["as_of_date"] = iso_date(restored.get("as_of_date"), "as-of date")
        for field in ("quantity_micro", "cost_basis_minor", "market_value_minor"):
            restored[field] = _restore_integer(restored.get(field), field.replace("_", " "))
            if restored[field] < 0:
                raise ValueError(f"backup contains a negative {field.replace('_', ' ')}")
    elif table == "finance_recurring_items":
        restored["account_id"] = _restore_uuid(
            restored.get("account_id"), "account ID", optional=True
        )
        if str(restored.get("cadence")) not in _CADENCES:
            raise ValueError("backup contains an invalid cadence")
        restored["next_due_date"] = iso_date(restored.get("next_due_date"), "next due date")
        restored["amount_minor"] = _restore_integer(restored.get("amount_minor"), "amount")
        if restored["amount_minor"] == 0:
            raise ValueError("backup contains a zero recurring amount")
        restored["active"] = _restore_integer(restored.get("active"), "active flag")
        if restored["active"] not in {0, 1}:
            raise ValueError("backup contains an invalid active flag")
    elif table == "finance_net_worth_snapshots":
        restored["snapshot_date"] = iso_date(restored.get("snapshot_date"), "snapshot date")
        restored["total_minor"] = _restore_integer(restored.get("total_minor"), "snapshot total")
    elif table == "finance_saved_reports":
        report_type = str(restored.get("report_type") or "")
        if report_type not in {"monthly", "budgets", "investments", "net_worth"}:
            raise ValueError("backup contains an invalid report type")
        try:
            parameters = json.loads(str(restored.get("parameters_json") or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("backup contains invalid report parameters") from exc
        if not isinstance(parameters, dict) or set(parameters) - {"month"}:
            raise ValueError("backup contains unsupported report parameters")
        if "month" in parameters:
            parameters["month"] = month_value(parameters["month"])
        restored["parameters_json"] = json.dumps(parameters, sort_keys=True)
    return restored


def restore_backup(payload: dict[str, Any]) -> dict[str, int]:
    if payload.get("format") != "luigi-finance-backup-v1":
        raise ValueError("unsupported finance backup format")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("backup tables are missing")
    allowed_columns = {
        "finance_accounts": ("id", "name", "account_type", "currency", "opening_balance_minor", "active", "created_at", "updated_at"),
        "finance_transactions": ("id", "account_id", "transaction_date", "amount_minor", "category", "memo", "import_hash", "created_at", "updated_at"),
        "finance_budgets": ("id", "month", "category", "limit_minor", "created_at", "updated_at"),
        "finance_holdings": ("id", "account_id", "symbol", "asset_name", "quantity_micro", "cost_basis_minor", "market_value_minor", "as_of_date", "created_at", "updated_at"),
        "finance_recurring_items": ("id", "account_id", "name", "category", "amount_minor", "cadence", "next_due_date", "active", "created_at", "updated_at"),
        "finance_net_worth_snapshots": ("id", "snapshot_date", "total_minor", "created_at"),
        "finance_saved_reports": ("id", "name", "report_type", "parameters_json", "created_at"),
    }
    init_db()
    counts: dict[str, int] = {}
    with connect(write=True) as connection:
        for table, columns in allowed_columns.items():
            rows = tables.get(table, [])
            if not isinstance(rows, list):
                raise ValueError(f"{table} must be a list")
            inserted = 0
            placeholders = ",".join("?" for _ in columns)
            updates = ",".join(f"{column}=excluded.{column}" for column in columns if column != "id")
            sql = (
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}"
            )
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError(f"invalid row in {table}")
                restored = _validated_restore_row(table, row)
                connection.execute(sql, tuple(restored.get(column) for column in columns))
                inserted += 1
            counts[table] = inserted
        _audit(connection, "restore", "backup", None, "Restored finance backup")
    return counts
