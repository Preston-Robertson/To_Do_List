"""Private, JSON-only planning HTTP adapter; composition belongs to the host."""
from __future__ import annotations

import json
from decimal import DecimalException
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ...auth import require_auth, require_finance_auth
from ...core.templating import create_templates
from . import plans, projections
from . import repository as finance
from .overview_routes import PRIVATE_HEADERS, PrivateRoute, _require_private_post


templates = create_templates()
router = APIRouter(route_class=PrivateRoute, dependencies=[
    Depends(require_auth), Depends(require_finance_auth), Depends(_require_private_post),
])


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status, headers=PRIVATE_HEADERS)


def _fields(data: Any, allowed: set[str], required: set[str]) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) - allowed or required - set(data):
        raise ValueError("Invalid planning fields")
    return data


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate planning field")
        result[key] = value
    return result


def _not_integer(value: str):
    raise ValueError("Canonical integers required")


async def _body(request: Request) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 32768:
            raise HTTPException(413)
        body.extend(chunk)
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_object,
                          parse_float=_not_integer, parse_constant=_not_integer)
        pending = [(data, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 8:
                raise ValueError("Planning request too deep")
            if isinstance(value, dict):
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
        if not isinstance(data, dict):
            raise ValueError("Planning object required")
        return data
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise HTTPException(422) from None


def _call(operation: Callable, *arguments):
    try:
        return operation(*arguments)
    except plans.PlanConflict:
        return _error(409, "Record changed. Reload before trying again.")
    except (ValueError, TypeError, OverflowError, DecimalException):
        return _error(422, "Invalid planning request. Check the values and try again.")
    except Exception:
        return _error(503, "Finance planning is temporarily unavailable.")


def _save(data: dict[str, Any]):
    _fields(data, {"kind", "name", "config", "id", "expected_version"}, {"kind", "name", "config"})
    return {"record": plans.save_plan(data["kind"], data["name"], data["config"],
                                      data.get("id"), data.get("expected_version"))}


def _delete(data: dict[str, Any]):
    _fields(data, {"id", "expected_version"}, {"id", "expected_version"})
    plans.delete_plan(data["id"], data["expected_version"])
    return {"deleted": True}


def _save_income(data: dict[str, Any]):
    _fields(data, {"id", "expected_version", "name", "amount_minor", "cadence", "start_date",
                   "end_date", "annual_growth_bps", "active", "currency"},
            {"name", "amount_minor", "cadence", "start_date"})
    return {"record": plans.save_income(data)}


def _delete_income(data: dict[str, Any]):
    _fields(data, {"id", "expected_version"}, {"id", "expected_version"})
    plans.delete_income(data["id"], data["expected_version"])
    return {"deleted": True}


def _schedules() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    currency = finance.base_currency()
    active_accounts = {row["id"] for row in finance.list_accounts()
                       if row["active"] and row["currency"] == currency}
    recurring = [{key: row[key] for key in ("amount_minor", "cadence", "next_due_date", "active")}
                 for row in finance.list_recurring_items()
                 if row["currency"] == currency and (not row.get("account_id") or row["account_id"] in active_accounts)]
    streams = plans.list_income()
    return recurring, streams


def planning_state() -> dict[str, Any]:
    currency = finance.base_currency()
    accounts = finance.list_accounts()
    active = {row["id"] for row in accounts if row["currency"] == currency and row["active"]}
    cash = sum(row["balance_minor"] for row in accounts
               if row["id"] in active and row["account_type"] in {"checking", "savings", "cash"})
    holdings = sum(row["market_value_minor"] for row in finance.list_holdings() if row["account_id"] in active)
    defaults = plans.normalize_config("cashflow", {
        "opening_cash_minor": cash, "opening_investments_minor": holdings,
    })
    recurring, streams = _schedules()
    return {"currency": currency, "baseline_as_of": finance.clock.local_today().isoformat(),
            "defaults": defaults, "recurring": recurring, "income_streams": streams,
            "plans": plans.list_plans()}


def _state():
    try:
        return planning_state()
    except Exception:
        return _error(503, "Finance planning is temporarily unavailable.")


def _kind(data: dict[str, Any]) -> str:
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in {"cashflow", "wealth", "housing"}:
        raise ValueError("Invalid calculation kind")
    return kind


def _years(data: dict[str, Any], kind: str, default: int = 1) -> int:
    if "years" in data and kind != "wealth":
        raise ValueError("Years override is only for wealth")
    years = data.get("years", default)
    if type(years) is not int or not 1 <= years <= 50:
        raise ValueError("Invalid wealth horizon")
    return years


def _project(kind: str, config: dict[str, Any], years: int,
             recurring: list[dict[str, Any]], streams: list[dict[str, Any]]):
    if kind == "housing":
        return projections.housing_projection(config)
    if kind == "wealth":
        return projections.wealth_projection({**config, "recurring": recurring, "income_streams": streams}, years)
    return projections.forecast(config, recurring, streams)


def _calculate(data: dict[str, Any]):
    _fields(data, {"kind", "config", "years"}, {"kind", "config"})
    kind = _kind(data)
    config = plans.normalize_config("housing" if kind == "housing" else "cashflow", data["config"])
    years = _years(data, kind, config["years"])
    recurring, streams = ([], []) if kind == "housing" else _schedules()
    return {"kind": kind, "config": config, "result": _project(kind, config, years, recurring, streams)}


def _compare(data: dict[str, Any]):
    _fields(data, {"kind", "plan_ids", "configs", "years"}, {"kind"})
    kind = _kind(data)
    expected_kind = "housing" if kind == "housing" else "cashflow"
    if ("plan_ids" in data) == ("configs" in data):
        raise ValueError("Choose one comparison source")
    scenarios = []
    if "plan_ids" in data:
        identifiers = data["plan_ids"]
        if (not isinstance(identifiers, list) or not 2 <= len(identifiers) <= 3
                or any(not isinstance(identifier, str) for identifier in identifiers)
                or len(set(identifiers)) != len(identifiers)):
            raise ValueError("Choose two or three different plans")
        for identifier in identifiers:
            record = plans.get_plan(identifier)
            if record is None or record["kind"] != expected_kind:
                raise ValueError("Compare plans of the same kind")
            scenarios.append({"name": record["name"], "config": plans.normalize_config(expected_kind, record["config"])})
    else:
        configs = data["configs"]
        if not isinstance(configs, list) or len(configs) != 2:
            raise ValueError("Two configurations required")
        scenarios = [{"name": f"Scenario {index}", "config": plans.normalize_config(expected_kind, config)}
                     for index, config in enumerate(configs, 1)]
    years = _years(data, kind, scenarios[0]["config"]["years"])
    if kind == "cashflow" and len({(row["config"]["start_month"], row["config"]["months"])
                                  for row in scenarios}) != 1:
        raise ValueError("Cash-flow dates and horizons must match")
    if kind == "housing" and len({row["config"]["years"] for row in scenarios}) != 1:
        raise ValueError("Housing horizons must match")
    recurring, streams = ([], []) if kind == "housing" else _schedules()
    for scenario in scenarios:
        scenario["result"] = _project(kind, scenario["config"], years, recurring, streams)
    return {"kind": kind, "scenarios": scenarios}


@router.get("/finance/planning/state")
def get_state():
    return _state()


@router.get("/finance/planning")
def planning_page(request: Request):
    state = _state()
    if isinstance(state, JSONResponse):
        return state
    return templates.TemplateResponse("finance_planning.html", {
        "request": request, "active_nav": "finance", "page_title": "Finance planning", "planning_seed": state,
    })


@router.post("/finance/planning/calculate")
async def calculate(request: Request):
    return await run_in_threadpool(_call, _calculate, await _body(request))


@router.post("/finance/planning/compare")
async def compare(request: Request):
    return await run_in_threadpool(_call, _compare, await _body(request))


@router.post("/finance/planning/save")
async def save_plan(request: Request):
    return await run_in_threadpool(_call, _save, await _body(request))


@router.post("/finance/planning/delete")
async def delete_plan(request: Request):
    return await run_in_threadpool(_call, _delete, await _body(request))


@router.post("/finance/planning/income/save")
async def save_income(request: Request):
    return await run_in_threadpool(_call, _save_income, await _body(request))


@router.post("/finance/planning/income/delete")
async def delete_income(request: Request):
    return await run_in_threadpool(_call, _delete_income, await _body(request))