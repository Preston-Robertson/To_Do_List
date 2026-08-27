"""App-owned storage for trading-card catalogs, decks, and collection data."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
import xml.etree.ElementTree as ElementTree
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit

from .paths import CARDS_DB_PATH

SCHEMA_VERSION = 1
BOARDS = ("commander", "main", "side", "maybe")
CONDITIONS = ("NM", "LP", "MP", "HP", "DMG")
SUPPORTED_CURRENCIES = ("USD", "EUR")
_TRUSTED_IMAGE_HOSTS = {"cards.scryfall.io", "images.pokemontcg.io"}


def db_path() -> Path:
    configured = os.environ.get("LUIGI_WEB_CARDS_DB", "").strip()
    return Path(configured).expanduser().resolve() if configured else CARDS_DB_PATH


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Expose one atomic write boundary for multi-row domain operations."""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        yield conn


def init_db() -> None:
    """Create or migrate the isolated trading-card database."""
    with _connect() as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"cards database schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS games (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                sort_order INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_code TEXT NOT NULL REFERENCES games(code),
                external_id TEXT NOT NULL,
                source TEXT NOT NULL,
                name TEXT NOT NULL,
                set_code TEXT,
                set_name TEXT,
                collector_number TEXT,
                rarity TEXT,
                type_line TEXT,
                mana_cost TEXT,
                cmc REAL,
                colors_json TEXT NOT NULL DEFAULT '[]',
                color_identity_json TEXT NOT NULL DEFAULT '[]',
                oracle_text TEXT,
                image_small TEXT,
                image_normal TEXT,
                image_art_crop TEXT,
                price_usd_minor INTEGER CHECK (price_usd_minor >= 0),
                price_usd_foil_minor INTEGER CHECK (price_usd_foil_minor >= 0),
                price_eur_minor INTEGER CHECK (price_eur_minor >= 0),
                price_updated_at TEXT,
                raw_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(game_code, external_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cards_name
                ON cards(game_code, name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_cards_set
                ON cards(game_code, set_code, collector_number);

            CREATE TABLE IF NOT EXISTS decks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_code TEXT NOT NULL REFERENCES games(code),
                name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
                format TEXT,
                commander_card_id INTEGER REFERENCES cards(id) ON DELETE SET NULL,
                partner_card_id INTEGER REFERENCES cards(id) ON DELETE SET NULL,
                cover_card_id INTEGER REFERENCES cards(id) ON DELETE SET NULL,
                description TEXT,
                notes_xml TEXT,
                archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_decks_game
                ON decks(game_code, archived, updated_at DESC);

            CREATE TABLE IF NOT EXISTS deck_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
                card_id INTEGER NOT NULL REFERENCES cards(id),
                qty INTEGER NOT NULL DEFAULT 1 CHECK (qty > 0),
                category TEXT,
                board TEXT NOT NULL DEFAULT 'main'
                    CHECK (board IN ('main', 'side', 'maybe', 'commander')),
                UNIQUE(deck_id, card_id, board)
            );
            CREATE INDEX IF NOT EXISTS idx_deck_cards_deck ON deck_cards(deck_id);
            CREATE INDEX IF NOT EXISTS idx_deck_cards_card ON deck_cards(card_id);

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                color TEXT NOT NULL DEFAULT '#6c757d'
            );

            CREATE TABLE IF NOT EXISTS deck_tags (
                deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (deck_id, tag_id)
            );

            CREATE TABLE IF NOT EXISTS collection (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL REFERENCES cards(id),
                qty INTEGER NOT NULL DEFAULT 1 CHECK (qty > 0),
                foil INTEGER NOT NULL DEFAULT 0 CHECK (foil IN (0, 1)),
                condition TEXT NOT NULL DEFAULT 'NM'
                    CHECK (condition IN ('NM', 'LP', 'MP', 'HP', 'DMG')),
                acquired_date TEXT,
                acquired_price_minor INTEGER CHECK (acquired_price_minor >= 0),
                acquired_currency TEXT NOT NULL DEFAULT 'USD'
                    CHECK (acquired_currency IN ('USD', 'EUR')),
                notes TEXT,
                UNIQUE(card_id, foil, condition)
            );
            CREATE INDEX IF NOT EXISTS idx_collection_card ON collection(card_id);

            CREATE TABLE IF NOT EXISTS bulk_refresh (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_code TEXT NOT NULL REFERENCES games(code),
                source TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT,
                status TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'ok', 'error')),
                cards_upserted INTEGER,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS price_history (
                card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                snapshot_date TEXT NOT NULL,
                price_usd_minor INTEGER CHECK (price_usd_minor >= 0),
                price_usd_foil_minor INTEGER CHECK (price_usd_foil_minor >= 0),
                price_eur_minor INTEGER CHECK (price_eur_minor >= 0),
                PRIMARY KEY (card_id, snapshot_date)
            );
            CREATE INDEX IF NOT EXISTS idx_price_history_date
                ON price_history(snapshot_date);
            """
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO games(code, name, active, sort_order)
            VALUES (?, ?, 1, ?)
            """,
            (
                ("mtg", "Magic: The Gathering", 0),
                ("pokemon", "Pokemon TCG", 1),
                ("riftbound", "Riftbound", 2),
            ),
        )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def storage_health() -> str:
    init_db()
    with _connect() as conn:
        card_count = int(conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
    return f"trading-card database ready; {card_count} catalog cards"


def _text(value: Any, field: str, *, maximum: int, required: bool = False) -> str:
    cleaned = str(value or "").strip()
    if required and not cleaned:
        raise ValueError(f"{field} is required")
    if len(cleaned) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return cleaned


def _positive_int(value: Any, field: str, *, maximum: int = 9999) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a whole number") from exc
    if parsed < 1 or parsed > maximum:
        raise ValueError(f"{field} must be between 1 and {maximum}")
    return parsed


def to_minor(value: Any) -> int | None:
    """Convert a decimal money value to exact two-decimal minor units."""
    if value is None or str(value).strip() == "":
        return None
    try:
        amount = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("price must be a decimal amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("price must be a non-negative decimal amount")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def trusted_image_url(value: Any) -> str | None:
    """Return a browser-safe Scryfall image URL, or ``None`` if untrusted."""
    text = str(value or "").strip()
    if not text or any(char in text for char in "\"'()\\\r\n\t"):
        return None
    parsed = urlsplit(text)
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname not in _TRUSTED_IMAGE_HOSTS:
        return None
    if parsed.username or parsed.password or port not in (None, 443):
        return None
    return text


def list_games() -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT code, name FROM games WHERE active = 1 ORDER BY sort_order, name"
        ).fetchall()
    return [dict(row) for row in rows]


def get_game(game_code: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT code, name FROM games WHERE code = ? AND active = 1",
            (str(game_code).strip().lower(),),
        ).fetchone()
    return dict(row) if row else None


def catalog_stats(game_code: str = "mtg") -> dict[str, Any]:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS card_count,
                   COUNT(DISTINCT set_code) AS set_count,
                   MAX(price_updated_at) AS price_updated_at
              FROM cards WHERE game_code = ?
            """,
            (game_code,),
        ).fetchone()
    return dict(row)


def list_sets(game_code: str) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT set_code, MAX(set_name) AS set_name, COUNT(*) AS card_count
              FROM cards WHERE game_code = ? AND set_code IS NOT NULL
          GROUP BY set_code ORDER BY MAX(set_name) COLLATE NOCASE, set_code
            """,
            (game_code,),
        ).fetchall()
    return [dict(row) for row in rows]


