"""Filtered holdings and bounded, atomic collection CSV exchange."""
from __future__ import annotations

import csv
import io
import json
import secrets
import sqlite3
from typing import Any, Generator

from . import purchases
from . import repository as cards

MAX_CSV_BYTES = 500_000
MAX_CSV_ROWS = 2000
MAX_EXPORT_ROWS = 100_000
CSV_FIELDS = (
    "schema", "external_id", "original_external_id", "name", "set_code", "collector_number",
    "qty", "foil", "condition", "acquired_date", "unit_price", "currency", "price_source",
    "priced_qty", "snapshot_date", "snapshot_source", "recorded_at", "notes",
)


def filters(values: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {key: cards._text(values.get(key), key, maximum=100) for key in
              ("q", "set_code", "rarity", "foil", "condition", "in_decks", "ownership", "min_price", "max_price")}
    result["currency"] = str(values.get("currency") or "USD").upper()
    for key, allowed in {
        "foil": ("", "0", "1"), "condition": ("", *cards.CONDITIONS),
        "in_decks": ("", "yes", "no"), "ownership": ("", "shortfall", "covered"),
        "currency": cards.SUPPORTED_CURRENCIES,
    }.items():
        if result[key] not in allowed:
            raise ValueError("invalid collection filter")
    for key in ("min_price", "max_price"):
        result[key + "_minor"] = cards.to_minor(result[key])
        if result[key + "_minor"] is not None and result[key + "_minor"] > 100_000_000_000:
            raise ValueError("price filter exceeds the supported limit")
    if (result["min_price_minor"] is not None and result["max_price_minor"] is not None
            and result["min_price_minor"] > result["max_price_minor"]):
        raise ValueError("minimum price exceeds maximum price")
    result["page"] = cards._positive_int(values.get("page") or 1, "page", maximum=10_000_000)
    result["page_size"] = min(cards._positive_int(values.get("page_size") or 50, "page size", maximum=10_000_000), 200)
    return result


def _query(game_code: str, selected: dict[str, Any]) -> tuple[str, str, list[Any]]:
    market = ("CASE WHEN col.foil = 1 THEN NULL ELSE card.price_eur_minor END"
              if selected["currency"] == "EUR" else
              "CASE WHEN col.foil = 1 THEN card.price_usd_foil_minor ELSE card.price_usd_minor END")
    base = f"""
        WITH usage AS (
            SELECT dc.card_id, COUNT(DISTINCT dc.deck_id) AS in_decks, SUM(dc.qty) AS deck_qty
              FROM deck_cards dc JOIN decks deck ON deck.id = dc.deck_id
             WHERE deck.archived = 0 GROUP BY dc.card_id
        ), costs AS (
            SELECT collection_id,
                   SUM(CASE WHEN price_source = 'entered' THEN remaining_priced_qty * unit_price_minor END) AS actual_cost_minor,
                   SUM(CASE WHEN price_source = 'market_estimate' THEN remaining_priced_qty * unit_price_minor END) AS estimate_cost_minor,
                   SUM(CASE WHEN price_source = 'legacy_summary' THEN NULLIF(remaining_priced_qty, 0) * unit_price_minor END) AS legacy_cost_minor,
                   SUM(NULLIF(remaining_priced_qty, 0) * unit_price_minor) AS exact_cost_minor,
                   SUM(CASE WHEN price_source = 'entered' THEN remaining_priced_qty ELSE 0 END) AS actual_qty
              FROM collection_lots WHERE remaining_qty > 0 GROUP BY collection_id
        ), owned AS (
            SELECT card_id, SUM(qty) AS owned_qty FROM collection GROUP BY card_id
        ), holdings AS (
            SELECT col.*, card.name, card.external_id, card.set_code, card.collector_number,
                   card.rarity, card.image_small, card.image_normal, card.price_updated_at,
                   {market} AS market_unit_minor,
                   COALESCE(usage.in_decks, 0) AS in_decks, COALESCE(usage.deck_qty, 0) AS deck_qty,
                   owned.owned_qty, costs.actual_cost_minor, costs.estimate_cost_minor,
                   costs.legacy_cost_minor, costs.exact_cost_minor, COALESCE(costs.actual_qty,0) AS actual_qty
              FROM collection col JOIN cards card ON card.id = col.card_id
              JOIN owned ON owned.card_id = col.card_id
              LEFT JOIN usage ON usage.card_id = col.card_id
              LEFT JOIN costs ON costs.collection_id = col.id
             WHERE card.game_code = ?
        )
    """
    predicates = ["1 = 1"]
    args: list[Any] = [game_code]
    if selected["q"]:
        escaped = selected["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        predicates.append("name LIKE ? ESCAPE '\\' COLLATE NOCASE")
        args.append("%" + escaped + "%")
    for key in ("set_code", "rarity", "condition", "foil"):
        if selected[key] != "":
            predicates.append(f"{key} = ? COLLATE NOCASE")
            args.append(selected[key])
    if selected["in_decks"]:
        predicates.append("in_decks > 0" if selected["in_decks"] == "yes" else "in_decks = 0")
    if selected["ownership"]:
        predicates.append("owned_qty < deck_qty" if selected["ownership"] == "shortfall" else "owned_qty >= deck_qty")
    for key, operator in (("min_price_minor", ">="), ("max_price_minor", "<=")):
        if selected[key] is not None:
            predicates.append(f"market_unit_minor {operator} ?")
            args.append(selected[key])
    return base, " AND ".join(predicates), args


def _totals(conn: sqlite3.Connection, base: str, where: str, args: list[Any], currency: str) -> dict[str, Any]:
    return dict(conn.execute(base + f"""
        SELECT COUNT(*) AS holdings, COALESCE(SUM(qty),0) AS qty,
               SUM(qty * market_unit_minor) AS market_minor,
               COALESCE(SUM(CASE WHEN market_unit_minor IS NULL THEN qty ELSE 0 END),0) AS unpriced_qty,
               COALESCE(SUM(qty - acquired_qty),0) AS unknown_cost_qty,
               SUM(CASE WHEN acquired_currency = ? THEN actual_cost_minor END) AS actual_cost_minor,
               SUM(CASE WHEN acquired_currency = ? THEN estimate_cost_minor END) AS estimate_cost_minor,
               SUM(CASE WHEN acquired_currency = ? THEN legacy_cost_minor END) AS legacy_cost_minor,
               SUM(CASE WHEN acquired_currency = ? AND actual_qty > 0
                        THEN actual_qty * market_unit_minor - actual_cost_minor END) AS actual_gain_minor,
               MIN(CASE WHEN market_unit_minor IS NOT NULL THEN price_updated_at END) AS price_asof_min,
               MAX(CASE WHEN market_unit_minor IS NOT NULL THEN price_updated_at END) AS price_asof_max
          FROM holdings WHERE {where}
    """, [args[0], currency, currency, currency, currency, *args[1:]]).fetchone())


def page(game_code: str, values: dict[str, Any]) -> dict[str, Any]:
    selected = filters(values)
    cards.init_db()
    base, where, args = _query(game_code, selected)
    with cards._connect() as conn:
        conn.execute("BEGIN")
        totals = _totals(conn, base, where, args, selected["currency"])
        all_totals = _totals(conn, base, "1 = 1", [game_code], selected["currency"])
        pages = max(1, (totals["holdings"] + selected["page_size"] - 1) // selected["page_size"])
        selected["page"] = min(selected["page"], pages)
        rows = conn.execute(base + f"""
            SELECT * FROM holdings WHERE {where}
             ORDER BY name COLLATE NOCASE, id LIMIT ? OFFSET ?
        """, [*args, selected["page_size"], (selected["page"] - 1) * selected["page_size"]]).fetchall()
        options = conn.execute("""
            SELECT DISTINCT card.set_code, card.rarity FROM collection col
              JOIN cards card ON card.id = col.card_id WHERE card.game_code = ?
        """, (game_code,)).fetchall()
    return dict(rows=[dict(row) for row in rows], filters=selected, totals=totals,
                all_totals=all_totals, pages=pages,
                sets=sorted({row["set_code"] for row in options if row["set_code"]}),
                rarities=sorted({row["rarity"] for row in options if row["rarity"]}))


def _safe_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if text.startswith("'") or text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def _unescape(value: str, own_format: bool) -> str:
    if own_format and value.startswith("'") and _safe_cell(value[1:]) == value:
        return value[1:]
    return value


def _money(price: int | None) -> str:
    return "" if price is None else f"{price // 100}.{price % 100:02d}"


def export_csv(game_code: str, values: dict[str, Any]) -> Generator[str, None, None]:
    """Stream every matching active lot, or reject above the explicit 100000-lot cap."""
    selected = filters(values)
    cards.init_db()
    base, where, args = _query(game_code, selected)
    selected_sql = base + f"""
        SELECT lot.*, holding.external_id, holding.name, holding.set_code, holding.collector_number,
               original.external_id AS original_external_id
          FROM holdings holding JOIN collection_lots lot ON lot.collection_id = holding.id
          JOIN cards original ON original.id = lot.original_card_id
         WHERE holding.id IN (SELECT id FROM holdings WHERE {where}) AND lot.remaining_qty > 0
         ORDER BY holding.name COLLATE NOCASE, holding.id, lot.id
    """

    def stream() -> Generator[str, None, None]:
        with cards._connect() as conn:
            conn.execute("BEGIN")
            count = conn.execute(base + f"""
                SELECT COUNT(*) FROM collection_lots lot
                 WHERE lot.collection_id IN (SELECT id FROM holdings WHERE {where}) AND lot.remaining_qty > 0
            """, args).fetchone()[0]
            if count > MAX_EXPORT_ROWS:
                raise ValueError("export exceeds 100000 purchase rows; narrow the filters")
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer, lineterminator="\r\n")
            writer.writerow(CSV_FIELDS)
            yield buffer.getvalue()
            for row in conn.execute(selected_sql, args):
                buffer.seek(0)
                buffer.truncate()
                writer.writerow([_safe_cell(value) for value in (
                    "luigi-collection-v1", row["external_id"], row["original_external_id"], row["name"],
                    row["set_code"], row["collector_number"], row["remaining_qty"], row["foil"],
                    row["condition"], row["acquired_date"], _money(row["unit_price_minor"]), row["currency"],
                    row["price_source"], row["remaining_priced_qty"], row["snapshot_date"],
                    row["snapshot_source"], row["recorded_at"], row["notes"],
                )])
                yield buffer.getvalue()
    return stream()


def _resolve(conn: sqlite3.Connection, game_code: str, row: dict[str, str]) -> int:
    if row.get("external_id"):
        matches = conn.execute("SELECT id FROM cards WHERE game_code = ? AND external_id = ?",
                               (game_code, row["external_id"])).fetchall()
    elif row.get("set_code") and row.get("collector_number"):
        matches = conn.execute("""
            SELECT id FROM cards WHERE game_code = ? AND set_code = ? COLLATE NOCASE AND collector_number = ?
        """, (game_code, row["set_code"], row["collector_number"])).fetchall()
    else:
        raise ValueError("external_id or exact set_code and collector_number required")
    if len(matches) != 1:
        raise ValueError("printing is missing or ambiguous")
    return int(matches[0]["id"])


def _csv_row(conn: sqlite3.Connection, game_code: str, row: dict[str, str]) -> dict[str, Any]:
    own = row.get("schema") == "luigi-collection-v1"
    row = {key: _unescape(value, own) for key, value in row.items()}
    card_id = _resolve(conn, game_code, row)
    original_id = _resolve(conn, game_code, {"external_id": row["original_external_id"]}) if row.get("original_external_id") else card_id
    if original_id != card_id:
        cards._validate_printing_swap(conn, original_id, card_id, game_code)
    if row.get("foil", "0") not in {"", "0", "1"}:
        raise ValueError("foil must be 0 or 1")
    source = row.get("price_source") or ("entered" if row.get("unit_price") else "unknown")
    if source not in {"entered", "market_estimate", "unknown", "legacy_summary"}:
        raise ValueError("invalid purchase price source")
    values = purchases._values(conn, card_id, qty=cards._positive_int(row.get("qty") or 1, "quantity"),
                              foil=row.get("foil") == "1", condition=row.get("condition") or "NM",
                              acquired_date=row.get("acquired_date", ""), acquired_price=row.get("unit_price"),
                              acquired_currency=row.get("currency") or "USD", notes=row.get("notes", ""))
    priced = int(row.get("priced_qty") or (values["qty"] if values["unit_price_minor"] is not None else 0))
    if not 0 <= priced <= values["qty"] or (values["unit_price_minor"] is None and priced):
        raise ValueError("invalid priced quantity")
    if source == "unknown" and values["unit_price_minor"] is not None:
        raise ValueError("unknown cost cannot include a price")
    if source in {"entered", "market_estimate"} and (values["unit_price_minor"] is None or priced != values["qty"]):
        raise ValueError("a priced purchase needs a unit price for every copy")
    snapshot_date = purchases.clean_date(row.get("snapshot_date", ""))
    snapshot_source = cards._text(row.get("snapshot_source"), "snapshot source", maximum=300)
    if source == "market_estimate":
        if not snapshot_date or snapshot_date != values["acquired_date"] or not snapshot_source:
            raise ValueError("market estimate needs exact purchase-day snapshot provenance")
        if values["foil"] and values["currency"] == "EUR":
            raise ValueError("EUR foil estimates are unavailable")
        source_name = snapshot_source.removeprefix("csv:")
        retained = conn.execute("""
            SELECT 1 FROM collection_lots WHERE original_card_id = ? AND foil = ?
             AND currency = ? AND acquired_date = ? AND snapshot_date = ?
             AND unit_price_minor = ? AND price_source = 'market_estimate'
             AND snapshot_source IN (?, ?)
        """, (original_id, values["foil"], values["currency"], values["acquired_date"],
              snapshot_date, values["unit_price_minor"], source_name, "csv:" + source_name)).fetchone()
        if not retained:
            matched = purchases.estimate(conn, original_id, snapshot_date, values["currency"], bool(values["foil"]))
            if not matched["available"] or matched["unit_price_minor"] != values["unit_price_minor"] or matched["snapshot_source"] != source_name:
                raise ValueError("imported estimate has no matching local exact-day snapshot")
        snapshot_source = "csv:" + snapshot_source.removeprefix("csv:")
    elif snapshot_date or snapshot_source:
        raise ValueError("snapshot fields are only valid for market estimates")
    return dict(values, card_id=card_id, original_card_id=original_id, priced_qty=priced,
                price_source=source, snapshot_date=snapshot_date, snapshot_source=snapshot_source or None)


def _apply_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]], mode: str) -> int:
    added = 0
    for row in rows:
        existing = conn.execute("SELECT id FROM collection WHERE card_id = ? AND foil = ? AND condition = ?",
                                (row["card_id"], row["foil"], row["condition"])).fetchone()
        if existing and mode == "reject":
            raise ValueError("duplicate holding; choose add or skip explicitly")
        if existing and mode == "skip":
            continue
        lot_id = purchases.record(conn, row["card_id"], qty=row["qty"], foil=bool(row["foil"]),
                                  condition=row["condition"], acquired_date=row["acquired_date"] or "",
                                  acquired_price=_money(row["unit_price_minor"]) if row["priced_qty"] else "", acquired_currency=row["currency"],
                                  notes=row["notes"] or "")
        conn.execute("""
            UPDATE collection_lots SET original_card_id = ?, price_source = ?, snapshot_date = ?,
                                snapshot_source = ?, priced_qty = ?, remaining_priced_qty = ?, unit_price_minor = ? WHERE id = ?
        """, (row["original_card_id"], row["price_source"], row["snapshot_date"], row["snapshot_source"],
                            row["priced_qty"], row["priced_qty"], row["unit_price_minor"], lot_id))
        holding = conn.execute("SELECT collection_id FROM collection_lots WHERE id = ?", (lot_id,)).fetchone()[0]
        purchases.recalculate(conn, holding)
        added += 1
    return added


