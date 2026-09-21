"""Local acquisition ledger. Legacy summaries are not reconstructed purchases."""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from . import repository as cards


def init_schema(conn: sqlite3.Connection, *, migrate_legacy: bool) -> None:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    ledger_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'collection_lots'"
    ).fetchone()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collection_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_id INTEGER REFERENCES collection(id) ON DELETE SET NULL,
            original_card_id INTEGER NOT NULL REFERENCES cards(id),
            holding_card_id INTEGER NOT NULL REFERENCES cards(id),
            qty INTEGER NOT NULL CHECK (qty > 0),
            remaining_qty INTEGER NOT NULL CHECK (remaining_qty BETWEEN 0 AND qty),
            priced_qty INTEGER NOT NULL CHECK (priced_qty BETWEEN 0 AND qty),
            remaining_priced_qty INTEGER NOT NULL
                CHECK (remaining_priced_qty BETWEEN 0 AND remaining_qty),
            foil INTEGER NOT NULL CHECK (foil IN (0, 1)),
            condition TEXT NOT NULL CHECK (condition IN ('NM','LP','MP','HP','DMG')),
            acquired_date TEXT,
            unit_price_minor INTEGER CHECK (unit_price_minor >= 0),
            currency TEXT NOT NULL CHECK (currency IN ('USD','EUR')),
            price_source TEXT NOT NULL CHECK (
                price_source IN ('entered','market_estimate','unknown','legacy_summary')),
            snapshot_date TEXT,
            snapshot_source TEXT,
            notes TEXT,
            recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            corrected_at TEXT,
            removed_at TEXT,
            CHECK (unit_price_minor IS NOT NULL OR priced_qty = 0),
            CHECK (remaining_priced_qty <= priced_qty)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_collection_lots_holding ON collection_lots(collection_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collection_imports (
            token TEXT PRIMARY KEY,
            game_code TEXT NOT NULL REFERENCES games(code),
            payload TEXT NOT NULL,
            duplicate_mode TEXT NOT NULL CHECK (duplicate_mode IN ('add','skip','reject')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            applied_at TEXT,
            result_count INTEGER
        )
    """)
    if migrate_legacy or not ledger_exists:
        conn.execute("""
            INSERT INTO collection_lots (
                collection_id, original_card_id, holding_card_id, qty, remaining_qty,
                priced_qty, remaining_priced_qty, foil, condition, acquired_date,
                unit_price_minor, currency, price_source, notes
            )
            SELECT col.id, col.card_id, col.card_id, col.qty, col.qty,
                   CASE WHEN col.acquired_price_minor IS NULL THEN 0
                        ELSE MIN(col.qty, col.acquired_qty) END,
                   CASE WHEN col.acquired_price_minor IS NULL THEN 0
                        ELSE MIN(col.qty, col.acquired_qty) END,
                   col.foil, col.condition, col.acquired_date,
                   col.acquired_price_minor, col.acquired_currency, 'legacy_summary', col.notes
              FROM collection col
             WHERE NOT EXISTS (
                 SELECT 1 FROM collection_lots lot WHERE lot.collection_id = col.id
             )
        """)


def clean_date(value: str) -> str | None:
    if not value:
        return None
    try:
        if len(value) != 10:
            raise ValueError
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("acquired date must be YYYY-MM-DD") from exc


def estimate(conn: sqlite3.Connection, card_id: int, acquired_date: str,
             currency: str = "USD", foil: bool = False) -> dict[str, Any]:
    day = clean_date(acquired_date)
    if not day:
        raise ValueError("purchase date is required for an estimate")
    if currency not in cards.SUPPORTED_CURRENCIES:
        raise ValueError("unsupported acquisition currency")
    card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not card:
        raise ValueError("card not found")
    result: dict[str, Any] = dict(available=False, unit_price_minor=None, currency=currency,
                  foil=bool(foil), snapshot_date=day, snapshot_source=None,
                  price_source="unknown")
    if currency == "EUR" and foil:
        return result
    column = "price_eur_minor" if currency == "EUR" else (
        "price_usd_foil_minor" if foil else "price_usd_minor")
    snapshot = conn.execute(
        f"SELECT {column} AS price FROM price_history WHERE card_id = ? AND snapshot_date = ?",
        (card_id, day),
    ).fetchone()
    price = snapshot["price"] if snapshot else None
    source = "price_history:" + str(card["source"])
    if price is None and str(card["price_updated_at"] or "")[:10] == day:
        price = card[column]
        source = "catalog:" + str(card["source"])
    if price is not None:
        result.update(available=True, unit_price_minor=int(price),
                      snapshot_source=source, price_source="market_estimate")
    return result


def _values(conn: sqlite3.Connection, card_id: int, *, qty: int = 1,
            foil: bool = False, condition: str = "NM", acquired_date: str = "",
            acquired_price: Any = None, acquired_currency: str = "USD",
            notes: str = "", price_source: str = "entered",
            estimate_confirmed: bool = False) -> dict[str, Any]:
    quantity = cards._positive_int(qty, "quantity")
    currency = str(acquired_currency or "USD").strip().upper()
    condition = str(condition or "NM").strip().upper()
    if currency not in cards.SUPPORTED_CURRENCIES:
        raise ValueError("unsupported acquisition currency")
    if condition not in cards.CONDITIONS:
        raise ValueError("invalid card condition")
    day = clean_date(str(acquired_date or "").strip())
    price = cards.to_minor(acquired_price)
    if price is not None and price > 100_000_000_000:
        raise ValueError("unit price exceeds the supported limit")
    source_date = source_name = None
    if price_source == "market_estimate":
        if price is not None:
            raise ValueError("confirm the estimate explicitly with no entered actual price")
        snapshot = estimate(conn, card_id, day or "", currency, foil)
        if snapshot["available"]:
            if not estimate_confirmed:
                raise ValueError("confirm the estimate explicitly with no entered actual price")
            price = snapshot["unit_price_minor"]
            source_date, source_name = snapshot["snapshot_date"], snapshot["snapshot_source"]
        else:
            price_source = "unknown"
    elif price_source not in {"entered", "unknown"}:
        raise ValueError("invalid purchase price source")
    elif price_source == "unknown" and price is not None:
        raise ValueError("unknown cost cannot include a price")
    else:
        price_source = "entered" if price is not None else "unknown"
    return dict(qty=quantity, foil=int(bool(foil)), condition=condition,
                acquired_date=day, unit_price_minor=price, currency=currency,
                price_source=price_source, snapshot_date=source_date,
                snapshot_source=source_name,
                notes=cards._text(notes, "notes", maximum=5000) or None)


def recalculate(conn: sqlite3.Connection, collection_id: int) -> None:
    totals = conn.execute("""
        SELECT SUM(remaining_qty) AS qty, SUM(remaining_priced_qty) AS priced,
               SUM(remaining_priced_qty * unit_price_minor) AS cost,
               MIN(acquired_date) AS day,
               MIN(CASE WHEN remaining_priced_qty > 0 THEN currency END) AS currency,
               COUNT(DISTINCT CASE WHEN remaining_priced_qty > 0 THEN currency END) AS currencies
          FROM collection_lots WHERE collection_id = ? AND remaining_qty > 0
    """, (collection_id,)).fetchone()
    if totals["currencies"] > 1:
        raise ValueError("acquisition currency must match the existing entry")
    quantity, priced = int(totals["qty"] or 0), int(totals["priced"] or 0)
    if quantity > 9999:
        raise ValueError("combined collection quantity must be at most 9999")
    if not quantity:
        conn.execute("DELETE FROM collection WHERE id = ?", (collection_id,))
        return
    average = (int(totals["cost"]) * 2 + priced) // (2 * priced) if priced else None
    conn.execute("""
        UPDATE collection SET qty = ?, acquired_qty = ?, acquired_price_minor = ?,
            acquired_currency = COALESCE(?, acquired_currency), acquired_date = ? WHERE id = ?
    """, (quantity, priced, average, totals["currency"], totals["day"], collection_id))


def record(conn: sqlite3.Connection, card_id: int, **fields: Any) -> int:
    values = _values(conn, card_id, **fields)
    if not conn.execute("SELECT 1 FROM cards WHERE id = ?", (card_id,)).fetchone():
        raise ValueError("card not found")
    existing = conn.execute(
        "SELECT * FROM collection WHERE card_id = ? AND foil = ? AND condition = ?",
        (card_id, values["foil"], values["condition"]),
    ).fetchone()
    if existing:
        collection_id = int(existing["id"])
        conn.execute("UPDATE collection SET notes = ? WHERE id = ?", (
            cards._merged_notes(existing["notes"], values["notes"]), collection_id))
    else:
        inserted_id = conn.execute("""
            INSERT INTO collection(card_id, qty, foil, condition, notes, acquired_currency)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (card_id, values["qty"], values["foil"], values["condition"],
              values["notes"], values["currency"])).lastrowid
        assert inserted_id is not None
        collection_id = inserted_id
    priced = values["qty"] if values["unit_price_minor"] is not None else 0
    lot_id = conn.execute("""
        INSERT INTO collection_lots (
            collection_id, original_card_id, holding_card_id, qty, remaining_qty,
            priced_qty, remaining_priced_qty, foil, condition, acquired_date,
            unit_price_minor, currency, price_source, snapshot_date, snapshot_source, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (collection_id, card_id, card_id, values["qty"], values["qty"], priced, priced,
          values["foil"], values["condition"], values["acquired_date"], values["unit_price_minor"],
          values["currency"], values["price_source"], values["snapshot_date"],
                    values["snapshot_source"], values["notes"])).lastrowid
    assert lot_id is not None
    recalculate(conn, collection_id)
    return lot_id


def correct(collection_id: int, game_code: str, *, lot_id: int | None = None,
            acquired_date: str = "", acquired_price: Any = None,
            acquired_currency: str = "USD", price_source: str = "entered",
            estimate_confirmed: bool = False) -> bool:
    """Correct one lot. An old aggregate editor is accepted only for a single lot.

    Multi-lot holdings require a lot ID. A legacy summary correction remains a
    legacy summary, not evidence of an actual individual purchase.
    """
    cards.init_db()
    with cards.transaction() as conn:
        lots = conn.execute("""
            SELECT lot.* FROM collection_lots lot JOIN cards card ON card.id = lot.holding_card_id
             WHERE lot.collection_id = ? AND card.game_code = ?
        """, (collection_id, game_code)).fetchall()
        if not lots:
            return False
        if lot_id is None and len(lots) != 1:
            compatible = (
                price_source == "entered" and acquired_price not in (None, "")
                and sum(item["price_source"] == "entered" for item in lots) == 1
                and all(item["price_source"] in {"entered", "unknown"}
                        and item["qty"] == item["remaining_qty"] for item in lots)
            )
            if not compatible:
                raise ValueError("multiple purchases: select a purchase lot ID; aggregate edits do not rewrite history")
            for item in lots:
                values = _values(conn, item["original_card_id"], qty=item["qty"],
                                 acquired_date=acquired_date or item["acquired_date"] or "",
                                 acquired_price=acquired_price, acquired_currency=acquired_currency)
                conn.execute("""
                    UPDATE collection_lots SET acquired_date = ?, unit_price_minor = ?, currency = ?,
                        price_source = 'entered', priced_qty = qty, remaining_priced_qty = remaining_qty,
                        corrected_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?
                """, (values["acquired_date"], values["unit_price_minor"], values["currency"], item["id"]))
            recalculate(conn, collection_id)
            return True
        lot = next((item for item in lots if lot_id is None or item["id"] == lot_id), None)
        if lot is None:
            return False
        values = _values(conn, lot["original_card_id"], qty=lot["qty"], foil=bool(lot["foil"]),
                         acquired_date=acquired_date, acquired_price=acquired_price,
                         acquired_currency=acquired_currency, price_source=price_source,
                         estimate_confirmed=estimate_confirmed)
        if lot["price_source"] == "legacy_summary":
            if price_source == "market_estimate":
                raise ValueError("a legacy summary cannot be relabeled as a historical market purchase")
            values["price_source"] = "legacy_summary"
        priced = values["unit_price_minor"] is not None
        priced_qty = lot["priced_qty"] if lot["price_source"] == "legacy_summary" else lot["qty"]
        remaining_priced_qty = (lot["remaining_priced_qty"]
                    if lot["price_source"] == "legacy_summary" else lot["remaining_qty"])
        conn.execute("""
            UPDATE collection_lots SET acquired_date = ?, unit_price_minor = ?, currency = ?,
                price_source = ?, snapshot_date = ?, snapshot_source = ?,
                priced_qty = ?, remaining_priced_qty = ?,
                corrected_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?
        """, (values["acquired_date"], values["unit_price_minor"], values["currency"],
              values["price_source"], values["snapshot_date"], values["snapshot_source"],
              priced_qty if priced else 0, remaining_priced_qty if priced else 0, lot["id"]))
        recalculate(conn, collection_id)
    return True


def remove(collection_id: int, qty: int | None = None, *, game_code: str | None = None) -> bool:
    """Remove holdings FIFO by recorded lot ID; retain original purchase evidence."""
    cards.init_db()
    with cards.transaction() as conn:
        row = conn.execute("""
            SELECT col.* FROM collection col JOIN cards card ON card.id = col.card_id
             WHERE col.id = ? AND (? IS NULL OR card.game_code = ?)
        """, (collection_id, game_code, game_code)).fetchone()
        if not row:
            return False
        remaining = row["qty"] if qty is None else min(cards._positive_int(qty, "quantity"), row["qty"])
        lots = conn.execute(
            "SELECT * FROM collection_lots WHERE collection_id = ? ORDER BY id", (collection_id,)
        ).fetchall()
        for lot in lots:
            removed = min(remaining, lot["remaining_qty"])
            if not removed:
                continue
            new_qty = lot["remaining_qty"] - removed
            priced = lot["remaining_priced_qty"]
            new_priced = (priced * new_qty * 2 + lot["remaining_qty"]) // (2 * lot["remaining_qty"])
            conn.execute("""
                UPDATE collection_lots SET remaining_qty = ?, remaining_priced_qty = ?,
                    removed_at = CASE WHEN ? = 0 THEN strftime('%Y-%m-%dT%H:%M:%fZ','now')
                                      ELSE removed_at END WHERE id = ?
            """, (new_qty, new_priced, new_qty, lot["id"]))
            remaining -= removed
        recalculate(conn, collection_id)
    return True


def lots(game_code: str, collection_id: int | None = None, *, deleted: bool = False) -> list[dict[str, Any]]:
    cards.init_db()
    with cards._connect() as conn:
        rows = conn.execute("""
            SELECT lot.*, original.name AS original_name, original.set_code AS original_set,
                   original.collector_number AS original_number
              FROM collection_lots lot JOIN cards original ON original.id = lot.original_card_id
             WHERE original.game_code = ? AND
                   ((? = 1 AND lot.collection_id IS NULL) OR lot.collection_id = ?)
             ORDER BY lot.id DESC
        """, (game_code, int(deleted), collection_id)).fetchall()
    return [dict(row) for row in rows]