def browse_catalog(
    game_code: str,
    *,
    query: str = "",
    set_code: str = "",
    page: int = 1,
    page_size: int = 48,
) -> dict[str, Any]:
    init_db()
    clean_query = _text(query, "search", maximum=100)
    clean_set = _text(set_code, "set code", maximum=20)
    safe_page = max(int(page), 1)
    safe_size = min(max(int(page_size), 12), 96)
    clauses = ["game_code = ?"]
    values: list[Any] = [game_code]
    if clean_query:
        escaped = clean_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("name LIKE ? ESCAPE '\\' COLLATE NOCASE")
        values.append(f"%{escaped}%")
    if clean_set:
        clauses.append("set_code = ? COLLATE NOCASE")
        values.append(clean_set)
    where = " AND ".join(clauses)
    with _connect() as conn:
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM cards WHERE {where}", values
        ).fetchone()[0])
        max_page = max(1, (total + safe_size - 1) // safe_size)
        safe_page = min(safe_page, max_page)
        rows = conn.execute(
            f"""
            SELECT id, name, set_code, set_name, collector_number, rarity,
                   type_line, mana_cost, image_small, image_normal,
                   price_usd_minor, price_usd_foil_minor, source
              FROM cards WHERE {where}
             ORDER BY name COLLATE NOCASE, set_code, collector_number
             LIMIT ? OFFSET ?
            """,
            (*values, safe_size, (safe_page - 1) * safe_size),
        ).fetchall()
    return {
        "rows": [dict(row) for row in rows],
        "total": total,
        "page": safe_page,
        "pages": max_page,
        "page_size": safe_size,
    }


def list_decks(game_code: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
    init_db()
    where = "" if include_archived else " AND d.archived = 0"
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT d.*,
                   cc.name AS commander_name,
                   cc.image_small AS commander_image,
                   cov.image_art_crop AS cover_image,
                   COALESCE(SUM(dc.qty), 0) AS card_count,
                   COALESCE(SUM(dc.qty * COALESCE(c.price_usd_minor, 0)), 0)
                       AS price_usd_minor
              FROM decks d
         LEFT JOIN cards cc ON cc.id = d.commander_card_id
         LEFT JOIN cards cov ON cov.id = d.cover_card_id
         LEFT JOIN deck_cards dc ON dc.deck_id = d.id
         LEFT JOIN cards c ON c.id = dc.card_id
             WHERE d.game_code = ?{where}
          GROUP BY d.id
          ORDER BY d.updated_at DESC, d.id DESC
            """,
            (game_code,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_deck(deck_id: int, game_code: str | None = None) -> dict[str, Any] | None:
    init_db()
    game_clause = " AND d.game_code = ?" if game_code else ""
    values: tuple[Any, ...] = (deck_id, game_code) if game_code else (deck_id,)
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT d.*,
                   cc.name AS commander_name,
                   cc.image_normal AS commander_image_normal,
                   cc.image_art_crop AS commander_art,
                   pc.name AS partner_name
              FROM decks d
         LEFT JOIN cards cc ON cc.id = d.commander_card_id
         LEFT JOIN cards pc ON pc.id = d.partner_card_id
             WHERE d.id = ?{game_clause}
            """,
            values,
        ).fetchone()
    return dict(row) if row else None


def list_deck_cards(deck_id: int) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT dc.id, dc.qty, dc.category, dc.board,
                   c.id AS card_id, c.name, c.mana_cost, c.cmc,
                   c.type_line, c.rarity, c.set_code, c.collector_number,
                   c.colors_json, c.color_identity_json,
                   c.image_small, c.image_normal, c.image_art_crop,
                   c.price_usd_minor, c.price_usd_foil_minor,
                   COALESCE((SELECT SUM(col.qty) FROM collection col
                              WHERE col.card_id = c.id), 0) AS owned_qty
              FROM deck_cards dc
              JOIN cards c ON c.id = dc.card_id
             WHERE dc.deck_id = ?
             ORDER BY CASE dc.board WHEN 'commander' THEN 0
                                    WHEN 'main' THEN 1
                                    WHEN 'side' THEN 2 ELSE 3 END,
                      COALESCE(dc.category, ''), c.cmc, c.name
            """,
            (deck_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_tags() -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM tags ORDER BY name COLLATE NOCASE").fetchall()
    return [dict(row) for row in rows]


def deck_tags(deck_id: int) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT t.* FROM tags t
              JOIN deck_tags dt ON dt.tag_id = t.id
             WHERE dt.deck_id = ? ORDER BY t.name COLLATE NOCASE
            """,
            (deck_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def search_cards(game_code: str, query: str, *, limit: int = 24) -> list[dict[str, Any]]:
    init_db()
    needle = _text(query, "search", maximum=100)
    if len(needle) < 2:
        return []
    safe_limit = min(max(int(limit), 1), 50)
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name, set_code, set_name, collector_number, mana_cost,
                   image_small, image_normal, price_usd_minor, type_line, rarity
              FROM cards
             WHERE game_code = ? AND name LIKE ? ESCAPE '\\' COLLATE NOCASE
             ORDER BY CASE WHEN name = ? COLLATE NOCASE THEN 0 ELSE 1 END,
                      name COLLATE NOCASE, set_code, collector_number
             LIMIT ?
            """,
            (game_code, f"%{escaped}%", needle, safe_limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_card(card_id: int, game_code: str | None = None) -> dict[str, Any] | None:
    init_db()
    game_clause = " AND game_code = ?" if game_code else ""
    values: tuple[Any, ...] = (card_id, game_code) if game_code else (card_id,)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT * FROM cards WHERE id = ?{game_clause}", values
        ).fetchone()
    return dict(row) if row else None


def list_collection(game_code: str, query: str = "", *, limit: int = 500) -> list[dict[str, Any]]:
    init_db()
    where = ""
    values: list[Any] = [game_code]
    needle = _text(query, "search", maximum=100)
    if needle:
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where = " AND c.name LIKE ? ESCAPE '\\' COLLATE NOCASE"
        values.append(f"%{escaped}%")
    values.append(min(max(int(limit), 1), 1000))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT col.id, col.qty, col.foil, col.condition, col.acquired_date,
                   col.acquired_price_minor, col.acquired_currency, col.notes,
                   c.id AS card_id, c.name, c.set_code, c.set_name,
                   c.collector_number, c.rarity, c.type_line,
                   c.image_small, c.image_normal,
                   c.price_usd_minor, c.price_usd_foil_minor,
                   (SELECT COUNT(DISTINCT dc.deck_id) FROM deck_cards dc
                     WHERE dc.card_id = c.id) AS in_decks
              FROM collection col
              JOIN cards c ON c.id = col.card_id
             WHERE c.game_code = ?{where}
             ORDER BY c.name COLLATE NOCASE, col.foil DESC LIMIT ?
            """,
            values,
        ).fetchall()
    return [dict(row) for row in rows]


def collection_totals(game_code: str) -> dict[str, int]:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(col.qty), 0) AS qty,
                   COALESCE(SUM(col.qty * CASE WHEN col.foil = 1
                       THEN COALESCE(c.price_usd_foil_minor, c.price_usd_minor, 0)
                       ELSE COALESCE(c.price_usd_minor, 0) END), 0)
                       AS value_usd_minor
              FROM collection col JOIN cards c ON c.id = col.card_id
             WHERE c.game_code = ?
            """,
            (game_code,),
        ).fetchone()
    return {"qty": int(row["qty"]), "value_usd_minor": int(row["value_usd_minor"])}


def _scryfall_values(payload: dict[str, Any]) -> tuple[Any, ...]:
    external_id = _text(payload.get("id"), "Scryfall card id", maximum=100, required=True)
    name = _text(payload.get("name"), "card name", maximum=300, required=True)
    prices = payload.get("prices") or {}
    images = payload.get("image_uris") or {}
    if not images and payload.get("card_faces"):
        images = (payload["card_faces"][0] or {}).get("image_uris") or {}
    return (
        external_id, name, payload.get("set"), payload.get("set_name"),
        payload.get("collector_number"), payload.get("rarity"),
        payload.get("type_line"), payload.get("mana_cost"), payload.get("cmc"),
        json.dumps(payload.get("colors") or [], separators=(",", ":")),
        json.dumps(payload.get("color_identity") or [], separators=(",", ":")),
        payload.get("oracle_text"), trusted_image_url(images.get("small")),
        trusted_image_url(images.get("normal")), trusted_image_url(images.get("art_crop")),
        to_minor(prices.get("usd")), to_minor(prices.get("usd_foil")),
        to_minor(prices.get("eur")), json.dumps(payload, separators=(",", ":")),
    )


def upsert_scryfall_cards(payloads: Iterable[dict[str, Any]]) -> int:
    """Upsert one batch atomically and return the number processed."""
    init_db()
    count = 0
    with transaction() as conn:
        for payload in payloads:
            conn.execute(
                """
                INSERT INTO cards (
                    game_code, external_id, source, name, set_code, set_name,
                    collector_number, rarity, type_line, mana_cost, cmc,
                    colors_json, color_identity_json, oracle_text,
                    image_small, image_normal, image_art_crop,
                    price_usd_minor, price_usd_foil_minor, price_eur_minor,
                    price_updated_at, raw_json
                ) VALUES ('mtg', ?, 'scryfall', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
                ON CONFLICT(game_code, external_id) DO UPDATE SET
                    source = excluded.source,
                    name = excluded.name,
                    set_code = excluded.set_code,
                    set_name = excluded.set_name,
                    collector_number = excluded.collector_number,
                    rarity = excluded.rarity,
                    type_line = excluded.type_line,
                    mana_cost = excluded.mana_cost,
                    cmc = excluded.cmc,
                    colors_json = excluded.colors_json,
                    color_identity_json = excluded.color_identity_json,
                    oracle_text = excluded.oracle_text,
                    image_small = excluded.image_small,
                    image_normal = excluded.image_normal,
                    image_art_crop = excluded.image_art_crop,
                    price_usd_minor = excluded.price_usd_minor,
                    price_usd_foil_minor = excluded.price_usd_foil_minor,
                    price_eur_minor = excluded.price_eur_minor,
                    price_updated_at = datetime('now'),
                    raw_json = excluded.raw_json,
                    updated_at = datetime('now')
                """,
                _scryfall_values(payload),
            )
            count += 1
    return count


def _pokemon_market(prices: dict[str, Any], *variants: str) -> Any:
    for variant in variants:
        detail = prices.get(variant) or {}
        if detail.get("market") not in (None, ""):
            return detail["market"]
    return None


def _pokemon_values(payload: dict[str, Any]) -> tuple[Any, ...]:
    external_id = _text(payload.get("id"), "Pokemon card id", maximum=100, required=True)
    name = _text(payload.get("name"), "card name", maximum=300, required=True)
    card_set = payload.get("set") or {}
    images = payload.get("images") or {}
    tcg_prices = ((payload.get("tcgplayer") or {}).get("prices") or {})
    cardmarket_prices = ((payload.get("cardmarket") or {}).get("prices") or {})
    subtypes = [str(value) for value in payload.get("subtypes") or []]
    type_line = " - ".join(
        value for value in (str(payload.get("supertype") or "").strip(), ", ".join(subtypes))
        if value
    )
    rules: list[str] = [str(value) for value in payload.get("rules") or []]
    for ability in payload.get("abilities") or []:
        rules.append(
            f"Ability - {ability.get('name', '')}: {ability.get('text', '')}".strip()
        )
    for attack in payload.get("attacks") or []:
        cost = ", ".join(str(value) for value in attack.get("cost") or [])
        damage = str(attack.get("damage") or "").strip()
        heading = " - ".join(value for value in (str(attack.get("name") or "").strip(), damage) if value)
        rules.append(f"Attack - {heading} ({cost}): {attack.get('text', '')}".strip())
    flavor = str(payload.get("flavorText") or "").strip()
    if flavor:
        rules.append(flavor)
    oracle_text = _text("\n".join(value for value in rules if value), "card text", maximum=10000)
    types = [str(value) for value in payload.get("types") or []]
    return (
        external_id, name, card_set.get("id"), card_set.get("name"),
        payload.get("number"), payload.get("rarity"), type_line or None,
        payload.get("convertedRetreatCost"), json.dumps(types, separators=(",", ":")),
        json.dumps(types, separators=(",", ":")), oracle_text or None,
        trusted_image_url(images.get("small")), trusted_image_url(images.get("large")),
        _pokemon_market(tcg_prices, "normal", "reverseHolofoil", "holofoil"),
        _pokemon_market(tcg_prices, "holofoil", "reverseHolofoil"),
        cardmarket_prices.get("averageSellPrice"),
        json.dumps(payload, separators=(",", ":")),
    )


def upsert_pokemon_cards(payloads: Iterable[dict[str, Any]]) -> int:
    """Upsert one Pokemon TCG API batch atomically."""
    init_db()
    count = 0
    with transaction() as conn:
        for payload in payloads:
            values = list(_pokemon_values(payload))
            values[13] = to_minor(values[13])
            values[14] = to_minor(values[14])
            values[15] = to_minor(values[15])
            conn.execute(
                """
                INSERT INTO cards (
                    game_code, external_id, source, name, set_code, set_name,
                    collector_number, rarity, type_line, cmc,
                    colors_json, color_identity_json, oracle_text,
                    image_small, image_normal,
                    price_usd_minor, price_usd_foil_minor, price_eur_minor,
                    price_updated_at, raw_json
                ) VALUES ('pokemon', ?, 'pokemontcg.io', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
                ON CONFLICT(game_code, external_id) DO UPDATE SET
                    source = excluded.source,
                    name = excluded.name,
                    set_code = excluded.set_code,
                    set_name = excluded.set_name,
                    collector_number = excluded.collector_number,
                    rarity = excluded.rarity,
                    type_line = excluded.type_line,
                    cmc = excluded.cmc,
                    colors_json = excluded.colors_json,
                    color_identity_json = excluded.color_identity_json,
                    oracle_text = excluded.oracle_text,
                    image_small = excluded.image_small,
                    image_normal = excluded.image_normal,
                    price_usd_minor = excluded.price_usd_minor,
                    price_usd_foil_minor = excluded.price_usd_foil_minor,
                    price_eur_minor = excluded.price_eur_minor,
                    price_updated_at = datetime('now'),
                    raw_json = excluded.raw_json,
                    updated_at = datetime('now')
                """,
                values,
            )
            count += 1
    return count


def create_manual_card(game_code: str, data: dict[str, Any]) -> int:
    """Create a private catalog entry for games without a bulk provider."""
    if not get_game(game_code):
        raise ValueError("unknown card game")
    name = _text(data.get("name"), "card name", maximum=300, required=True)
    with _connect() as conn:
        result = conn.execute(
            """
            INSERT INTO cards (
                game_code, external_id, source, name, set_code, set_name,
                collector_number, rarity, type_line, oracle_text
            ) VALUES (?, ?, 'manual', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                game_code, f"manual:{uuid.uuid4()}", name,
                _text(data.get("set_code"), "set code", maximum=20) or None,
                _text(data.get("set_name"), "set name", maximum=200) or None,
                _text(data.get("collector_number"), "collector number", maximum=40) or None,
                _text(data.get("rarity"), "rarity", maximum=50) or None,
                _text(data.get("type_line"), "type", maximum=300) or None,
                _text(data.get("oracle_text"), "card text", maximum=10000) or None,
            ),
        )
        return int(result.lastrowid)


def create_deck(
    game_code: str,
    name: str,
    format_: str = "",
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    clean_name = _text(name, "deck name", maximum=200, required=True)
    clean_format = _text(format_, "format", maximum=80)

    def write(active: sqlite3.Connection) -> int:
        if not active.execute(
            "SELECT 1 FROM games WHERE code = ? AND active = 1", (game_code,)
        ).fetchone():
            raise ValueError("unknown card game")
        result = active.execute(
            "INSERT INTO decks(game_code, name, format) VALUES (?, ?, ?)",
            (game_code, clean_name, clean_format or None),
        )
        return int(result.lastrowid)
    if conn is not None:
        return write(conn)
    init_db()
    with _connect() as active:
        return write(active)


def update_deck(deck_id: int, game_code: str, data: dict[str, Any]) -> bool:
    name = _text(data.get("name"), "deck name", maximum=200, required=True)
    format_ = _text(data.get("format"), "format", maximum=80)
    description = _text(data.get("description"), "description", maximum=10000)
    with _connect() as conn:
        existing = conn.execute(
            "SELECT archived, commander_card_id, cover_card_id FROM decks "
            "WHERE id = ? AND game_code = ?",
            (deck_id, game_code),
        ).fetchone()
        if not existing:
            return False
        archived = (
            1 if str(data.get("archived") or "").lower() in {"1", "true", "on"} else 0
        ) if "archived" in data else int(existing["archived"])
        commander_raw = data.get("commander_card_id")
        commander_id = (
            int(commander_raw) if str(commander_raw or "").isdigit() else None
        ) if "commander_card_id" in data else existing["commander_card_id"]
        cover_raw = data.get("cover_card_id")
        cover_id = (
            int(cover_raw) if str(cover_raw or "").isdigit() else None
        ) if "cover_card_id" in data else existing["cover_card_id"]
        for card_id, field in ((commander_id, "commander"), (cover_id, "cover")):
            if card_id is None:
                continue
            exists = conn.execute(
                "SELECT 1 FROM cards WHERE id = ? AND game_code = ?",
                (card_id, game_code),
            ).fetchone()
            if not exists:
                raise ValueError(f"{field} card does not belong to this game")
        result = conn.execute(
            """
            UPDATE decks SET name = ?, format = ?, description = ?, archived = ?,
                              commander_card_id = ?, cover_card_id = ?,
                              updated_at = datetime('now')
             WHERE id = ? AND game_code = ?
            """,
            (name, format_ or None, description or None, archived,
             commander_id, cover_id, deck_id, game_code),
        )
        return bool(result.rowcount)


def delete_deck(deck_id: int, game_code: str) -> bool:
    init_db()
    with _connect() as conn:
        result = conn.execute(
            "DELETE FROM decks WHERE id = ? AND game_code = ?", (deck_id, game_code)
        )
        return bool(result.rowcount)


def add_card_to_deck(
    deck_id: int,
    card_id: int,
    *,
    qty: int = 1,
    board: str = "main",
    category: str = "",
    conn: sqlite3.Connection | None = None,
) -> None:
    clean_qty = _positive_int(qty, "quantity")
    clean_board = str(board or "main").strip().lower()
    if clean_board not in BOARDS:
        raise ValueError("invalid deck board")
    clean_category = _text(category, "category", maximum=100) or None

    def write(active: sqlite3.Connection) -> None:
        row = active.execute(
            """
            SELECT d.game_code AS deck_game, c.game_code AS card_game
              FROM decks d CROSS JOIN cards c
             WHERE d.id = ? AND c.id = ?
            """,
            (deck_id, card_id),
        ).fetchone()
        if not row:
            raise ValueError("deck or card was not found")
        if row["deck_game"] != row["card_game"]:
            raise ValueError("card and deck must belong to the same game")
        active.execute(
            """
            INSERT INTO deck_cards(deck_id, card_id, qty, board, category)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(deck_id, card_id, board) DO UPDATE SET
                qty = deck_cards.qty + excluded.qty,
                category = COALESCE(excluded.category, deck_cards.category)
            """,
            (deck_id, card_id, clean_qty, clean_board, clean_category),
        )
        if clean_board == "commander":
            active.execute(
                "UPDATE decks SET commander_card_id = ? WHERE id = ?",
                (card_id, deck_id),
            )
        active.execute(
            "UPDATE decks SET updated_at = datetime('now') WHERE id = ?", (deck_id,)
        )

    if conn is not None:
        write(conn)
    else:
        init_db()
        with _connect() as active:
            write(active)


def update_deck_card(deck_id: int, deck_card_id: int, qty: int, category: str = "") -> bool:
    clean_category = _text(category, "category", maximum=100) or None
    try:
        clean_qty = int(qty)
    except (TypeError, ValueError) as exc:
        raise ValueError("quantity must be a whole number") from exc
    init_db()
    with _connect() as conn:
        if clean_qty <= 0:
            result = conn.execute(
                "DELETE FROM deck_cards WHERE id = ? AND deck_id = ?",
                (deck_card_id, deck_id),
            )
        else:
            if clean_qty > 9999:
                raise ValueError("quantity must be at most 9999")
            result = conn.execute(
                "UPDATE deck_cards SET qty = ?, category = ? WHERE id = ? AND deck_id = ?",
                (clean_qty, clean_category, deck_card_id, deck_id),
            )
        if result.rowcount:
            conn.execute(
                "UPDATE decks SET updated_at = datetime('now') WHERE id = ?", (deck_id,)
            )
        return bool(result.rowcount)


def remove_deck_card(deck_id: int, deck_card_id: int) -> bool:
    return update_deck_card(deck_id, deck_card_id, 0)


def set_deck_tags(deck_id: int, tag_names: Iterable[str]) -> None:
    names = sorted({_text(name, "tag", maximum=40) for name in tag_names if str(name).strip()})
    if len(names) > 20:
        raise ValueError("a deck can have at most 20 tags")
    init_db()
    with transaction() as conn:
        if not conn.execute("SELECT 1 FROM decks WHERE id = ?", (deck_id,)).fetchone():
            raise ValueError("deck not found")
        conn.execute("DELETE FROM deck_tags WHERE deck_id = ?", (deck_id,))
        for name in names:
            conn.execute("INSERT INTO tags(name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
            tag_id = conn.execute("SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)).fetchone()[0]
            conn.execute(
                "INSERT INTO deck_tags(deck_id, tag_id) VALUES (?, ?)", (deck_id, tag_id)
            )


def add_to_collection(
    card_id: int,
    *,
    qty: int = 1,
    foil: bool = False,
    condition: str = "NM",
    acquired_date: str = "",
    acquired_price: Any = None,
    acquired_currency: str = "USD",
    notes: str = "",
) -> None:
    clean_qty = _positive_int(qty, "quantity")
    clean_condition = str(condition or "NM").strip().upper()
    if clean_condition not in CONDITIONS:
        raise ValueError("invalid card condition")
    currency = str(acquired_currency or "USD").strip().upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError("unsupported acquisition currency")
    clean_date = str(acquired_date or "").strip()
    if clean_date:
        try:
            clean_date = date.fromisoformat(clean_date).isoformat()
        except ValueError as exc:
            raise ValueError("acquired date must be YYYY-MM-DD") from exc
    clean_notes = _text(notes, "notes", maximum=5000) or None
    price_minor = to_minor(acquired_price)
    init_db()
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM cards WHERE id = ?", (card_id,)).fetchone():
            raise ValueError("card not found")
        existing = conn.execute(
            """
            SELECT acquired_price_minor, acquired_currency FROM collection
             WHERE card_id = ? AND foil = ? AND condition = ?
            """,
            (card_id, int(bool(foil)), clean_condition),
        ).fetchone()
        if (
            existing
            and price_minor is not None
            and existing["acquired_price_minor"] is not None
            and existing["acquired_currency"] != currency
        ):
            raise ValueError("acquisition currency must match the existing entry")
        conn.execute(
            """
            INSERT INTO collection(
                card_id, qty, foil, condition, acquired_date, acquired_price_minor,
                acquired_currency, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(card_id, foil, condition) DO UPDATE SET
                qty = collection.qty + excluded.qty,
                acquired_date = COALESCE(
                    excluded.acquired_date, collection.acquired_date
                ),
                acquired_price_minor = COALESCE(
                    excluded.acquired_price_minor, collection.acquired_price_minor
                ),
                acquired_currency = CASE WHEN excluded.acquired_price_minor IS NULL
                    THEN collection.acquired_currency ELSE excluded.acquired_currency END,
                notes = COALESCE(excluded.notes, collection.notes)
            """,
              (card_id, clean_qty, int(bool(foil)), clean_condition,
               clean_date or None, price_minor, currency, clean_notes),
        )


def remove_from_collection(
    collection_id: int,
    qty: int | None = None,
    *,
    game_code: str | None = None,
) -> bool:
    init_db()
    with _connect() as conn:
        game_join = " JOIN cards c ON c.id = col.card_id" if game_code else ""
        game_where = " AND c.game_code = ?" if game_code else ""
        values: tuple[Any, ...] = (
            (collection_id, game_code) if game_code else (collection_id,)
        )
        row = conn.execute(
            f"SELECT col.qty FROM collection col{game_join} "
            f"WHERE col.id = ?{game_where}",
            values,
        ).fetchone()
        if not row:
            return False
        if qty is None or _positive_int(qty, "quantity") >= int(row["qty"]):
            conn.execute("DELETE FROM collection WHERE id = ?", (collection_id,))
        else:
            conn.execute(
                "UPDATE collection SET qty = qty - ? WHERE id = ?",
                (int(qty), collection_id),
            )
        return True


def _validated_notes_xml(value: Any) -> str:
    xml = str(value or "").strip()
    if not xml:
        return ""
    if len(xml.encode("utf-8")) > 1_000_000:
        raise ValueError("diagram notes must be at most 1 MB")
    lowered = xml.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ValueError("diagram notes cannot contain XML declarations")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ValueError("diagram notes are not valid XML") from exc
    root_name = root.tag.rsplit("}", 1)[-1]
    if root_name not in {"mxfile", "mxGraphModel"}:
        raise ValueError("diagram notes must be a draw.io document")
    forbidden_tags = {"script", "iframe", "object", "embed"}
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() in forbidden_tags:
            raise ValueError("diagram notes contain a forbidden element")
        for attribute in element.attrib.values():
            normalized = str(attribute).strip().lower()
            if normalized.startswith(("javascript:", "data:text/html")):
                raise ValueError("diagram notes contain a forbidden URL")
    return xml


def save_deck_notes(deck_id: int, game_code: str, xml: Any) -> int:
    validated = _validated_notes_xml(xml)
    init_db()
    with _connect() as conn:
        result = conn.execute(
            """
            UPDATE decks SET notes_xml = ?, updated_at = datetime('now')
             WHERE id = ? AND game_code = ?
            """,
            (validated or None, deck_id, game_code),
        )
        if not result.rowcount:
            raise ValueError("deck not found")
    return len(validated.encode("utf-8"))


def deck_export_text(deck_id: int) -> str:
    rows = list_deck_cards(deck_id)
    sections: list[str] = []
    labels = {"commander": "Commander", "main": "Mainboard", "side": "Sideboard", "maybe": "Maybeboard"}
    for board in BOARDS:
        board_rows = [row for row in rows if row["board"] == board]
        if not board_rows:
            continue
        if sections:
            sections.append("")
        sections.append(labels[board])
        for row in board_rows:
            printing = f" ({row['set_code'].upper()})" if row.get("set_code") else ""
            number = f" {row['collector_number']}" if row.get("collector_number") else ""
            sections.append(f"{row['qty']} {row['name']}{printing}{number}")
    return "\n".join(sections) + ("\n" if sections else "")


def find_card(
    game_code: str,
    name: str,
    *,
    set_code: str | None = None,
    collector_number: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    clean_name = _text(name, "card name", maximum=300, required=True)

    def read(active: sqlite3.Connection) -> sqlite3.Row | None:
        if set_code and collector_number:
            row = active.execute(
                """
                SELECT * FROM cards WHERE game_code = ? AND name = ? COLLATE NOCASE
                 AND set_code = ? COLLATE NOCASE AND collector_number = ? LIMIT 1
                """,
                (game_code, clean_name, set_code, collector_number),
            ).fetchone()
            if row:
                return row
        if set_code:
            row = active.execute(
                """
                SELECT * FROM cards WHERE game_code = ? AND name = ? COLLATE NOCASE
                 AND set_code = ? COLLATE NOCASE ORDER BY collector_number LIMIT 1
                """,
                (game_code, clean_name, set_code),
            ).fetchone()
            if row:
                return row
        return active.execute(
            """
            SELECT * FROM cards WHERE game_code = ? AND name = ? COLLATE NOCASE
             ORDER BY id DESC LIMIT 1
            """,
            (game_code, clean_name),
        ).fetchone()

    if conn is not None:
        row = read(conn)
    else:
        init_db()
        with _connect() as active:
            row = read(active)
    return dict(row) if row else None


def refresh_start(game_code: str, source: str) -> int:
    init_db()
    with _connect() as conn:
        result = conn.execute(
            "INSERT INTO bulk_refresh(game_code, source) VALUES (?, ?)",
            (game_code, _text(source, "refresh source", maximum=100, required=True)),
        )
        return int(result.lastrowid)


def refresh_finish(refresh_id: int, cards_upserted: int, error: str | None = None) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE bulk_refresh SET finished_at = datetime('now'), status = ?,
                                    cards_upserted = ?, error = ? WHERE id = ?
            """,
            ("error" if error else "ok", max(0, int(cards_upserted)),
             _text(error, "refresh error", maximum=2000) or None, refresh_id),
        )


def mark_interrupted_refreshes() -> int:
    init_db()
    with _connect() as conn:
        result = conn.execute(
            """
            UPDATE bulk_refresh SET status = 'error', finished_at = datetime('now'),
                                    error = 'Refresh interrupted by application restart'
             WHERE status = 'running'
            """
        )
        return int(result.rowcount)


def last_refresh(game_code: str = "mtg") -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM bulk_refresh WHERE game_code = ? ORDER BY id DESC LIMIT 1",
            (game_code,),
        ).fetchone()
    return dict(row) if row else None


def snapshot_prices(game_code: str = "mtg") -> int:
    init_db()
    with transaction() as conn:
        result = conn.execute(
            """
            INSERT OR IGNORE INTO price_history(
                card_id, snapshot_date, price_usd_minor,
                price_usd_foil_minor, price_eur_minor
            )
            SELECT c.id, date('now'), c.price_usd_minor,
                   c.price_usd_foil_minor, c.price_eur_minor
              FROM cards c
             WHERE c.game_code = ?
               AND (c.price_usd_minor IS NOT NULL
                    OR c.price_usd_foil_minor IS NOT NULL
                    OR c.price_eur_minor IS NOT NULL)
               AND NOT EXISTS (
                    SELECT 1 FROM price_history ph
                     WHERE ph.card_id = c.id
                       AND ph.snapshot_date = (
                           SELECT MAX(previous.snapshot_date)
                             FROM price_history previous
                            WHERE previous.card_id = c.id
                       )
                       AND ph.price_usd_minor IS c.price_usd_minor
                       AND ph.price_usd_foil_minor IS c.price_usd_foil_minor
                       AND ph.price_eur_minor IS c.price_eur_minor
               )
            """,
            (game_code,),
        )
        return max(0, int(result.rowcount or 0))


def card_price_history(card_id: int, *, days: int = 180) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT snapshot_date, price_usd_minor, price_usd_foil_minor
              FROM price_history WHERE card_id = ?
               AND snapshot_date >= date('now', ?)
             ORDER BY snapshot_date
            """,
            (card_id, f"-{min(max(int(days), 1), 3650)} days"),
        ).fetchall()
    return [dict(row) for row in rows]


def deck_price_series(deck_id: int, *, days: int = 180) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            WITH deck AS (
                SELECT card_id, qty FROM deck_cards WHERE deck_id = ?
            ), dates AS (
                SELECT DISTINCT snapshot_date FROM price_history
                 WHERE snapshot_date >= date('now', ?)
            )
            SELECT dates.snapshot_date,
                   COALESCE(SUM(deck.qty * history.price_usd_minor), 0)
                       AS value_usd_minor
              FROM dates JOIN deck ON 1 = 1
              JOIN price_history history
                ON history.card_id = deck.card_id
               AND history.snapshot_date = (
                    SELECT MAX(previous.snapshot_date) FROM price_history previous
                     WHERE previous.card_id = deck.card_id
                       AND previous.snapshot_date <= dates.snapshot_date
               )
             GROUP BY dates.snapshot_date ORDER BY dates.snapshot_date
            """,
            (deck_id, f"-{min(max(int(days), 1), 3650)} days"),
        ).fetchall()
    return [dict(row) for row in rows]