def preview_csv(game_code: str, raw: bytes, duplicate_mode: str) -> dict[str, Any]:
    if len(raw) > MAX_CSV_BYTES:
        raise ValueError("CSV exceeds 500000 bytes")
    if duplicate_mode not in {"add", "skip", "reject"}:
        raise ValueError("choose a duplicate mode: add, skip, or reject")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        fields = reader.fieldnames
        if not fields or len(fields) != len(set(fields)) or any(field not in CSV_FIELDS for field in fields):
            raise ValueError("invalid CSV headers")
        parsed = []
        for row in reader:
            if len(parsed) >= MAX_CSV_ROWS:
                raise ValueError("CSV exceeds 2000 rows")
            if None in row or any(value is None for value in row.values()):
                raise ValueError("CSV row has the wrong number of columns")
            parsed.append(row)
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("CSV must be valid UTF-8 with consistent columns") from exc
    if not parsed:
        raise ValueError("CSV has no purchase rows")
    cards.init_db()
    with cards.transaction() as conn:
        rows = []
        for index, row in enumerate(parsed, 1):
            try:
                rows.append(_csv_row(conn, game_code, row))
            except (ValueError, OverflowError) as exc:
                raise ValueError(f"CSV row {index}: invalid printing, quantity, price, or provenance") from exc
        conn.execute("SAVEPOINT collection_preview")
        count = _apply_rows(conn, rows, duplicate_mode)
        conn.execute("ROLLBACK TO collection_preview")
        conn.execute("RELEASE collection_preview")
        conn.execute("DELETE FROM collection_imports WHERE applied_at IS NULL AND created_at < datetime('now','-1 hour')")
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO collection_imports(token,game_code,payload,duplicate_mode) VALUES (?,?,?,?)",
                     (token, game_code, json.dumps(rows), duplicate_mode))
    return dict(token=token, rows=rows, added=count, skipped=len(rows)-count, duplicate_mode=duplicate_mode,
                estimate_count=sum(row["price_source"] == "market_estimate" for row in rows))


def apply_csv(game_code: str, token: str, *, confirm_estimates: bool = False) -> dict[str, Any]:
    cards.init_db()
    with cards.transaction() as conn:
        preview = conn.execute("""
            SELECT * FROM collection_imports WHERE token = ? AND game_code = ?
              AND (applied_at IS NOT NULL OR created_at >= datetime('now','-1 hour'))
        """, (token, game_code)).fetchone()
        if not preview:
            raise ValueError("preview expired or not found")
        if preview["applied_at"]:
            return dict(added=preview["result_count"], already_applied=True)
        rows = json.loads(preview["payload"])
        if any(row["price_source"] == "market_estimate" for row in rows) and not confirm_estimates:
            raise ValueError("confirm imported market estimates explicitly")
        count = _apply_rows(conn, rows, preview["duplicate_mode"])
        conn.execute("""
            UPDATE collection_imports SET applied_at = datetime('now'), result_count = ?, payload = '[]'
             WHERE token = ?
        """, (count, token))
    return dict(added=count, already_applied=False)