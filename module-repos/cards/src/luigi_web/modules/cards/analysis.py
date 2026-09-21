"""Offline, read-only deck snapshots. No physical reservations or rules enforcement.

Public functions return JSON-compatible dictionaries with schema_version=1.
Quantities are copies, prices are nullable integer USD minor units. Identity keys
are opaque hashes, never card names. AnalysisError messages contain no records.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from . import repository

DEFAULT_BOARDS = ("commander", "main")
MAX_RESERVATIONS = 20
MAX_ROWS = 20000
MAX_PRINTINGS = 50000
SUPPORTED_FORMATS = ("commander", "standard", "modern", "pioneer", "legacy")
_CARD_COLUMNS = (
    "id", "game_code", "name", "set_code", "collector_number", "type_line",
    "mana_cost", "cmc", "colors_json", "color_identity_json", "oracle_text",
    "price_usd_minor", "price_updated_at", "raw_json", "updated_at", "source",
)


class AnalysisError(ValueError):
    """Invalid options or a snapshot exceeding explicit processing bounds."""


class DeckNotFound(AnalysisError):
    """At least one requested deck does not belong to the requested game."""


def _raw(value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return {}
    if not isinstance(value, str) or len(value) > 1000000:
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _identity(card: dict[str, Any], equivalent: bool = True) -> tuple[str, str]:
    game = card["game_code"]
    basis = "exact_printing"
    value = str(card["id"])
    if equivalent and game == "mtg":
        raw = _raw(card.get("raw_json"))
        oracle = raw.get("oracle_id") if raw is not None else None
        if isinstance(oracle, str) and oracle.strip() and len(oracle) <= 128:
            basis, value = "oracle_id", oracle.strip().casefold()
        elif raw is not None and oracle is None and str(card.get("name") or "").strip():
            basis, value = "name_fallback", str(card["name"]).strip().casefold()
    digest = hashlib.sha256(f"{game}\0{basis}\0{value}".encode()).hexdigest()[:32]
    return digest, basis


def _boards(values: Iterable[str] | None) -> tuple[str, ...]:
    selected = tuple(DEFAULT_BOARDS if values is None else values)
    if len(selected) > 4 or any(value not in repository.BOARDS for value in selected):
        raise AnalysisError("Invalid board selection.")
    return tuple(board for board in repository.BOARDS if board in selected)


def _ids(deck_id: int, reserve_deck_ids: Iterable[int]) -> list[int]:
    reservations = list(reserve_deck_ids)
    if len(reservations) > MAX_RESERVATIONS:
        raise AnalysisError("Select at most 20 competing decks.")
    requested = [deck_id, *reservations]
    if any(type(value) is not int or value < 1 for value in requested):
        raise AnalysisError("Invalid deck selection.")
    if len(set(requested)) != len(requested):
        raise AnalysisError("Select each deck only once; exclude the current deck.")
    return requested


def _snapshot(game: str, deck_ids: list[int], *, equivalents: bool = False,
              holdings: bool = False) -> tuple[list[dict], list[dict], list[dict]]:
    if game not in {"mtg", "pokemon", "riftbound"}:
        raise DeckNotFound("Deck not found.")
    placeholders = ",".join("?" for _ in deck_ids)
    columns = ", ".join(f"card.{column}" for column in _CARD_COLUMNS)
    with repository._connect() as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        decks = [dict(row) for row in conn.execute(
            f"SELECT id, game_code, name, format, commander_card_id, partner_card_id "
            f"FROM decks WHERE game_code=? AND id IN ({placeholders})",
            [game, *deck_ids],
        )]
        if len(decks) != len(deck_ids):
            raise DeckNotFound("Deck not found.")
        rows = [dict(row) for row in conn.execute(
            f"SELECT slot.deck_id, slot.board, slot.category, slot.qty, {columns} "
            f"FROM deck_cards slot JOIN cards card ON card.id=slot.card_id "
            f"WHERE slot.deck_id IN ({placeholders}) ORDER BY slot.id LIMIT ?",
            [*deck_ids, MAX_ROWS + 1],
        )]
        if len(rows) > MAX_ROWS:
            raise AnalysisError("Deck snapshot exceeds the 20000-row limit.")
        if any(row["game_code"] != game for row in rows):
            raise AnalysisError("Deck contains a card from another game.")
        candidates: list[dict] = []
        if holdings and rows:
            def identity(card_id: int, name: str, raw_json: str | None) -> str:
                return _identity({"game_code": game, "id": card_id,
                                  "name": name, "raw_json": raw_json})[0]

            conn.create_function("analysis_identity", 3, identity, deterministic=True)
            match = "card.id IN (SELECT card_id FROM requested)"
            if equivalents and game == "mtg":
                match = (
                    "analysis_identity(card.id, card.name, card.raw_json) IN "
                    "(SELECT analysis_identity(chosen.id, chosen.name, chosen.raw_json) "
                    "FROM cards chosen JOIN requested ON requested.card_id=chosen.id)"
                )
            candidates = [dict(row) for row in conn.execute(
                f"WITH requested AS (SELECT DISTINCT card_id FROM deck_cards "
                f"WHERE deck_id IN ({placeholders})), "
                f"matched AS MATERIALIZED (SELECT card.id FROM cards card WHERE card.game_code=? AND {match}), "
                "owned AS (SELECT collection.card_id, SUM(collection.qty) AS owned_qty "
                "FROM collection JOIN matched ON matched.id=collection.card_id GROUP BY collection.card_id) "
                f"SELECT {columns}, COALESCE(owned.owned_qty, 0) AS owned_qty "
                "FROM cards card JOIN matched ON matched.id=card.id "
                "LEFT JOIN owned ON owned.card_id=card.id ORDER BY card.id LIMIT ?",
                [*deck_ids, game, MAX_PRINTINGS + 1],
            )]
            if len(candidates) > MAX_PRINTINGS:
                raise AnalysisError("Matching printings exceed the 50000-row limit.")
    return decks, rows, candidates


def _price(card: dict) -> int | None:
    price = card.get("price_usd_minor")
    return price if type(price) is int and price >= 0 else None


def _date(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _group(rows: list[dict], equivalent: bool) -> dict[str, dict]:
    grouped: dict[str, dict] = {}
    for row in rows:
        key, basis = _identity(row, equivalent)
        group = grouped.setdefault(key, {"key": key, "identity_basis": basis,
            "name": row["name"], "required": 0, "cards": {}, "boards": Counter(),
            "categories": Counter()})
        group["required"] += row["qty"]
        group["cards"][row["id"]] = row
        group["boards"][row["board"]] += row["qty"]
        group["categories"][(row["board"], row["category"])] += row["qty"]
    return grouped


def _printing(card: dict) -> dict:
    return {"card_id": card["id"], "name": card["name"],
            "set_code": card["set_code"], "collector_number": card["collector_number"],
            "owned": card.get("owned_qty", 0), "unit_price_minor": _price(card),
            "price_as_of": _date(card["price_updated_at"])}


def build_checklist(game: str, deck_id: int, *, match_mode: str = "exact",
                    boards: Iterable[str] | None = None,
                    reserve_deck_ids: Iterable[int] = ()) -> dict[str, Any]:
    """Return a read-only shopping plan for one deck, schema version 1.

    `boards=None` selects commander/main; an empty iterable selects none.
    Reservations consume these same boards in caller-supplied order, before the
    current deck. Duplicate, current, missing and cross-game IDs are rejected.
    `any` uses MTG oracle identity; name fallback joins ONLY other absent-oracle
    records. Other games explicitly fall back to exact printing. Holdings count
    each printing once regardless of board/category/foil/condition rows.

    Items expose required/owned/reserved/available/allocated/missing, requested
    IDs, up to 20 matching printings, nullable est_unit_minor and missing_cost_minor.
    Totals distinguish known_missing_cost_minor from estimated_missing_cost_minor
    (None when any missing copy is unpriced), plus missing_price_count in copies.
    Prices prefer the cheapest priced requested printing, then the cheapest cached
    equivalent; price_basis and estimate_card_id identify that estimate explicitly.
    This is neither a purchase nor a durable assignment of physical holdings.
    """
    if match_mode not in {"exact", "any"}:
        raise AnalysisError("Invalid printing match mode.")
    selected = _boards(boards)
    requested_ids = _ids(deck_id, reserve_deck_ids)
    equivalent = match_mode == "any" and game == "mtg"
    decks, rows, candidates = _snapshot(game, requested_ids, equivalents=equivalent, holdings=True)
    by_deck = {identifier: _group([row for row in rows if row["deck_id"] == identifier
                                 and row["board"] in selected], equivalent)
               for identifier in requested_ids}
    by_family: dict[str, list[dict]] = {}
    for card in candidates:
        by_family.setdefault(_identity(card, equivalent)[0], []).append(card)
    owned = {key: sum(card["owned_qty"] for card in family) for key, family in by_family.items()}
    remaining = dict(owned)
    reservation_results = []
    for identifier in requested_ids[1:]:
        allocated = 0
        required = 0
        for key, group in by_deck[identifier].items():
            quantity = min(group["required"], remaining.get(key, 0))
            remaining[key] = remaining.get(key, 0) - quantity
            allocated += quantity
            required += group["required"]
        reservation_results.append({"deck_id": identifier,
                        "name": next(deck["name"] for deck in decks if deck["id"] == identifier),
                        "required": required, "allocated": allocated})
    items = []
    for key, group in by_deck[deck_id].items():
        family = by_family.get(key, [])
        requested = list(group["cards"].values())
        priced = [card for card in requested if _price(card) is not None]
        basis = "lowest_priced_requested_printing"
        if not priced:
            priced = [card for card in family if _price(card) is not None]
            basis = "lowest_cached_equivalent" if priced else "unknown"
        estimate = min(priced, key=lambda card: (_price(card), card["id"])) if priced else None
        unit = _price(estimate) if estimate else None
        available = remaining.get(key, 0)
        allocated = min(group["required"], available)
        missing = group["required"] - allocated
        items.append({"key": key, "name": group["name"], "identity_basis": group["identity_basis"],
            "required": group["required"], "owned": owned.get(key, 0),
            "reserved": owned.get(key, 0) - available, "available": available,
            "allocated": allocated, "missing": missing, "est_unit_minor": unit,
            "missing_cost_minor": missing * unit if unit is not None else (0 if missing == 0 else None),
            "price_basis": basis, "estimate_card_id": estimate["id"] if estimate else None,
            "price_as_of": _date(estimate["price_updated_at"]) if estimate else None,
            "requested_card_ids": sorted(group["cards"]),
            "matching_printings": [_printing(card) for card in family[:20]],
            "matching_printings_count": len(family), "boards": dict(group["boards"]),
            "categories": [{"board": board, "category": category, "quantity": quantity}
                           for (board, category), quantity in group["categories"].items()]})
    totals = {field: sum(item[field] for item in items)
              for field in ("required", "owned", "reserved", "available", "allocated", "missing")}
    totals["missing_price_count"] = sum(item["missing"] for item in items if item["est_unit_minor"] is None)
    totals["priced_count"] = sum(item["required"] for item in items if item["est_unit_minor"] is not None)
    totals["known_missing_cost_minor"] = sum(item["missing_cost_minor"] or 0 for item in items)
    totals["estimated_missing_cost_minor"] = (None if totals["missing_price_count"]
                                              else totals["known_missing_cost_minor"])
    dates = sorted(item["price_as_of"] for item in items if item["price_as_of"])
    return {"schema_version": 1, "game": game, "deck_id": deck_id,
            "deck": next(deck for deck in decks if deck["id"] == deck_id),
            "currency": "USD", "boards": list(selected), "match_mode": match_mode,
            "effective_match_mode": "any" if equivalent else "exact",
            "match_notice": ("Equivalent identity is unsupported for this game; exact printings are used."
                             if match_mode == "any" and not equivalent else None),
            "reservations": reservation_results, "items": items, "totals": totals,
            "price_as_of_min": dates[0] if dates else None,
            "price_as_of_max": dates[-1] if dates else None,
            "reservation_notice": "Planning only; no physical holdings are assigned or changed.",
            "price_notice": "Cached USD regular-printing estimates. Dates reflect cached price records, not current provider quotes."}


def _price_summary(rows: list[dict]) -> dict:
    priced = [row for row in rows if _price(row) is not None]
    unknown = sum(row["qty"] for row in rows if _price(row) is None)
    known = sum(row["qty"] * _price(row) for row in priced)
    dates = sorted(date for row in priced if (date := _date(row["price_updated_at"])))
    return {"currency": "USD", "priced_count": sum(row["qty"] for row in priced),
            "missing_price_count": unknown, "known_value_minor": known,
            "estimated_value_minor": None if unknown else known,
            "undated_priced_count": sum(row["qty"] for row in priced if not _date(row["price_updated_at"])),
            "as_of_min": dates[0] if dates else None, "as_of_max": dates[-1] if dates else None}


def reservation_choices(game: str, deck_id: int, *, page: int = 1) -> dict:
    """Return at most 25 same-game alternatives (including archived decks).

    The build route validates the current deck first. Selection is never inferred
    from this list; only explicitly requested IDs reserve quantities.
    """
    if type(page) is not int or not 1 <= page <= 10000:
        raise AnalysisError("Invalid deck choices page.")
    with repository._connect() as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = [dict(row) for row in conn.execute(
            "SELECT id, name, archived FROM decks WHERE game_code=? AND id<>? "
            "ORDER BY id LIMIT ? OFFSET ?", (game, deck_id, 26, (page - 1) * 25),
        )]
    return {"items": rows[:25], "page": page, "has_more": len(rows) > 25}


def _mtg_types(row: dict) -> list[str]:
    headers = [re.split(r"\s(?:\u2014|-)\s", face, maxsplit=1)[0]
               for face in str(row.get("type_line") or "").split(" // ")]
    return [kind for kind in ("Artifact", "Battle", "Creature", "Enchantment", "Instant",
                              "Kindred", "Land", "Planeswalker", "Sorcery", "Tribal")
            if any(re.search(rf"\b{kind}\b", header, re.IGNORECASE) for header in headers)]


def _color_values(row: dict, field: str) -> set[str] | None:
    raw = _raw(row["raw_json"])
    if raw is not None and field in raw:
        values = raw[field]
    else:
        try:
            values = json.loads(row.get(field + "_json") or "null")
        except (ValueError, RecursionError):
            return None
        if values == []:
            return None
    if not isinstance(values, list) or any(not isinstance(value, str) or value not in "WUBRG"
                                           or len(value) != 1 for value in values):
        return None
    return set(values)


def _mana_bucket(row: dict) -> str:
    if re.search(r"\{X\}", str(row.get("mana_cost") or ""), re.IGNORECASE):
        return "X"
    try:
        value = Decimal(str(row.get("cmc")))
    except InvalidOperation:
        return "Unknown"
    if not value.is_finite() or value < 0 or value > 1000:
        return "Unknown"
    return format(value.normalize(), "f")


def _distribution(counter: Counter) -> list[dict]:
    return [{"label": label, "quantity": quantity} for label, quantity in sorted(counter.items())]


def _metadata_labels(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 20:
        return []
    if any(not isinstance(label, str) or not label.strip() or len(label) > 100 for label in value):
        return []
    return sorted(set(value))


def _copy_limit(row: dict, default: int) -> tuple[int | None, bool]:
    header = re.split(r"\s(?:\u2014|-)\s", str(row.get("type_line") or ""), maxsplit=1)[0]
    if re.search(r"\bBasic\b", header, re.I) and re.search(r"\bLand\b", header, re.I):
        return None, True
    text = row.get("oracle_text")
    if not isinstance(text, str):
        return None, False
    name = re.escape(row["name"])
    if re.search(rf"\bA deck can have any number of cards named {name}\.", text, re.I):
        return None, True
    bounded = re.search(rf"\bA deck can have up to ([a-z]+|[0-9]{{1,3}}) cards named {name}\.", text, re.I)
    if bounded:
        words = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                 "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
                 "eighteen", "nineteen", "twenty")
        token = bounded[1].lower()
        limit = int(token) if token.isdigit() else (words.index(token) + 1 if token in words else 0)
        return (limit, True) if 1 <= limit <= 100 else (None, False)
    if re.search(r"deck can have|cards? named|deck.*(?:number|copies|limit)", text, re.I):
        return None, False
    if not _mtg_types(row):
        return None, False
    return default, True


def _advisory(game: str, deck: dict, rows: list[dict]) -> dict:
    configured = str(deck.get("format") or "").strip()
    supported = configured.lower() if game == "mtg" and configured.lower() in SUPPORTED_FORMATS else None
    issues: list[dict] = []

    def issue(code: str, severity: str, message: str, count: int = 1) -> None:
        issues.append({"code": code, "severity": severity, "message": message, "count": count})

    if not supported:
        issue("unsupported_format", "unknown", "No bounded format checks are available for this configured game/format.")
    else:
        commander = [row for row in rows if row["board"] == "commander"]
        main = [row for row in rows if row["board"] == "main"]
        side = [row for row in rows if row["board"] == "side"]
        checked = main + (commander if supported == "commander" else side)
        main_count = sum(row["qty"] for row in main)
        commander_count = sum(row["qty"] for row in commander)
        if supported == "commander":
            total = main_count + commander_count
            issue("deck_size", "pass" if total == 100 else "warning",
                  f"Commander plus mainboard: {total} copies; expected 100.")
            if commander_count == 2:
                issue("commander_pair", "unknown", "Two commanders require pair-specific rules not evaluated here.")
            elif commander_count != 1:
                issue("commander_count", "warning", "Expected one commander, or a separately verified commander pair.")
            for row in commander:
                types = _mtg_types(row)
                legendary = re.search(r"\bLegendary\b", str(row.get("type_line") or ""), re.I)
                text = row.get("oracle_text")
                explicit = isinstance(text, str) and re.search(r"\bcan be your commander\b", text, re.I)
                if (legendary and "Creature" in types) or explicit:
                    issue("commander_eligibility", "pass", "Cached metadata meets a basic commander eligibility condition.", row["qty"])
                elif types and isinstance(text, str):
                    issue("commander_eligibility", "warning", "A commander lacks legendary-creature or explicit commander eligibility.", row["qty"])
                else:
                    issue("commander_eligibility", "unknown", "Commander eligibility metadata is incomplete.", row["qty"])
            commander_colors = [_color_values(row, "color_identity") for row in commander]
            if not commander_colors or any(value is None for value in commander_colors):
                issue("color_identity", "unknown", "Commander color identity is missing or invalid.")
            else:
                allowed = set().union(*commander_colors)
                outside = 0
                unknown = 0
                for row in main:
                    colors = _color_values(row, "color_identity")
                    if colors is None:
                        unknown += row["qty"]
                    elif not colors <= allowed:
                        outside += row["qty"]
                if outside:
                    issue("color_identity", "warning", "Mainboard copies have colors outside the cached commander identity.", outside)
                if unknown:
                    issue("color_identity", "unknown", "Color identity is missing or invalid for mainboard copies.", unknown)
                if not outside and not unknown:
                    issue("color_identity", "pass", "Known mainboard color identities fit the cached commander identity.")
        else:
            issue("main_size", "pass" if main_count >= 60 else "warning",
                  f"Mainboard: {main_count} copies; expected at least 60.")
            side_count = sum(row["qty"] for row in side)
            issue("side_size", "pass" if side_count <= 15 else "warning",
                  f"Sideboard: {side_count} copies; expected at most 15.")
            if commander_count:
                issue("commander_board", "warning", "Commander-board copies are outside this format's checked main/side deck.")
        exceeded = 0
        uncertain = 0
        for group in _group(checked, True).values():
            limits = {_copy_limit(row, 1 if supported == "commander" else 4)
                      for row in group["cards"].values()}
            if len(limits) != 1 or not next(iter(limits))[1]:
                uncertain += group["required"]
                continue
            limit, _known = next(iter(limits))
            if limit is None:
                continue
            if group["identity_basis"] != "oracle_id":
                uncertain += group["required"]
            elif group["required"] > limit:
                exceeded += 1
        if exceeded:
            issue("copy_limits", "warning", "Logical card families exceed the cached copy limit across checked boards.", exceeded)
        if uncertain:
            issue("copy_limits", "unknown", "Identity or copy-exception metadata is insufficient for some copies.", uncertain)
        if not exceeded and not uncertain:
            issue("copy_limits", "pass", "Known copy limits are satisfied for the checked boards.")
        forbidden = 0
        unknown_legalities = 0
        for row in checked:
            raw = _raw(row["raw_json"]) or {}
            legalities = raw.get("legalities")
            status = legalities.get(supported) if isinstance(legalities, dict) else None
            if status in ("banned", "not_legal"):
                forbidden += row["qty"]
            elif status != "legal":
                unknown_legalities += row["qty"]
        if forbidden:
            issue("cached_legalities", "warning", "Cached provider metadata marks copies banned or not legal in this format.", forbidden)
        if unknown_legalities:
            issue("cached_legalities", "unknown", "Provider format status is missing or not recognized for some copies.", unknown_legalities)
        if not forbidden and not unknown_legalities:
            issue("cached_legalities", "pass", "Cached provider statuses are legal for the checked copies; not a tournament ruling.")
    issue("rules_scope", "unknown", "Offline advisory only. Current rules, provider changes, and complex exceptions are not fully evaluated.")
    dates = sorted(date for row in rows if (date := _date(row["updated_at"])))
    return {"configured_format": configured, "supported_format": supported,
            "format_label": supported.title() if supported else configured or "Unspecified",
            "supported_formats": list(SUPPORTED_FORMATS), "issues": issues,
            "severity_counts": {severity: sum(issue["severity"] == severity for issue in issues)
                                for severity in ("warning", "unknown", "pass")},
            "cache_as_of_min": dates[0] if dates else None, "cache_as_of_max": dates[-1] if dates else None,
            "provider_rules_as_of": None,
            "freshness_note": "Cache row dates reflect local imports, not the provider's current rules date. No online rules lookup.",
            "checked_boards": (["commander", "main"] if supported == "commander" else ["main", "side"]) if supported else [],
            "writes_blocked": False}


def deck_analysis(game: str, deck_id: int) -> dict[str, Any]:
    """Quantity-weighted, complete bounded deck snapshot for the `analysis` partial.

    `counts` separates playable (main+commander), side, maybe, and all copies.
    `boards` includes zero boards, per-board nullable prices and price-date ranges.
    Distributions describe playable copies only: types/colors may count a copy
    multiple times; MTG mana_curve excludes cards with any land face and separates
    X and unknown mana values. Pokemon exposes supertypes/types, Riftbound only
    its cached type labels. `categories` retains board identity.
    `advisory.issues` contain code/severity/message/count; severity_counts counts
    issues (not affected copies). No legal/tournament-legal verdict is returned.
    No network, collection mutation, or paginated catalog API is involved.
    """
    decks, rows, _candidates = _snapshot(game, _ids(deck_id, []))
    counts = {board: sum(row["qty"] for row in rows if row["board"] == board) for board in repository.BOARDS}
    counts["playable"] = counts["main"] + counts["commander"]
    counts["all"] = sum(row["qty"] for row in rows)
    playable = [row for row in rows if row["board"] in DEFAULT_BOARDS]
    boards = []
    for board in repository.BOARDS:
        subset = [row for row in rows if row["board"] == board]
        boards.append({"board": board, "quantity": counts[board],
                       "distinct_printings": len({row["id"] for row in subset}), "prices": _price_summary(subset)})
    categories = Counter()
    for row in rows:
        categories[(row["board"], row["category"])] += row["qty"]
    types = Counter()
    colors = Counter()
    supertypes = Counter()
    curve = Counter()
    land_count = 0
    for row in playable:
        quantity = row["qty"]
        if game == "mtg":
            row_types = _mtg_types(row)
            for label in row_types or ["Unknown"]:
                types[label] += quantity
            if "Land" in row_types:
                land_count += quantity
            elif not row_types:
                curve["Unknown"] += quantity
            else:
                curve[_mana_bucket(row)] += quantity
            row_colors = _color_values(row, "colors")
            for label in (["Unknown"] if row_colors is None else sorted(row_colors) or ["Colorless"]):
                colors[label] += quantity
        elif game == "pokemon":
            raw = _raw(row["raw_json"]) or {}
            supertype = raw.get("supertype")
            supertypes[supertype if isinstance(supertype, str) and 0 < len(supertype) <= 100 else "Unknown"] += quantity
            for label in _metadata_labels(raw.get("types")) or ["Unknown"]:
                types[label] += quantity
        else:
            label = row.get("type_line")
            types[label if isinstance(label, str) and 0 < len(label) <= 100 else "Unknown"] += quantity
    curve_order = sorted((key for key in curve if key not in ("X", "Unknown")), key=Decimal)
    curve_order.extend(key for key in ("X", "Unknown") if key in curve)
    return {"schema_version": 1, "game": game, "deck_id": deck_id, "currency": "USD",
            "counts": counts, "boards": boards, "prices": _price_summary(rows),
            "playable_prices": _price_summary(playable),
            "categories": [{"board": board, "category": category, "quantity": quantity}
                           for (board, category), quantity in categories.items()],
            "mana_curve": {"label": "Nonland mana curve (main + commander; land faces excluded)",
                           "buckets": [{"label": key, "quantity": curve[key]} for key in curve_order],
                           "land_count": land_count, "unknown_count": curve["Unknown"], "x_count": curve["X"]},
            "colors": _distribution(colors), "types": _distribution(types), "supertypes": _distribution(supertypes),
            "distribution_note": "Main + commander copies. Multicolor and multi-type cards count in each matching group.",
            "price_note": "Cached regular-printing USD estimates, not live quotes. Missing prices are excluded from known subtotals.",
            "advisory": _advisory(game, decks[0], rows)}