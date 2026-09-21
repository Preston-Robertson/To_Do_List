"""Deterministic planning math; forecasts never mutate recorded finances."""
from __future__ import annotations

from calendar import monthrange
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Context, Decimal, ROUND_HALF_UP, localcontext
from typing import Any


def add_months(anchor: date, months: int) -> date:
    index = anchor.year * 12 + anchor.month - 1 + months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    return date(year, month, min(anchor.day, monthrange(year, month)[1]))


def scheduled_dates(item: dict[str, Any], start: date, end: date) -> list[date]:
    """Project from the original next-due anchor without rewriting overdue records."""
    if end < start or (end - start).days > 366 * 50:
        raise ValueError("Invalid projection window")
    if not int(item.get("active", 1)):
        return []
    anchor = date.fromisoformat(str(item["next_due_date"]))
    cadence = item["cadence"]
    if cadence not in {"weekly", "monthly", "quarterly", "yearly"}:
        raise ValueError("Invalid planning cadence")
    if cadence == "weekly":
        offset = max(0, ((start - anchor).days + 6) // 7)
        current = anchor + timedelta(days=offset * 7)
        result = []
        while current <= end:
            result.append(current)
            current += timedelta(days=7)
        return result
    step = {"monthly": 1, "quarterly": 3, "yearly": 12}[cadence]
    offset = max(0, ((start.year - anchor.year) * 12 + start.month - anchor.month) // step)
    result = []
    while True:
        current = add_months(anchor, offset * step)
        if current > end:
            return result
        if current >= start:
            result.append(current)
        offset += 1


def scheduled_months(items: list[dict[str, Any]], start: date, months: int = 12) -> list[dict[str, Any]]:
    if type(months) is not int or not 1 <= months <= 600 or start.day != 1:
        raise ValueError("Use one to six hundred complete calendar months")
    end = add_months(start, months) - timedelta(days=1)
    rows = {add_months(start, index).strftime("%Y-%m"): {
        "month": add_months(start, index).strftime("%Y-%m"),
        "income_minor": 0, "expense_minor": 0, "occurrences": 0,
    } for index in range(months)}
    for item in items:
        amount = item["amount_minor"]
        if type(amount) is not int or abs(amount) > 10**14:
            raise ValueError("Invalid scheduled amount")
        for day in scheduled_dates(item, start, end):
            row = rows[day.strftime("%Y-%m")]
            row["income_minor" if amount >= 0 else "expense_minor"] += abs(amount)
            row["occurrences"] += 1
    return list(rows.values())


_MAX_MINOR = 10**14
_BASIS_POINTS = Decimal(10000)


def _integer(values: dict[str, Any], key: str, default: int = 0,
             minimum: int = 0, maximum: int = _MAX_MINOR) -> int:
    value = values.get(key, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid projection integer or range")
    return value


def _minor(value: Decimal | int) -> int:
    amount = int(Decimal(value).to_integral_value(rounding=ROUND_HALF_UP))
    if abs(amount) > _MAX_MINOR:
        raise ValueError("Projection amount exceeds supported range")
    return amount


def _factor(rate_bps: int) -> Decimal:
    return Decimal(1) + Decimal(rate_bps) / _BASIS_POINTS


def _monthly_factor(rate_bps: int) -> Decimal:
    return _factor(rate_bps) ** (Decimal(1) / Decimal(12))


def _iso_date(value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError("Invalid projection date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("Invalid projection date") from None
    if parsed.isoformat() != value:
        raise ValueError("Invalid projection date")
    return parsed


def _start_month(config: dict[str, Any], months: int) -> date:
    value = config.get("start_month")
    if "start_month" not in config:
        start = date.today().replace(day=1)
        offset = 1
    else:
        if not isinstance(value, str) or len(value) != 7:
            raise ValueError("Invalid projection month")
        start = _iso_date(value + "-01")
        offset = 0
    try:
        start = add_months(start, offset)
        add_months(start, months)
    except (ValueError, OverflowError):
        raise ValueError("Invalid projection window") from None
    return start


def _items(values: Any) -> Sequence[dict[str, Any]]:
    if not isinstance(values, (list, tuple)) or any(not isinstance(item, dict) for item in values):
        raise ValueError("Invalid projection schedule")
    return values


def _active(item: dict[str, Any]) -> bool:
    value = item.get("active", True)
    if type(value) not in (bool, int) or value not in (0, 1):
        raise ValueError("Invalid projection activity flag")
    return bool(value)


def _recurring_months(items: Sequence[dict[str, Any]], start: date,
                      months: int, include_income: bool) -> list[dict[str, Any]]:
    selected = []
    for item in _items(items):
        if not _active(item):
            continue
        if "amount_minor" not in item:
            raise ValueError("Missing scheduled amount")
        amount = _integer(item, "amount_minor", minimum=-_MAX_MINOR)
        if amount >= 0 and not include_income:
            continue
        cadence = item.get("cadence")
        if cadence not in ("weekly", "monthly", "quarterly", "yearly"):
            raise ValueError("Invalid planning cadence")
        anchor = _iso_date(item.get("next_due_date"))
        selected.append({"amount_minor": amount, "cadence": cadence,
                         "next_due_date": anchor.isoformat(), "active": True})
    try:
        return scheduled_months(selected, start, months)
    except (ValueError, OverflowError):
        raise ValueError("Invalid projection schedule") from None


def _stream_dates(stream: dict[str, Any], start: date, end: date) -> list[date]:
    anchor = _iso_date(stream.get("start_date"))
    stop = stream.get("end_date")
    if stop is not None:
        stop = _iso_date(stop)
        if stop < anchor:
            raise ValueError("Invalid income stream window")
        end = min(end, stop)
    cadence = stream.get("cadence")
    if cadence not in ("weekly", "biweekly", "monthly", "quarterly", "yearly"):
        raise ValueError("Invalid income stream cadence")
    if end < start or end < anchor:
        return []
    if cadence == "biweekly":
        first = max(0, ((start - anchor).days + 13) // 14)
        last = (end - anchor).days // 14
        return [anchor + timedelta(days=index * 14) for index in range(first, last + 1)]
    try:
        return scheduled_dates({"next_due_date": anchor.isoformat(), "cadence": cadence}, start, end)
    except (ValueError, OverflowError):
        raise ValueError("Invalid income stream window") from None


def _stream_months(streams: Sequence[dict[str, Any]], start: date, months: int) -> list[int]:
    income = [0] * months
    end = add_months(start, months) - timedelta(days=1)
    for stream in _items(streams):
        if not _active(stream):
            continue
        if "amount_minor" not in stream:
            raise ValueError("Missing income stream amount")
        amount = _integer(stream, "amount_minor")
        growth = _integer(stream, "annual_growth_bps", maximum=3000)
        anchor = _iso_date(stream.get("start_date"))
        for occurrence in _stream_dates(stream, start, end):
            elapsed_years = occurrence.year - anchor.year
            if occurrence < add_months(anchor, elapsed_years * 12):
                elapsed_years -= 1
            month_index = (occurrence.year - start.year) * 12 + occurrence.month - start.month
            income[month_index] = _minor(income[month_index] + _minor(
                Decimal(amount) * _factor(growth) ** elapsed_years))
    return income


def _compute_forecast(config: dict[str, Any], recurring: Sequence[dict[str, Any]],
                      income_streams: Sequence[dict[str, Any]], maximum_months: int) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("Invalid projection config")
    months = _integer(config, "months", 12, 1, maximum_months)
    start = _start_month(config, months)
    mode = config.get("income_mode", "recurring")
    if mode not in ("recurring", "fixed", "streams"):
        raise ValueError("Invalid income mode")
    cash = _integer(config, "opening_cash_minor", minimum=-_MAX_MINOR)
    investments = _integer(config, "opening_investments_minor")
    net_income = _integer(config, "net_income_minor")
    variable = _integer(config, "variable_expense_minor")
    contribution = _integer(config, "monthly_contribution_minor")
    reserve = _integer(config, "reserve_minor")
    income_growth = _integer(config, "annual_income_growth_bps", maximum=3000)
    expense_growth = _integer(config, "annual_expense_growth_bps", maximum=3000)
    annual_return = _integer(config, "annual_return_bps", minimum=-5000, maximum=3000)
    inflation = _integer(config, "inflation_bps", maximum=2000)
    scheduled = _recurring_months(recurring, start, months, mode == "recurring")
    totals: dict[str, Any] = {
        "income_minor": 0, "outflow_minor": 0, "contributions_minor": 0,
        "opening_cash_minor": cash, "opening_investments_minor": investments,
        "starting_net_worth_minor": _minor(cash + investments),
        "min_cash_minor": cash, "first_shortfall_month": None,
        "first_below_reserve_month": None,
    }
    rows = []
    with localcontext(Context(prec=50, rounding=ROUND_HALF_UP)):
        stream_income = _stream_months(income_streams, start, months) if mode == "streams" else []
        investment_factor = _monthly_factor(annual_return)
        inflation_factor = _factor(inflation)
        for index, scheduled_row in enumerate(scheduled):
            month_label = add_months(start, index).isoformat()[:7]
            year_index = index // 12
            if mode == "streams":
                income = stream_income[index]
            else:
                base_income = net_income if mode == "fixed" else scheduled_row["income_minor"]
                income = _minor(Decimal(base_income) * _factor(income_growth) ** year_index)
            expense_factor = _factor(expense_growth) ** year_index
            bills = _minor(Decimal(scheduled_row["expense_minor"]) * expense_factor)
            spending = _minor(Decimal(variable) * expense_factor)
            surplus = _minor(income - bills - spending)
            cash = _minor(cash + surplus - contribution)
            investments = _minor(Decimal(investments) * investment_factor + contribution)
            net_worth = _minor(cash + investments)
            rows.append({
                "month": month_label, "income_minor": income,
                "bills_minor": bills, "variable_minor": spending,
                "contribution_minor": contribution, "surplus_minor": surplus,
                "ending_cash_minor": cash, "investment_value_minor": investments,
                "net_worth_minor": net_worth,
                "real_net_worth_minor": _minor(Decimal(net_worth) /
                                              inflation_factor ** (Decimal(index + 1) / Decimal(12))),
                "below_reserve": cash < reserve, "funding_shortfall": cash < 0,
            })
            totals["income_minor"] = _minor(totals["income_minor"] + income)
            totals["outflow_minor"] = _minor(totals["outflow_minor"] + bills + spending)
            totals["contributions_minor"] = _minor(totals["contributions_minor"] + contribution)
            totals["min_cash_minor"] = min(totals["min_cash_minor"], cash)
            if cash < 0 and totals["first_shortfall_month"] is None:
                totals["first_shortfall_month"] = month_label
            if cash < reserve and totals["first_below_reserve_month"] is None:
                totals["first_below_reserve_month"] = month_label
    totals.update({key: rows[-1][key] for key in (
        "ending_cash_minor", "investment_value_minor", "net_worth_minor", "real_net_worth_minor")})
    totals["funding_shortfall"] = totals["min_cash_minor"] < 0
    warnings = []
    if totals["income_minor"] == 0:
        warnings.append("No positive net income is scheduled; missing income is unknown, not verified zero.")
    if totals["funding_shortfall"]:
        warnings.append("Negative cash indicates unfunded spending or transfers; no borrowing cost is modeled.")
    return {"start_month": start.isoformat()[:7], "months": months, "rows": rows,
            "totals": totals, "warnings": warnings, "assumptions": [
                "Opening balances precede the first projected month; no actual transactions or budgets are loaded.",
                "Income is explicit net pay; fixed or streams mode replaces positive recurring income.",
                "Bills are positive costs from negative recurring items; variable spending is additional.",
                "Surplus is income less spending before transfers; contributions move cash to investments at month end.",
                "Income and expense growth step every twelve projected months; streams step on start-date anniversaries.",
                "Stream growth replaces general income growth; contributions and reserve remain fixed.",
                "Returns and inflation use effective monthly factors; cash earns no interest.",
                "Money is rounded half up to minor units monthly (stream pay per occurrence); outputs are bounded.",
                "Real values are in opening-month purchasing power; returns are scenarios, not guarantees.",
                "Reserve and shortfall flags use month-end cash, not intramonth payment timing.",
            ]}


def forecast(config: dict[str, Any], recurring: list[dict[str, Any]],
             income_streams: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    """Return 1..60 monthly rows, totals, warnings and assumptions without I/O.

    Defaults to twelve months starting next month, zero opening balances and
    recurring net income. Costs are positive; surplus excludes contributions.
    Totals exclude transfers from outflow. Shortfall means cash below zero,
    separately from the configured reserve. All monetary results are integers.

    Inputs and monetary outputs are bounded to abs(value) <= 10**14. Growth is
    0..3000 bps, return -5000..3000 bps, and inflation 0..2000 bps. Streams use
    nonnegative net pay per occurrence, inclusive end dates, and their original
    start-date anchors. Minimum cash includes the opening balance; first flagged
    months refer to month ends only. No intramonth liquidity is inferred.
    """
    return _compute_forecast(config, recurring, income_streams, 60)


def wealth_projection(config: dict[str, Any], years: int = 10) -> dict[str, Any]:
    """Return year zero plus 1..50 year-end rows using the monthly forecast engine.

    Accepts forecast config plus optional ``recurring`` and ``income_streams``
    lists. ``years`` replaces ``months``. Contributions are cumulative transfers;
    annual contributions and minimum cash expose funding needs within each year.
    Compare scenarios by calling separately with independent configurations.
    """
    if not isinstance(config, dict):
        raise ValueError("Invalid projection config")
    years = _integer({"years": years}, "years", 10, 1, 50)
    monthly = _compute_forecast({**config, "months": years * 12},
                                config.get("recurring", ()), config.get("income_streams", ()), 600)
    totals = monthly["totals"]
    opening_cash = totals["opening_cash_minor"]
    rows = [{
        "year": 0, "cash_minor": opening_cash,
        "investment_value_minor": totals["opening_investments_minor"],
        "net_worth_minor": totals["starting_net_worth_minor"],
        "real_net_worth_minor": totals["starting_net_worth_minor"],
        "contributions_minor": 0, "annual_contributions_minor": 0,
        "min_cash_minor": opening_cash,
        "below_reserve": opening_cash < config.get("reserve_minor", 0),
        "funding_shortfall": opening_cash < 0,
    }]
    cumulative_contributions = 0
    for year in range(1, years + 1):
        annual = monthly["rows"][(year - 1) * 12:year * 12]
        ending = annual[-1]
        annual_contributions = _minor(sum(row["contribution_minor"] for row in annual))
        cumulative_contributions = _minor(cumulative_contributions + annual_contributions)
        minimum_cash = min(rows[-1]["cash_minor"], *(row["ending_cash_minor"] for row in annual))
        rows.append({
            "year": year, "cash_minor": ending["ending_cash_minor"],
            "investment_value_minor": ending["investment_value_minor"],
            "net_worth_minor": ending["net_worth_minor"],
            "real_net_worth_minor": ending["real_net_worth_minor"],
            "contributions_minor": cumulative_contributions,
            "annual_contributions_minor": annual_contributions,
            "min_cash_minor": minimum_cash,
            "below_reserve": minimum_cash < config.get("reserve_minor", 0),
            "funding_shortfall": minimum_cash < 0,
        })
    return {"start_month": monthly["start_month"], "years": years, "rows": rows,
            "totals": totals, "warnings": monthly["warnings"],
            "assumptions": monthly["assumptions"] + [
                "Year zero is the opening position; yearly rows use the same rounded monthly forecast math.",
                "Yearly contributions are cumulative transfers, not investment gains or new income.",
                "Yearly minimum cash and flags include the opening balance and all month ends in that year.",
                "Independent scenario comparisons are not probability bands or guaranteed outcomes.",
            ]}


def housing_projection(config: dict[str, Any]) -> dict[str, Any]:
    """Return fixed-rate amortization and annual rent/ownership cash costs.

    Rows cover years 1..50, with annual rent/owner costs and explicit cumulative
    counters. Total cash out includes the initial down payment and closing costs.
    Principal builds equity, not consumption. No rental portfolio is assumed;
    investment_return_bps is validated and disclosed but not applied to costs.
    """
    if not isinstance(config, dict):
        raise ValueError("Invalid projection config")
    years = _integer(config, "years", 10, 1, 50)
    term_years = _integer(config, "term_years", 30, 1, 50)
    rent = _integer(config, "rent_minor")
    rent_growth = _integer(config, "rent_growth_bps", maximum=3000)
    home_price = _integer(config, "home_price_minor", minimum=1)
    down_payment = _integer(config, "down_payment_minor", maximum=home_price)
    mortgage_rate = _integer(config, "mortgage_rate_bps", maximum=3000)
    property_tax_rate = _integer(config, "annual_property_tax_bps", maximum=10000)
    insurance = _integer(config, "annual_insurance_minor")
    maintenance_rate = _integer(config, "annual_maintenance_bps", maximum=10000)
    hoa = _integer(config, "hoa_minor")
    closing_cost = _integer(config, "closing_cost_minor")
    appreciation = _integer(config, "appreciation_bps", minimum=-5000, maximum=3000)
    investment_return = _integer(config, "investment_return_bps", minimum=-5000, maximum=3000)
    initial_cash = _minor(down_payment + closing_cost)
    balance = home_price - down_payment
    term_months = term_years * 12
    cumulative_rent = 0
    cumulative_owner = 0
    cumulative_principal = 0
    cumulative_interest = 0
    rows = []
    with localcontext(Context(prec=50, rounding=ROUND_HALF_UP)):
        monthly_rate = Decimal(mortgage_rate) / (_BASIS_POINTS * 12)
        if not balance:
            payment = 0
        elif not monthly_rate:
            payment = _minor(Decimal(balance) / term_months)
        else:
            payment = _minor(Decimal(balance) * monthly_rate /
                             (Decimal(1) - (Decimal(1) + monthly_rate) ** -term_months))
        for year in range(1, years + 1):
            principal_paid = 0
            interest_paid = 0
            for month_index in range((year - 1) * 12, year * 12):
                if balance == 0:
                    break
                interest = _minor(Decimal(balance) * monthly_rate)
                due = balance + interest
                current_payment = due if month_index == term_months - 1 else min(payment, due)
                principal = current_payment - interest
                balance -= principal
                principal_paid = _minor(principal_paid + principal)
                interest_paid = _minor(interest_paid + interest)
            beginning_value = Decimal(home_price) * _factor(appreciation) ** (year - 1)
            home_value = _minor(Decimal(home_price) * _factor(appreciation) ** year)
            property_tax = _minor(beginning_value * Decimal(property_tax_rate) / _BASIS_POINTS)
            maintenance = _minor(beginning_value * Decimal(maintenance_rate) / _BASIS_POINTS)
            annual_hoa = _minor(hoa * 12)
            rent_cost = _minor(_minor(Decimal(rent) * _factor(rent_growth) ** (year - 1)) * 12)
            owner_cost = _minor(principal_paid + interest_paid + property_tax + insurance + maintenance + annual_hoa)
            cumulative_rent = _minor(cumulative_rent + rent_cost)
            cumulative_owner = _minor(cumulative_owner + owner_cost)
            cumulative_principal = _minor(cumulative_principal + principal_paid)
            cumulative_interest = _minor(cumulative_interest + interest_paid)
            rows.append({
                "year": year, "rent_cost_minor": rent_cost,
                "cumulative_rent_cost_minor": cumulative_rent,
                "owner_cost_minor": owner_cost, "cumulative_owner_cost_minor": cumulative_owner,
                "owner_consumption_minor": owner_cost - principal_paid,
                "principal_paid_minor": principal_paid, "interest_paid_minor": interest_paid,
                "cumulative_principal_paid_minor": cumulative_principal,
                "cumulative_interest_paid_minor": cumulative_interest,
                "property_tax_minor": property_tax, "insurance_minor": insurance,
                "maintenance_minor": maintenance, "hoa_minor": annual_hoa,
                "mortgage_balance_minor": balance, "home_value_minor": home_value,
                "equity_minor": _minor(home_value - balance),
                "total_cash_out_minor": _minor(initial_cash + cumulative_owner),
            })
    return {"monthly_payment_minor": payment, "initial_cash_minor": initial_cash,
            "years": years, "rows": rows, "investment_return_bps": investment_return,
            "rental_portfolio_modeled": False, "assumptions": [
                "Purchase occurs at the start; initial cash is down payment plus closing cost.",
                "Mortgage APR is nominal, divided by twelve; the fixed payment covers principal and interest only.",
                "Monthly interest and payment round half up to minor units; the final payment clears rounding residuals.",
                "Owner cost is annual cash outflow including principal; principal builds equity rather than consumption.",
                "Total cash out is cumulative ownership cost plus initial cash; cumulative rent is a separate alternative.",
                "Property tax and maintenance use beginning-of-year scenario home value, not a verified tax assessment.",
                "Rent growth steps annually; insurance and HOA stay fixed; unspecified costs are zero assumptions.",
                "Equity is home value minus mortgage balance, not sale proceeds or spendable cash.",
                "No tax deductions, PMI, sale costs, sale income or borrowing beyond the initial mortgage are modeled.",
                "No rental portfolio or opportunity returns are modeled; investment_return_bps does not change cash costs.",
                "Cash outlay alone cannot establish which alternative is more profitable; appreciation is not guaranteed.",
            ]}