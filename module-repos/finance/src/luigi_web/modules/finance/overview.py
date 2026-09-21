"""Private, read-only Finance overview; amounts remain integer minor units."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ... import clock
from . import repository as finance
from .projections import add_months, scheduled_dates


def _flows(row: Any) -> dict[str, int]:
    inflow = int(row["inflow_minor"] or 0)
    outflow = int(row["outflow_minor"] or 0)
    return {"inflow_minor": inflow, "outflow_minor": outflow,
            "net_minor": inflow - outflow, "count": int(row["count"])}


def _comparison(current: int, previous: int) -> dict[str, int | None]:
    return {"previous_minor": previous, "change_minor": current - previous,
            "change_bps": ((current - previous) * 10000 // abs(previous)) if previous else None}


def overview_state(month: str | None = None, *, account_id: str = "",
                   category: str = "", query: str = "", include_memo: bool = False,
                   page: int = 1, page_size: int = 50, status: str = "all") -> dict[str, Any]:
    """Return shell-ready context without recording filters or changing ledger data."""
    selected = finance.month_value(month)
    start = date.fromisoformat(selected + "-01")
    if not 2 <= start.year <= 9998:
        raise ValueError("Invalid overview filters")
    if (not isinstance(account_id, str) or len(account_id) > 64
            or not isinstance(category, str) or len(category) > 60
            or not isinstance(query, str) or len(query) > 120
            or type(page) is not int or not 1 <= page <= 1000000
            or type(page_size) is not int or page_size < 1
            or type(include_memo) is not bool
            or status not in {"all", "inflows", "outflows", "zero"}):
        raise ValueError("Invalid overview filters")
    page_size = min(page_size, 200)
    end = add_months(start, 1)
    today = clock.local_today()
    currency = finance.base_currency()
    month_starts = [add_months(start, offset) for offset in range(-11, 1)]
    windows = [(day.isoformat()[:7], day.isoformat(), add_months(day, 1).isoformat())
               for day in month_starts]
    finance.init_db()
    with finance.connect() as connection:
        connection.execute("BEGIN")
        accounts = [dict(row) for row in connection.execute("""
            SELECT a.id, a.name, a.account_type, a.currency, a.active,
                   a.opening_balance_minor + COALESCE(SUM(t.amount_minor), 0) AS balance_minor
            FROM finance_accounts a LEFT JOIN finance_transactions t ON t.account_id = a.id
            GROUP BY a.id ORDER BY a.active DESC, a.name COLLATE NOCASE, a.id
        """)]
        if account_id and not any(row["id"] == account_id and row["currency"] == currency for row in accounts):
            raise ValueError("Invalid overview filters")
        trend = [dict(row) for row in connection.execute(f"""
            WITH months(month, start_date, end_date) AS (VALUES {','.join(['(?, ?, ?)'] * 12)})
            SELECT months.month,
                   COALESCE(SUM(CASE WHEN a.id IS NOT NULL AND t.amount_minor > 0 THEN t.amount_minor ELSE 0 END), 0) AS inflow_minor,
                   COALESCE(SUM(CASE WHEN a.id IS NOT NULL AND t.amount_minor < 0 THEN -t.amount_minor ELSE 0 END), 0) AS outflow_minor,
                   COUNT(a.id) AS count
            FROM months LEFT JOIN finance_transactions t
              ON t.transaction_date >= months.start_date AND t.transaction_date < months.end_date
            LEFT JOIN finance_accounts a ON a.id = t.account_id AND a.currency = ?
            GROUP BY months.month ORDER BY months.month
        """, (*[value for window in windows for value in window], currency))]
        for row in trend:
            row.update(_flows(row))
        categories = [dict(row) for row in connection.execute("""
            SELECT MIN(t.category) AS category, lower(t.category) AS category_key,
                   SUM(CASE WHEN t.amount_minor < 0 THEN -t.amount_minor ELSE 0 END) AS outflow_minor,
                   COUNT(*) AS count
            FROM finance_transactions t JOIN finance_accounts a ON a.id = t.account_id
            WHERE t.transaction_date >= ? AND t.transaction_date < ? AND a.currency = ?
            GROUP BY lower(t.category) ORDER BY outflow_minor DESC, category COLLATE NOCASE
        """, (start.isoformat(), end.isoformat(), currency))]
        where = ["t.transaction_date >= ?", "t.transaction_date < ?", "a.currency = ?"]
        parameters: list[Any] = [start.isoformat(), end.isoformat(), currency]
        if account_id:
            where.append("t.account_id = ?")
            parameters.append(account_id)
        if category:
            where.append("lower(t.category) = lower(?)")
            parameters.append(category)
        if query:
            search = ["instr(lower(t.category), lower(?)) > 0", "instr(lower(a.name), lower(?)) > 0"]
            parameters.extend([query, query])
            if include_memo:
                search.append("instr(lower(COALESCE(t.memo, '')), lower(?)) > 0")
                parameters.append(query)
            where.append("(" + " OR ".join(search) + ")")
        if status != "all":
            where.append({"inflows": "t.amount_minor > 0", "outflows": "t.amount_minor < 0",
                          "zero": "t.amount_minor = 0"}[status])
        predicate = " AND ".join(where)
        filtered = _flows(connection.execute(f"""
            SELECT COUNT(*) AS count,
                   COALESCE(SUM(CASE WHEN t.amount_minor > 0 THEN t.amount_minor ELSE 0 END), 0) AS inflow_minor,
                   COALESCE(SUM(CASE WHEN t.amount_minor < 0 THEN -t.amount_minor ELSE 0 END), 0) AS outflow_minor
            FROM finance_transactions t JOIN finance_accounts a ON a.id = t.account_id
            WHERE {predicate}
        """, parameters).fetchone())
        pages = max(1, (filtered["count"] + page_size - 1) // page_size)
        page = min(page, pages)
        transactions = [dict(row) for row in connection.execute(f"""
            SELECT t.id, t.transaction_date, t.amount_minor, t.category, t.memo,
                   a.name AS account_name, a.currency
            FROM finance_transactions t JOIN finance_accounts a ON a.id = t.account_id
            WHERE {predicate}
            ORDER BY t.transaction_date DESC, t.created_at DESC, t.id DESC LIMIT ? OFFSET ?
        """, (*parameters, page_size, (page - 1) * page_size))]
        budgets = [dict(row) for row in connection.execute("""
            SELECT category, lower(category) AS category_key, limit_minor FROM finance_budgets WHERE month = ?
            ORDER BY category COLLATE NOCASE, id
        """, (selected,))]
        holdings = [dict(row) for row in connection.execute("""
            SELECT h.symbol, h.market_value_minor, h.as_of_date, a.id AS account_id, a.name AS account_name
            FROM finance_holdings h JOIN finance_accounts a ON a.id = h.account_id
            WHERE a.active = 1 AND a.currency = ?
            ORDER BY h.market_value_minor DESC, h.symbol, a.id
        """, (currency,))]
        recurring = [dict(row) for row in connection.execute("""
            SELECT r.id, r.name, r.category, r.amount_minor, r.cadence, r.next_due_date, r.active,
                   a.name AS account_name
            FROM finance_recurring_items r LEFT JOIN finance_accounts a ON a.id = r.account_id
            WHERE r.active = 1 AND (r.account_id IS NULL OR (a.active = 1 AND a.currency = ?))
            ORDER BY r.next_due_date, r.id
        """, (currency,))]
        snapshots = [dict(row) for row in connection.execute("""
            SELECT snapshot_date, total_minor FROM finance_net_worth_snapshots
            WHERE snapshot_date >= ? AND snapshot_date < ?
            ORDER BY snapshot_date
        """, (month_starts[0].isoformat(), end.isoformat()))]
        foreign_activity = bool(connection.execute("""
            SELECT 1 FROM finance_transactions t JOIN finance_accounts a ON a.id = t.account_id
            WHERE t.transaction_date >= ? AND t.transaction_date < ? AND a.currency != ? LIMIT 1
        """, (month_starts[0].isoformat(), end.isoformat(), currency)).fetchone())
    active = [row for row in accounts if row["active"] and row["currency"] == currency]
    for account in accounts:
        account["excluded_currency"] = account["currency"] != currency
    holding_total = sum(row["market_value_minor"] for row in holdings)
    for holding in holdings:
        holding["stale"] = date.fromisoformat(holding["as_of_date"]) < today - timedelta(days=7)
    spent = {row["category_key"]: row["outflow_minor"] for row in categories}
    for budget in budgets:
        budget["spent_minor"] = spent.get(budget["category_key"], 0)
        budget["remaining_minor"] = budget["limit_minor"] - budget["spent_minor"]
        budget["overspend_minor"] = max(0, -budget["remaining_minor"])
    upcoming = []
    overdue = []
    invalid_schedule_count = 0
    for item in recurring:
        try:
            due = date.fromisoformat(item["next_due_date"])
            occurrences = scheduled_dates(item, today, today + timedelta(days=29))
        except (ValueError, OverflowError, KeyError, TypeError):
            invalid_schedule_count += 1
            continue
        if due < today:
            overdue.append(item)
        upcoming.extend({**item, "scheduled_date": day.isoformat()} for day in occurrences)
    upcoming.sort(key=lambda row: (row["scheduled_date"], row["id"]))
    allocation_accounts: dict[str, dict[str, Any]] = {}
    allocation_symbols: dict[str, int] = {}
    for holding in holdings:
        entry = allocation_accounts.setdefault(holding["account_id"], {
            "label": holding["account_name"], "value_minor": 0})
        entry["value_minor"] += holding["market_value_minor"]
        symbol = holding["symbol"]
        allocation_symbols[symbol] = allocation_symbols.get(symbol, 0) + holding["market_value_minor"]
    current, previous = trend[-1], trend[-2]
    return {
        "month": selected, "today": today.isoformat(), "currency": currency,
        "filters": {"account_id": account_id, "category": category, "query": query,
                    "include_memo": include_memo, "status": status},
        "page": page, "page_size": page_size, "pages": pages,
        "accounts": accounts, "transactions": transactions, "filtered": filtered,
        "month_totals": _flows(current), "previous_month": previous["month"],
        "comparison": {key: _comparison(current[key], previous[key]) for key in
                       ("inflow_minor", "outflow_minor", "net_minor")},
        "trend": trend, "categories": categories, "budgets": budgets,
        "net_worth_minor": sum(row["balance_minor"] for row in active) + holding_total,
        "liquid_cash_minor": sum(row["balance_minor"] for row in active
                                 if row["account_type"] in {"checking", "savings", "cash"}),
        "liabilities_minor": -sum(min(0, row["balance_minor"]) for row in active
                                  if row["account_type"] == "credit"),
        "net_credit_balance_minor": sum(row["balance_minor"] for row in active if row["account_type"] == "credit"),
        "investment_cash_minor": sum(row["balance_minor"] for row in active if row["account_type"] == "investment"),
        "holdings_minor": holding_total, "holdings": holdings,
        "stale_holdings_count": sum(row["stale"] for row in holdings),
        "allocation_accounts": list(allocation_accounts.values()),
        "allocation_symbols": [{"label": symbol, "value_minor": value} for symbol, value in allocation_symbols.items()],
        "upcoming": upcoming, "overdue": overdue, "invalid_schedule_count": invalid_schedule_count,
        "upcoming_inflow_minor": sum(max(0, row["amount_minor"]) for row in upcoming),
        "upcoming_outflow_minor": -sum(min(0, row["amount_minor"]) for row in upcoming),
        "upcoming_end": (today + timedelta(days=29)).isoformat(),
        "snapshots": snapshots, "snapshot_currency_known": False,
        "foreign_currency_excluded": foreign_activity or any(row["excluded_currency"] for row in accounts),
    }