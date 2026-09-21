"""Jinja helpers for trading-card pages."""
from __future__ import annotations

import html
import json
import re
from typing import Any

_MANA_RE = re.compile(r"\{([^{}]{1,12})\}")


def parse_json(text: str | None) -> Any:
    if not text:
        return []
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return []


def format_mana(mana_cost: str | None) -> str:
    if not mana_cost:
        return ""
    pips: list[str] = []
    for symbol in _MANA_RE.findall(str(mana_cost)):
        css_symbol = re.sub(r"[^a-z0-9]+", "-", symbol.lower()).strip("-") or "generic"
        pips.append(
            f'<span class="card-mana card-mana-{css_symbol}">{html.escape(symbol)}</span>'
        )
    return "".join(pips)


def money_minor(value: Any, currency: str = "USD") -> str:
    if value is None:
        return "-"
    try:
        minor = int(value)
    except (TypeError, ValueError):
        return "-"
    code = str(currency or "USD").upper()
    symbol = {"USD": "$", "EUR": "€"}.get(code)
    amount = f"{minor / 100:,.2f}"
    return f"{symbol}{amount}" if symbol else f"{amount} {html.escape(code)}"


def signed_money_minor(value: Any, currency: str = "USD") -> str:
    if value is None:
        return "-"
    try:
        minor = int(value)
    except (TypeError, ValueError):
        return "-"
    sign = "+" if minor > 0 else "-" if minor < 0 else ""
    return f"{sign}{money_minor(abs(minor), currency)}"


def percent_basis_points(value: Any) -> str:
    if value is None:
        return "-"
    try:
        basis_points = int(value)
    except (TypeError, ValueError):
        return "-"
    sign = "+" if basis_points > 0 else ""
    return f"{sign}{basis_points / 100:.2f}%"


def register_filters(environment: Any) -> None:
    environment.filters["card_json"] = parse_json
    environment.filters["card_mana"] = format_mana
    environment.filters["money_minor"] = money_minor
    environment.filters["signed_money_minor"] = signed_money_minor
    environment.filters["percent_basis_points"] = percent_basis_points