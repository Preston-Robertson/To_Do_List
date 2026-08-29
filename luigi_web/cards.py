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

SCHEMA_VERSION = 2
BOARDS = ("commander", "main", "side", "maybe")
CONDITIONS = ("NM", "LP", "MP", "HP", "DMG")
SUPPORTED_CURRENCIES = ("USD", "EUR")
MTG_COLORS = ("W", "U", "B", "R", "G", "C")
MTG_GAMES = ("paper", "arena", "mtgo")
MTG_FORMATS = (
    "standard", "future", "historic", "timeless", "gladiator", "pioneer",
    "explorer", "modern", "legacy", "pauper", "vintage", "penny",
    "commander", "oathbreaker", "standardbrawl", "brawl", "alchemy",
    "paupercommander", "duel", "oldschool", "premodern", "predh",
)
MTG_RARITIES = ("common", "uncommon", "rare", "mythic", "special", "bonus")
MTG_CRITERIA_OPTIONS = (
    ("adventure", "Adventure"), ("arenaid", "Arena ID"),
    ("artseries", "Art Series"), ("artist", "Artist"),
    ("artistmisprint", "Artist Misprint"), ("lights", "Attraction Lights"),
    ("atypical", "Atypical"), ("augmentation", "Augment"),
    ("back", "Back"), ("bear", "Bear"), ("beginnerbox", "Beginner Box"),
    ("booster", "Booster"), ("borderless", "Borderless"),
    ("brawlcommander", "Brawl Commander"), ("buyabox", "Buy-a-Box"),
    ("cardmarket", "Cardmarket ID"), ("class", "Class Layout"),
    ("ci", "Color Indicator"), ("colorshifted", "Colorshifted"),
    ("commander", "Commander"), ("companion", "Companion"),
    ("contentwarning", "Content Warning"), ("covered", "Covered"),
    ("manland", "Creature Land"), ("datestamped", "Datestamped"),
    ("default", "Default"), ("digital", "Digital"),
    ("doublesided", "Double Sided"), ("duelcommander", "Duel Commander"),
    ("etb", "ETB"), ("englishart", "English Art"), ("etch", "Etched"),
    ("extended", "Extended Art"), ("extra", "Extra"),
    ("ff", "Final Fantasy"), ("firstprint", "First Printing"),
    ("flavorname", "Flavor Name"), ("flavor", "Flavor Text"),
    ("flip", "Flip"), ("foil", "Foil"), ("fbb", "Foreign Black Border"),
    ("fwb", "Foreign White Border"), ("frenchvanilla", "French Vanilla"),
    ("fullart", "Full Art"), ("funny", "Funny"), ("future", "Future"),
    ("gamechanger", "Game Changer"), ("gameday", "Game Day"),
    ("hires", "High Resolution"), ("historic", "Historic"),
    ("splitmana", "Hybrid Mana"), ("illustration", "Illustration"),
    ("intropack", "Intro Pack"), ("invitational", "Invitational Card"),
    ("leveler", "Leveler"), ("localizedname", "Localized Name"),
    ("mtgoid", "MTGO ID"), ("masterpiece", "Masterpiece"),
    ("meld", "Meld"), ("modal", "Modal"),
    ("mdfc", "Modal Double Faced"), ("modern", "Modern"),
    ("multiverse", "Multiverse ID"), ("new", "New"),
    ("nonfoil", "Nonfoil"), ("notuniversesbeyond", "Not Universes Beyond"),
    ("oathbreaker", "Oathbreaker"), ("old", "Old Frame"),
    ("outlaw", "Outlaw"), ("oversized", "Oversized"),
    ("partner", "Paired Commander"), ("paperart", "Paper Art"),
    ("party", "Party"), ("permanent", "Permanent"),
    ("phyrexia", "Phyrexian Mana"), ("planar", "Planar"),
    ("planeswalkerdeck", "Planeswalker Deck"), ("prepare", "Prepare"),
    ("prerelease", "Prerelease Promo"), ("printedtext", "Printed Text"),
    ("promo", "Promo"), ("related", "Related Cards"),
    ("release", "Release Promo"), ("reprint", "Reprint"),
    ("reserved", "Reserved List"), ("resource", "Resource ID"),
    ("reversible", "Reversible"), ("stamp", "Security Stamp"),
    ("showcase", "Showcase"), ("spell", "Spell"),
    ("spellbook", "Spellbook"), ("spikey", "Spikey"),
    ("split", "Split Card"), ("stamped", "Stamped"),
    ("startercollection", "Starter Collection"),
    ("starterdeck", "Starter Deck"), ("story", "Story Spotlight"),
    ("tcgplayer", "TCGplayer ID"), ("textless", "Textless"),
    ("token", "Token"), ("tombstone", "Tombstone"),
    ("transform", "Transform"), ("translucent", "Translucent"),
    ("onlyprint", "Unique Printing"), ("universesbeyond", "Universes Beyond"),
    ("vanilla", "Vanilla"), ("variation", "Variation"),
    ("watermark", "Watermark"), ("worthy", "Worthy"),
)
MTG_CRITERIA = tuple(value for value, _label in MTG_CRITERIA_OPTIONS)
CATALOG_SORTS = (
    "name", "released", "set", "rarity", "color", "usd", "tix", "eur",
    "cmc", "power", "toughness", "artist", "collector",
)
CATALOG_PREFERS = (
    "best", "newest", "oldest", "promo", "default", "atypical",
    "universesbeyond", "notuniversesbeyond", "usdlow", "usdhigh",
    "eurlow", "eurhigh", "tixlow", "tixhigh",
)
MTG_LANGUAGES = (
    "en", "es", "fr", "de", "it", "pt", "ja", "ko", "ru", "zhs",
    "zht", "he", "la", "grc", "ar", "sa", "ph", "qya", "dw", "tlh",
)
_TRUSTED_IMAGE_HOSTS = {"cards.scryfall.io", "images.pokemontcg.io"}
_EXTRA_CARD_LAYOUTS = (
    "token", "double_faced_token", "emblem", "art_series", "scheme",
    "planar", "vanguard",
)
_TRUSTED_CARD_LINK_HOSTS = {
    "scryfall.com",
    "www.scryfall.com",
    "www.tcgplayer.com",
    "shop.tcgplayer.com",
    "www.cardmarket.com",
    "gatherer.wizards.com",
}


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
                category TEXT NOT NULL DEFAULT '',
                board TEXT NOT NULL DEFAULT 'main'
                    CHECK (board IN ('main', 'side', 'maybe', 'commander')),
                UNIQUE(deck_id, card_id, board, category)
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
        if 0 < version < 2:
            conn.executescript(
                """
                BEGIN IMMEDIATE;
                ALTER TABLE deck_cards RENAME TO deck_cards_v1;
                CREATE TABLE deck_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
                    card_id INTEGER NOT NULL REFERENCES cards(id),
                    qty INTEGER NOT NULL DEFAULT 1 CHECK (qty > 0),
                    category TEXT NOT NULL DEFAULT '',
                    board TEXT NOT NULL DEFAULT 'main'
                        CHECK (board IN ('main', 'side', 'maybe', 'commander')),
                    UNIQUE(deck_id, card_id, board, category)
                );
                INSERT INTO deck_cards(id, deck_id, card_id, qty, category, board)
                SELECT id, deck_id, card_id, qty, COALESCE(category, ''), board
                  FROM deck_cards_v1;
                DROP TABLE deck_cards_v1;
                CREATE INDEX idx_deck_cards_deck ON deck_cards(deck_id);
                CREATE INDEX idx_deck_cards_card ON deck_cards(card_id);
                COMMIT;
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
    filters: dict[str, Any] | None = None,
    page: int = 1,
    page_size: int = 48,
) -> dict[str, Any]:
    init_db()
    requested = dict(filters or {})
    if query and not requested.get("q"):
        requested["q"] = query
    if set_code and not requested.get("set_code"):
        requested["set_code"] = set_code
    normalized = normalize_catalog_filters(game_code, requested)
    safe_page = max(int(page), 1)
    safe_size = min(max(int(page_size), 12), 96)
    clauses = ["c.game_code = ?"]
    values: list[Any] = [game_code]
    _catalog_filter_clauses(normalized, clauses, values)
    where = " AND ".join(clauses)
    partition = {
        "cards": "COALESCE(json_extract(c.raw_json, '$.oracle_id'), LOWER(c.name))",
        "art": "COALESCE(json_extract(c.raw_json, '$.illustration_id'), json_extract(c.raw_json, '$.oracle_id'), LOWER(c.name))",
    }.get(normalized["unique"])
    rank_sql = (
        f"ROW_NUMBER() OVER (PARTITION BY {partition} ORDER BY {_catalog_prefer_sql(normalized['prefer'])}, c.id)"
        if partition else "1"
    )
    source_sql = f"(SELECT c.*, {rank_sql} AS catalog_rank FROM cards c WHERE {where}) c"
    order_sql = _catalog_order_sql(normalized["order"], normalized["direction"])
    with _connect() as conn:
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM {source_sql} WHERE c.catalog_rank = 1", values
        ).fetchone()[0])
        max_page = max(1, (total + safe_size - 1) // safe_size)
        safe_page = min(safe_page, max_page)
        rows = conn.execute(
            f"""
            SELECT c.id, c.name, c.set_code, c.set_name, c.collector_number,
                   c.rarity, c.type_line, c.mana_cost, c.image_small,
                   c.image_normal, c.price_usd_minor,
                   c.price_usd_foil_minor, c.source
                FROM {source_sql} WHERE c.catalog_rank = 1
             ORDER BY {order_sql}, c.id
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
        "filters": normalized,
        "active_filter_count": _active_catalog_filter_count(normalized),
    }


def _filter_list(value: Any, allowed: tuple[str, ...]) -> list[str]:
    source = value if isinstance(value, (list, tuple, set)) else [value]
    allowed_values = {option.lower() for option in allowed}
    return list(dict.fromkeys(
        text for item in source
        if (text := str(item or "").strip().lower()) in allowed_values
    ))


def _filter_text(value: Any, field: str, maximum: int = 200) -> str:
    return _text(value, field, maximum=maximum)


def _filter_decimal(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite number")
    return str(parsed)


def normalize_catalog_filters(game_code: str, values: dict[str, Any]) -> dict[str, Any]:
    """Validate public catalog filters into a fixed query contract."""
    operator = str(values.get("stat_operator") or "=").strip()
    price_operator = str(values.get("price_operator") or "<=").strip()
    legal_status = str(values.get("legal_status") or "legal").strip().lower()
    color_mode = str(values.get("color_mode") or "including").strip().lower()
    normalized = {
        "q": _filter_text(values.get("q"), "card name", 100),
        "oracle": _filter_text(values.get("oracle"), "rules text", 500),
        "type_line": _filter_text(values.get("type_line"), "type line", 200),
        "type_mode": "not" if str(values.get("type_mode") or "is").lower() == "not" else "is",
        "colors": _filter_list(values.get("colors"), MTG_COLORS),
        "color_mode": color_mode if color_mode in {"including", "exact", "at_most"} else "including",
        "identity": _filter_list(values.get("identity"), MTG_COLORS),
        "mana_cost": _filter_text(values.get("mana_cost"), "mana cost", 100),
        "stat": str(values.get("stat") or "").strip().lower(),
        "stat_operator": operator if operator in {"=", "!=", "<", "<=", ">", ">="} else "=",
        "stat_value": _filter_decimal(values.get("stat_value"), "stat value"),
        "games": _filter_list(values.get("games"), MTG_GAMES),
        "games_mode": "not" if str(values.get("games_mode") or "is").lower() == "not" else "is",
        "format": str(values.get("format") or "").strip().lower(),
        "legal_status": legal_status if legal_status in {"legal", "restricted", "banned", "not_legal"} else "legal",
        "set_code": _filter_text(values.get("set_code"), "set code", 20).lower(),
        "group": _filter_text(values.get("group"), "block or group", 100),
        "rarities": _filter_list(values.get("rarities"), MTG_RARITIES),
        "criteria": _filter_list(values.get("criteria"), MTG_CRITERIA),
        "include_extras": str(values.get("include_extras") or "").strip().lower()
            in {"1", "true", "yes", "on"},
        "price_currency": str(values.get("price_currency") or "usd").strip().lower(),
        "price_operator": price_operator if price_operator in {"=", "!=", "<", "<=", ">", ">="} else "<=",
        "price_value": _filter_decimal(values.get("price_value"), "price"),
        "artist": _filter_text(values.get("artist"), "artist", 200),
        "flavor": _filter_text(values.get("flavor"), "flavor text", 300),
        "lore": _filter_text(values.get("lore"), "lore", 300),
        "language": _filter_text(values.get("language"), "language", 10).lower(),
        "order": str(values.get("order") or "name").strip().lower(),
        "direction": "desc" if str(values.get("direction") or "asc").lower() == "desc" else "asc",
        "unique": str(values.get("unique") or "prints").strip().lower(),
        "prefer": str(values.get("prefer") or "best").strip().lower(),
    }
    if normalized["stat"] not in {"cmc", "power", "toughness", "loyalty", "defense"}:
        normalized["stat"] = ""
    if normalized["format"] not in MTG_FORMATS:
        normalized["format"] = ""
    if normalized["price_currency"] not in {"usd", "usd_foil", "eur", "tix"}:
        normalized["price_currency"] = "usd"
    if normalized["price_value"] and Decimal(normalized["price_value"]) < 0:
        raise ValueError("price must be non-negative")
    if normalized["order"] not in CATALOG_SORTS:
        normalized["order"] = "name"
    if normalized["unique"] not in {"prints", "cards", "art"}:
        normalized["unique"] = "prints"
    if normalized["prefer"] not in CATALOG_PREFERS:
        normalized["prefer"] = "best"
    if normalized["unique"] == "prints":
        normalized["prefer"] = "best"
    if set(normalized["criteria"]) & {"extra", "token", "artseries", "planar"}:
        normalized["include_extras"] = True
    if normalized["language"] not in MTG_LANGUAGES:
        normalized["language"] = ""
    if not normalized["type_line"]:
        normalized["type_mode"] = "is"
    if not normalized["colors"]:
        normalized["color_mode"] = "including"
    if not normalized["games"]:
        normalized["games_mode"] = "is"
    if not normalized["format"]:
        normalized["legal_status"] = "legal"
    if not normalized["stat"] or not normalized["stat_value"]:
        normalized["stat"] = ""
        normalized["stat_operator"] = "="
        normalized["stat_value"] = ""
    if not normalized["price_value"]:
        normalized["price_currency"] = "usd"
        normalized["price_operator"] = "<="
    if game_code != "mtg":
        for key in (
            "colors", "identity", "mana_cost", "games", "format", "group",
            "criteria", "artist", "flavor", "lore", "language",
        ):
            normalized[key] = [] if key in {"colors", "identity", "games", "criteria"} else ""
        normalized["unique"] = "prints"
        normalized["prefer"] = "best"
        normalized["include_extras"] = True
    return normalized


def _like_terms(value: str) -> list[str]:
    return [term for term in value.split() if term][:20]


def _escaped_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _catalog_filter_clauses(filters: dict[str, Any], clauses: list[str], values: list[Any]) -> None:
    if not filters["include_extras"]:
        clauses.append(f"NOT ({_extra_card_sql('c')})")
    text_fields = {
        "q": "c.name",
        "oracle": "COALESCE(c.oracle_text, '')",
        "artist": "COALESCE(json_extract(c.raw_json, '$.artist'), '')",
        "flavor": "COALESCE(json_extract(c.raw_json, '$.flavor_text'), '')",
    }
    for key, expression in text_fields.items():
        for term in _like_terms(filters[key]):
            clauses.append(f"{expression} LIKE ? ESCAPE '\\' COLLATE NOCASE")
            values.append(f"%{_escaped_like(term)}%")
    if filters["type_line"]:
        clause = "COALESCE(c.type_line, '') LIKE ? ESCAPE '\\' COLLATE NOCASE"
        clauses.append(f"NOT ({clause})" if filters["type_mode"] == "not" else clause)
        values.append(f"%{_escaped_like(filters['type_line'])}%")
    _catalog_color_clauses("c.colors_json", filters["colors"], filters["color_mode"], clauses, values)
    _catalog_identity_clauses(filters["identity"], clauses, values)
    if filters["mana_cost"]:
        clauses.append("COALESCE(c.mana_cost, '') = ? COLLATE NOCASE")
        values.append(filters["mana_cost"])
    if filters["stat"] and filters["stat_value"]:
        stat_expressions = {
            "cmc": "c.cmc",
            "power": "CAST(json_extract(c.raw_json, '$.power') AS REAL)",
            "toughness": "CAST(json_extract(c.raw_json, '$.toughness') AS REAL)",
            "loyalty": "CAST(json_extract(c.raw_json, '$.loyalty') AS REAL)",
            "defense": "CAST(json_extract(c.raw_json, '$.defense') AS REAL)",
        }
        clauses.append(f"{stat_expressions[filters['stat']]} IS NOT NULL AND {stat_expressions[filters['stat']]} {filters['stat_operator']} ?")
        values.append(filters["stat_value"])
    for game in filters["games"]:
        game_clause = "EXISTS (SELECT 1 FROM json_each(c.raw_json, '$.games') WHERE value = ?)"
        clauses.append(f"NOT ({game_clause})" if filters["games_mode"] == "not" else game_clause)
        values.append(game)
    if filters["format"]:
        clauses.append("json_extract(c.raw_json, ?) = ?")
        values.extend((f"$.legalities.{filters['format']}", filters["legal_status"]))
    if filters["set_code"]:
        clauses.append("c.set_code = ? COLLATE NOCASE")
        values.append(filters["set_code"])
    if filters["group"]:
        pattern = f"%{_escaped_like(filters['group'])}%"
        clauses.append("(COALESCE(json_extract(c.raw_json, '$.block'), '') LIKE ? ESCAPE '\\' COLLATE NOCASE OR COALESCE(json_extract(c.raw_json, '$.set_type'), '') LIKE ? ESCAPE '\\' COLLATE NOCASE)")
        values.extend((pattern, pattern))
    if filters["rarities"]:
        placeholders = ",".join("?" for _ in filters["rarities"])
        clauses.append(f"LOWER(COALESCE(c.rarity, '')) IN ({placeholders})")
        values.extend(filters["rarities"])
    _catalog_criteria_clauses(filters["criteria"], clauses)
    if filters["price_value"]:
        columns = {
            "usd": ("c.price_usd_minor", True),
            "usd_foil": ("c.price_usd_foil_minor", True),
            "eur": ("c.price_eur_minor", True),
            "tix": ("CAST(json_extract(c.raw_json, '$.prices.tix') AS REAL)", False),
        }
        expression, minor_units = columns[filters["price_currency"]]
        clauses.append(f"{expression} IS NOT NULL AND {expression} {filters['price_operator']} ?")
        values.append(to_minor(filters["price_value"]) if minor_units else filters["price_value"])
    if filters["lore"]:
        for term in _like_terms(filters["lore"]):
            pattern = f"%{_escaped_like(term)}%"
            clauses.append("(c.name LIKE ? ESCAPE '\\' COLLATE NOCASE OR COALESCE(c.type_line, '') LIKE ? ESCAPE '\\' COLLATE NOCASE OR COALESCE(c.oracle_text, '') LIKE ? ESCAPE '\\' COLLATE NOCASE OR c.raw_json LIKE ? ESCAPE '\\' COLLATE NOCASE)")
            values.extend((pattern, pattern, pattern, pattern))
    if filters["language"]:
        clauses.append("json_extract(c.raw_json, '$.lang') = ?")
        values.append(filters["language"])


def _catalog_color_clauses(expression: str, selected: list[str], mode: str, clauses: list[str], values: list[Any]) -> None:
    colors = [color.upper() for color in selected if color.upper() != "C"]
    wants_colorless = "c" in selected
    if not selected:
        return
    if wants_colorless and not colors:
        clauses.append(f"json_array_length({expression}) = 0")
        return
    if mode in {"including", "exact"}:
        for color in colors:
            clauses.append(f"EXISTS (SELECT 1 FROM json_each({expression}) WHERE value = ?)")
            values.append(color)
    if mode == "exact":
        clauses.append(f"json_array_length({expression}) = ?")
        values.append(len(colors))
    elif mode == "at_most":
        placeholders = ",".join("?" for _ in colors)
        if colors:
            clauses.append(f"NOT EXISTS (SELECT 1 FROM json_each({expression}) WHERE value NOT IN ({placeholders}))")
            values.extend(colors)
        else:
            clauses.append(f"json_array_length({expression}) = 0")


def _catalog_identity_clauses(selected: list[str], clauses: list[str], values: list[Any]) -> None:
    if not selected:
        return
    colors = [color.upper() for color in selected if color.upper() != "C"]
    if not colors:
        clauses.append("json_array_length(c.color_identity_json) = 0")
        return
    placeholders = ",".join("?" for _ in colors)
    clauses.append(f"NOT EXISTS (SELECT 1 FROM json_each(c.color_identity_json) WHERE value NOT IN ({placeholders}))")
    values.extend(colors)


def _catalog_criteria_clauses(criteria: list[str], clauses: list[str]) -> None:
    expressions = {
        "adventure": _layout_sql("adventure"),
        "arenaid": _json_present("arena_id"),
        "artseries": _layout_sql("art_series"),
        "artist": _json_present("artist"),
        "artistmisprint": _array_has("promo_types", "artist_misprint"),
        "lights": "json_array_length(json_extract(c.raw_json, '$.attraction_lights')) > 0",
        "atypical": "(json_array_length(json_extract(c.raw_json, '$.frame_effects')) > 0 OR json_extract(c.raw_json, '$.frame') NOT IN ('1993','1997','2003','2015'))",
        "augmentation": _layout_sql("augment"),
        "back": "json_extract(c.raw_json, '$.layout') IN ('transform','modal_dfc','double_faced_token','reversible_card')",
        "bear": "c.type_line LIKE '%Creature%' AND c.cmc = 2 AND json_extract(c.raw_json, '$.power') = '2' AND json_extract(c.raw_json, '$.toughness') = '2'",
        "beginnerbox": _array_has("promo_types", "beginner_box"),
        "booster": _json_true("booster"),
        "borderless": "json_extract(c.raw_json, '$.border_color') = 'borderless'",
        "brawlcommander": _array_has("promo_types", "brawlcommander"),
        "buyabox": _array_has("promo_types", "buyabox"),
        "cardmarket": _json_present("cardmarket_id"),
        "class": _layout_sql("class"),
        "ci": "json_array_length(json_extract(c.raw_json, '$.color_indicator')) > 0",
        "colorshifted": _array_has("frame_effects", "colorshifted"),
        "commander": "json_extract(c.raw_json, '$.legalities.commander') = 'legal' AND (c.type_line LIKE '%Legendary%Creature%' OR COALESCE(c.oracle_text, '') LIKE '%can be your commander%')",
        "companion": _array_has("keywords", "Companion"),
        "contentwarning": _json_true("content_warning"),
        "covered": _array_has("promo_types", "covered"),
        "manland": "c.type_line LIKE '%Land%' AND COALESCE(c.oracle_text, '') LIKE '%becomes%creature%'",
        "datestamped": _array_has("promo_types", "datestamped"),
        "default": "json_array_length(json_extract(c.raw_json, '$.frame_effects')) IS NULL AND COALESCE(json_extract(c.raw_json, '$.border_color'), 'black') = 'black'",
        "digital": _json_true("digital"),
        "doublesided": "json_array_length(json_extract(c.raw_json, '$.card_faces')) > 1",
        "duelcommander": "json_extract(c.raw_json, '$.legalities.duel') = 'legal'",
        "etb": "COALESCE(c.oracle_text, '') LIKE '%enters%'",
        "englishart": "json_extract(c.raw_json, '$.lang') = 'en'",
        "etch": _array_has("finishes", "etched"),
        "extended": _array_has("frame_effects", "extendedart"),
        "extra": _extra_card_sql("c"),
        "ff": "(LOWER(c.set_code) IN ('fin','fic','fca') OR COALESCE(json_extract(c.raw_json, '$.block'), '') LIKE '%Final Fantasy%')",
        "firstprint": "COALESCE(json_extract(c.raw_json, '$.reprint'), 0) = 0",
        "flavorname": _json_present("flavor_name"),
        "flavor": _json_present("flavor_text"),
        "flip": _layout_sql("flip"),
        "foil": _json_true("foil"),
        "fbb": "json_extract(c.raw_json, '$.lang') != 'en' AND json_extract(c.raw_json, '$.border_color') = 'black'",
        "fwb": "json_extract(c.raw_json, '$.lang') != 'en' AND json_extract(c.raw_json, '$.border_color') = 'white'",
        "frenchvanilla": "c.type_line LIKE '%Creature%' AND COALESCE(c.oracle_text, '') != '' AND COALESCE(c.oracle_text, '') NOT LIKE '%Whenever%' AND COALESCE(c.oracle_text, '') NOT LIKE '%When %' AND COALESCE(c.oracle_text, '') NOT LIKE '%:%'",
        "fullart": _json_true("full_art"),
        "funny": "(json_extract(c.raw_json, '$.security_stamp') = 'acorn' OR json_extract(c.raw_json, '$.border_color') = 'silver')",
        "future": "json_extract(c.raw_json, '$.frame') = 'future'",
        "gamechanger": _json_true("game_changer"),
        "gameday": _array_has("promo_types", "gameday"),
        "hires": _json_true("highres_image"),
        "historic": "json_extract(c.raw_json, '$.legalities.historic') = 'legal'",
        "splitmana": "COALESCE(c.mana_cost, '') LIKE '%/%'",
        "illustration": "json_extract(c.raw_json, '$.image_uris.art_crop') IS NOT NULL",
        "intropack": _array_has("promo_types", "intropack"),
        "invitational": _array_has("promo_types", "invitational"),
        "leveler": _layout_sql("leveler"),
        "localizedname": _json_present("printed_name"),
        "mtgoid": _json_present("mtgo_id"),
        "masterpiece": "json_extract(c.raw_json, '$.set_type') = 'masterpiece'",
        "meld": _layout_sql("meld"),
        "modal": "json_extract(c.raw_json, '$.layout') IN ('modal_dfc','adventure','split')",
        "mdfc": _layout_sql("modal_dfc"),
        "modern": "json_extract(c.raw_json, '$.legalities.modern') = 'legal'",
        "multiverse": "json_array_length(json_extract(c.raw_json, '$.multiverse_ids')) > 0",
        "new": "COALESCE(json_extract(c.raw_json, '$.reprint'), 0) = 0",
        "nonfoil": _json_true("nonfoil"),
        "notuniversesbeyond": "COALESCE(json_extract(c.raw_json, '$.set_type'), '') != 'universes_beyond'",
        "oathbreaker": "json_extract(c.raw_json, '$.legalities.oathbreaker') = 'legal'",
        "old": "json_extract(c.raw_json, '$.frame') IN ('1993','1997')",
        "outlaw": "c.type_line LIKE '%Assassin%' OR c.type_line LIKE '%Mercenary%' OR c.type_line LIKE '%Pirate%' OR c.type_line LIKE '%Rogue%' OR c.type_line LIKE '%Warlock%'",
        "oversized": _json_true("oversized"),
        "partner": "COALESCE(c.oracle_text, '') LIKE '%Partner%' OR COALESCE(c.oracle_text, '') LIKE '%Friends forever%'",
        "paperart": "EXISTS (SELECT 1 FROM json_each(c.raw_json, '$.games') WHERE value = 'paper') AND c.image_normal IS NOT NULL",
        "party": "c.type_line LIKE '%Cleric%' OR c.type_line LIKE '%Rogue%' OR c.type_line LIKE '%Warrior%' OR c.type_line LIKE '%Wizard%'",
        "permanent": "c.type_line NOT LIKE '%Instant%' AND c.type_line NOT LIKE '%Sorcery%'",
        "phyrexia": "COALESCE(c.mana_cost, '') LIKE '%/P}%'",
        "planar": "c.type_line LIKE '%Plane%' OR c.type_line LIKE '%Phenomenon%'",
        "planeswalkerdeck": _array_has("promo_types", "planeswalkerdeck"),
        "prepare": "c.name LIKE 'Prepare //%'",
        "prerelease": _array_has("promo_types", "prerelease"),
        "printedtext": _json_present("printed_text"),
        "promo": _json_true("promo"),
        "related": "json_array_length(json_extract(c.raw_json, '$.all_parts')) > 0",
        "release": _array_has("promo_types", "release"),
        "reprint": _json_true("reprint"),
        "reserved": _json_true("reserved"),
        "resource": _json_present("resource_id"),
        "reversible": _layout_sql("reversible_card"),
        "stamp": _json_present("security_stamp"),
        "showcase": _array_has("frame_effects", "showcase"),
        "spell": "c.type_line NOT LIKE '%Land%'",
        "spellbook": _array_has("promo_types", "spellbook"),
        "spikey": "c.name LIKE '%Spike,%' OR c.name LIKE '%Spikey%'",
        "split": _layout_sql("split"),
        "stamped": _json_present("security_stamp"),
        "startercollection": _array_has("promo_types", "starter_collection"),
        "starterdeck": _array_has("promo_types", "starter_deck"),
        "story": _json_true("story_spotlight"),
        "tcgplayer": _json_present("tcgplayer_id"),
        "textless": _json_true("textless"),
        "token": "json_extract(c.raw_json, '$.layout') IN ('token','double_faced_token')",
        "tombstone": _array_has("frame_effects", "tombstone"),
        "transform": _layout_sql("transform"),
        "translucent": _array_has("frame_effects", "translucent"),
        "onlyprint": "(SELECT COUNT(*) FROM cards c3 WHERE c3.game_code = c.game_code AND COALESCE(json_extract(c3.raw_json, '$.oracle_id'), LOWER(c3.name)) = COALESCE(json_extract(c.raw_json, '$.oracle_id'), LOWER(c.name))) = 1",
        "universesbeyond": "json_extract(c.raw_json, '$.set_type') = 'universes_beyond'",
        "vanilla": "c.type_line LIKE '%Creature%' AND COALESCE(c.oracle_text, '') = ''",
        "variation": _json_true("variation"),
        "watermark": _json_present("watermark"),
        "worthy": "COALESCE(json_extract(c.raw_json, '$.edhrec_rank'), 999999999) <= 1000",
    }
    clauses.extend(f"({expressions[item]})" for item in criteria)


def _json_true(field: str) -> str:
    return f"json_extract(c.raw_json, '$.{field}') = 1"


def _json_present(field: str) -> str:
    return f"json_extract(c.raw_json, '$.{field}') IS NOT NULL"


def _layout_sql(layout: str) -> str:
    return f"json_extract(c.raw_json, '$.layout') = '{layout}'"


def _array_has(field: str, value: str) -> str:
    return f"EXISTS (SELECT 1 FROM json_each(c.raw_json, '$.{field}') WHERE value = '{value}')"


def _extra_card_sql(alias: str) -> str:
    layouts = ",".join(f"'{layout}'" for layout in _EXTRA_CARD_LAYOUTS)
    return (
        f"COALESCE(json_extract({alias}.raw_json, '$.layout') IN ({layouts}), 0) = 1 "
        f"OR COALESCE(json_extract({alias}.raw_json, '$.set_type'), '') = 'token'"
    )


def _catalog_order_sql(order: str, direction: str) -> str:
    expressions = {
        "name": "c.name COLLATE NOCASE",
        "set": "COALESCE(c.set_name, c.set_code, '') COLLATE NOCASE",
        "released": "COALESCE(json_extract(c.raw_json, '$.released_at'), '')",
        "rarity": "CASE LOWER(c.rarity) WHEN 'common' THEN 1 WHEN 'uncommon' THEN 2 WHEN 'rare' THEN 3 WHEN 'mythic' THEN 4 ELSE 5 END",
        "color": "c.colors_json",
        "usd": "COALESCE(c.price_usd_minor, -1)",
        "eur": "COALESCE(c.price_eur_minor, -1)",
        "tix": "COALESCE(CAST(json_extract(c.raw_json, '$.prices.tix') AS REAL), -1)",
        "cmc": "COALESCE(c.cmc, -1)",
        "power": "CAST(json_extract(c.raw_json, '$.power') AS REAL)",
        "toughness": "CAST(json_extract(c.raw_json, '$.toughness') AS REAL)",
        "artist": "COALESCE(json_extract(c.raw_json, '$.artist'), '') COLLATE NOCASE",
        "collector": "COALESCE(CAST(c.collector_number AS INTEGER), 999999999), c.collector_number COLLATE NOCASE",
    }
    return f"{expressions[order]} {'DESC' if direction == 'desc' else 'ASC'}"


def _catalog_prefer_sql(prefer: str) -> str:
    expressions = {
        "best": "0",
        "newest": "COALESCE(json_extract(c.raw_json, '$.released_at'), '') DESC",
        "oldest": "COALESCE(json_extract(c.raw_json, '$.released_at'), '9999-99-99') ASC",
        "promo": "COALESCE(json_extract(c.raw_json, '$.promo'), 0) DESC",
        "default": "CASE WHEN json_array_length(json_extract(c.raw_json, '$.frame_effects')) IS NULL THEN 0 ELSE 1 END",
        "atypical": "CASE WHEN json_array_length(json_extract(c.raw_json, '$.frame_effects')) IS NULL THEN 1 ELSE 0 END",
        "universesbeyond": "CASE WHEN json_extract(c.raw_json, '$.set_type') = 'universes_beyond' THEN 0 ELSE 1 END",
        "notuniversesbeyond": "CASE WHEN json_extract(c.raw_json, '$.set_type') = 'universes_beyond' THEN 1 ELSE 0 END",
        "usdlow": "CASE WHEN c.price_usd_minor IS NULL THEN 1 ELSE 0 END, c.price_usd_minor ASC",
        "usdhigh": "CASE WHEN c.price_usd_minor IS NULL THEN 1 ELSE 0 END, c.price_usd_minor DESC",
        "eurlow": "CASE WHEN c.price_eur_minor IS NULL THEN 1 ELSE 0 END, c.price_eur_minor ASC",
        "eurhigh": "CASE WHEN c.price_eur_minor IS NULL THEN 1 ELSE 0 END, c.price_eur_minor DESC",
        "tixlow": "CASE WHEN json_extract(c.raw_json, '$.prices.tix') IS NULL THEN 1 ELSE 0 END, CAST(json_extract(c.raw_json, '$.prices.tix') AS REAL) ASC",
        "tixhigh": "CASE WHEN json_extract(c.raw_json, '$.prices.tix') IS NULL THEN 1 ELSE 0 END, CAST(json_extract(c.raw_json, '$.prices.tix') AS REAL) DESC",
    }
    return expressions[prefer]


def _active_catalog_filter_count(filters: dict[str, Any]) -> int:
    defaults = {
        "type_mode": "is", "color_mode": "including", "stat_operator": "=",
        "legal_status": "legal", "price_currency": "usd",
        "price_operator": "<=", "order": "name", "direction": "asc",
        "unique": "prints", "prefer": "best", "games_mode": "is",
        "include_extras": False,
    }
    return sum(
        1 for key, value in filters.items()
        if value not in ("", [], None) and value != defaults.get(key)
    )


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


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _trusted_card_link(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or any(char in text for char in "\"'()\\\r\n\t"):
        return None
    parsed = urlsplit(text)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _TRUSTED_CARD_LINK_HOSTS
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        return None
    return text


def _card_faces(card: dict[str, Any], raw: dict[str, Any]) -> list[dict[str, Any]]:
    raw_faces = raw.get("card_faces")
    if not isinstance(raw_faces, list) or not raw_faces:
        raw_faces = [raw]
    faces: list[dict[str, Any]] = []
    for index, value in enumerate(raw_faces):
        face = value if isinstance(value, dict) else {}
        images = face.get("image_uris") or raw.get("image_uris") or raw.get("images") or {}
        image_normal = trusted_image_url(
            images.get("normal") or images.get("large") or card.get("image_normal")
        )
        image_small = trusted_image_url(images.get("small") or card.get("image_small"))
        faces.append({
            "name": face.get("name") or card["name"],
            "mana_cost": face.get("mana_cost") or (card.get("mana_cost") if index == 0 else None),
            "type_line": face.get("type_line") or card.get("type_line"),
            "oracle_text": face.get("oracle_text") or (card.get("oracle_text") if index == 0 else None),
            "flavor_text": face.get("flavor_text") or (
                (raw.get("flavor_text") or raw.get("flavorText"))
                if index == 0 else None
            ),
            "artist": face.get("artist") or raw.get("artist"),
            "power": face.get("power") or (raw.get("power") if index == 0 else None),
            "toughness": face.get("toughness") or (raw.get("toughness") if index == 0 else None),
            "loyalty": face.get("loyalty") or (raw.get("loyalty") if index == 0 else None),
            "defense": face.get("defense") or (raw.get("defense") if index == 0 else None),
            "image_normal": image_normal or image_small,
            "image_small": image_small or image_normal,
        })
    return faces


def card_detail(card_id: int, game_code: str) -> dict[str, Any] | None:
    """Return rich provider data plus local deck and collection usage."""
    card = get_card(card_id, game_code)
    if not card:
        return None
    raw = _json_object(card.get("raw_json"))
    purchase_uris = raw.get("purchase_uris") if isinstance(raw.get("purchase_uris"), dict) else {}
    links = [
        {"label": "Scryfall", "url": _trusted_card_link(raw.get("scryfall_uri"))},
        {"label": "TCGplayer", "url": _trusted_card_link(purchase_uris.get("tcgplayer"))},
        {"label": "Cardmarket", "url": _trusted_card_link(purchase_uris.get("cardmarket"))},
        {"label": "Gatherer", "url": _trusted_card_link(purchase_uris.get("gatherer"))},
    ]
    legalities = raw.get("legalities") if isinstance(raw.get("legalities"), dict) else {}
    with _connect() as conn:
        printings = conn.execute(
            """
                 SELECT id, set_code, set_name, collector_number, rarity,
                     image_small, image_normal, price_usd_minor,
                     price_usd_foil_minor,
                     json_extract(raw_json, '$.illustration_id') AS illustration_id,
                     json_extract(raw_json, '$.released_at') AS released_at,
                     json_extract(raw_json, '$.finishes') AS finishes_json
              FROM cards WHERE game_code = ? AND name = ? COLLATE NOCASE
             ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END,
                      COALESCE(json_extract(raw_json, '$.released_at'), '') DESC,
                      set_code, collector_number LIMIT 100
            """,
            (game_code, card["name"], card_id),
        ).fetchall()
        decks = conn.execute(
            """
            SELECT d.id, d.name, d.format, dc.qty, dc.board, dc.category
              FROM deck_cards dc JOIN decks d ON d.id = dc.deck_id
             WHERE dc.card_id = ? ORDER BY d.name COLLATE NOCASE
            """,
            (card_id,),
        ).fetchall()
        collection = conn.execute(
            """
            SELECT id, qty, foil, condition, acquired_date,
                   acquired_price_minor, acquired_currency, notes
              FROM collection WHERE card_id = ?
             ORDER BY foil DESC, condition
            """,
            (card_id,),
        ).fetchall()
    public_card = {key: value for key, value in card.items() if key != "raw_json"}
    printing_rows = [dict(row) for row in printings]
    art_count = len({
        row["illustration_id"] or f"printing:{row['id']}"
        for row in printing_rows
    })
    return {
        **public_card,
        "faces": _card_faces(card, raw),
        "released_at": raw.get("released_at"),
        "set_type": raw.get("set_type"),
        "lang": raw.get("lang"),
        "artist": raw.get("artist"),
        "flavor_text": raw.get("flavor_text") or raw.get("flavorText"),
        "layout": raw.get("layout"),
        "keywords": raw.get("keywords") if isinstance(raw.get("keywords"), list) else [],
        "games": raw.get("games") if isinstance(raw.get("games"), list) else [],
        "finishes": raw.get("finishes") if isinstance(raw.get("finishes"), list) else [],
        "legalities": legalities,
        "legal_groups": {
            status: sorted(name.replace("_", " ").title() for name, value in legalities.items() if value == status)
            for status in ("legal", "restricted", "banned", "not_legal")
        },
        "links": [link for link in links if link["url"]],
        "printings": printing_rows,
        "art_count": art_count,
        "decks": [dict(row) for row in decks],
        "collection": [dict(row) for row in collection],
        "pokemon": {
            "hp": raw.get("hp"),
            "evolves_from": raw.get("evolvesFrom"),
            "evolves_to": raw.get("evolvesTo") if isinstance(raw.get("evolvesTo"), list) else [],
            "regulation_mark": raw.get("regulationMark"),
            "pokedex_numbers": raw.get("nationalPokedexNumbers") if isinstance(raw.get("nationalPokedexNumbers"), list) else [],
            "weaknesses": raw.get("weaknesses") if isinstance(raw.get("weaknesses"), list) else [],
            "resistances": raw.get("resistances") if isinstance(raw.get("resistances"), list) else [],
            "retreat_cost": raw.get("retreatCost") if isinstance(raw.get("retreatCost"), list) else [],
        },
    }


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
    clean_category = _text(category, "category", maximum=100)

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
            ON CONFLICT(deck_id, card_id, board, category) DO UPDATE SET
                qty = deck_cards.qty + excluded.qty
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
    clean_category = _text(category, "category", maximum=100)
    try:
        clean_qty = int(qty)
    except (TypeError, ValueError) as exc:
        raise ValueError("quantity must be a whole number") from exc
    init_db()
    with _connect() as conn:
        source = conn.execute(
            "SELECT * FROM deck_cards WHERE id = ? AND deck_id = ?",
            (deck_card_id, deck_id),
        ).fetchone()
        if not source:
            return False
        if clean_qty <= 0:
            result = conn.execute(
                "DELETE FROM deck_cards WHERE id = ? AND deck_id = ?",
                (deck_card_id, deck_id),
            )
        else:
            if clean_qty > 9999:
                raise ValueError("quantity must be at most 9999")
            destination = conn.execute(
                """
                SELECT id, qty FROM deck_cards
                 WHERE deck_id = ? AND card_id = ? AND board = ?
                   AND category = ? AND id != ?
                """,
                (
                    deck_id, source["card_id"], source["board"],
                    clean_category, deck_card_id,
                ),
            ).fetchone()
            if destination:
                total_qty = int(destination["qty"]) + clean_qty
                if total_qty > 9999:
                    raise ValueError("combined deck quantity must be at most 9999")
                conn.execute(
                    "UPDATE deck_cards SET qty = ? WHERE id = ?",
                    (total_qty, destination["id"]),
                )
                result = conn.execute(
                    "DELETE FROM deck_cards WHERE id = ? AND deck_id = ?",
                    (deck_card_id, deck_id),
                )
            else:
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


def get_deck_card(
    deck_id: int,
    deck_card_id: int,
    game_code: str | None = None,
) -> dict[str, Any] | None:
    init_db()
    game_clause = " AND d.game_code = ?" if game_code else ""
    values: tuple[Any, ...] = (
        (deck_id, deck_card_id, game_code)
        if game_code else (deck_id, deck_card_id)
    )
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT dc.*, d.game_code, c.name AS card_name
              FROM deck_cards dc
              JOIN decks d ON d.id = dc.deck_id
              JOIN cards c ON c.id = dc.card_id
             WHERE dc.deck_id = ? AND dc.id = ?{game_clause}
            """,
            values,
        ).fetchone()
    return dict(row) if row else None


def _printing_family(card: sqlite3.Row | dict[str, Any]) -> tuple[str, str]:
    raw = _json_object(card["raw_json"])
    oracle_id = str(raw.get("oracle_id") or "").strip()
    if oracle_id:
        return "oracle", oracle_id
    return "name", str(card["name"]).strip().casefold()


def _validate_printing_swap(
    conn: sqlite3.Connection,
    source_card_id: int,
    target_card_id: int,
    game_code: str,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    source = conn.execute(
        "SELECT * FROM cards WHERE id = ? AND game_code = ?",
        (source_card_id, game_code),
    ).fetchone()
    target = conn.execute(
        "SELECT * FROM cards WHERE id = ? AND game_code = ?",
        (target_card_id, game_code),
    ).fetchone()
    if not source or not target:
        raise ValueError("source or target printing was not found")
    if _printing_family(source) != _printing_family(target):
        raise ValueError("target card is not an alternate printing of this card")
    return source, target


def printings_are_compatible(
    source_card_id: int,
    target_card_id: int,
    game_code: str,
) -> bool:
    init_db()
    with _connect() as conn:
        try:
            _validate_printing_swap(
                conn, source_card_id, target_card_id, game_code
            )
        except ValueError:
            return False
    return True


def swap_deck_card_printing(
    deck_id: int,
    deck_card_id: int,
    target_card_id: int,
    game_code: str,
) -> dict[str, Any]:
    """Replace one deck slot's printing, merging an existing target slot."""
    init_db()
    with transaction() as conn:
        source = conn.execute(
            """
            SELECT dc.*, d.game_code
              FROM deck_cards dc JOIN decks d ON d.id = dc.deck_id
             WHERE dc.id = ? AND dc.deck_id = ? AND d.game_code = ?
            """,
            (deck_card_id, deck_id, game_code),
        ).fetchone()
        if not source:
            raise ValueError("deck card not found")
        _validate_printing_swap(
            conn, int(source["card_id"]), int(target_card_id), game_code
        )
        if int(source["card_id"]) == int(target_card_id):
            return {"deck_card_id": deck_card_id, "merged": False}

        destination = conn.execute(
            """
            SELECT * FROM deck_cards
             WHERE deck_id = ? AND card_id = ? AND board = ? AND category = ?
            """,
            (
                deck_id, target_card_id, source["board"], source["category"],
            ),
        ).fetchone()
        if destination:
            total_qty = int(destination["qty"]) + int(source["qty"])
            if total_qty > 9999:
                raise ValueError("combined deck quantity must be at most 9999")
            conn.execute(
                "UPDATE deck_cards SET qty = ?, category = ? WHERE id = ?",
                (
                    total_qty,
                    source["category"] or destination["category"],
                    destination["id"],
                ),
            )
            conn.execute("DELETE FROM deck_cards WHERE id = ?", (deck_card_id,))
            result_id = int(destination["id"])
            merged = True
        else:
            conn.execute(
                "UPDATE deck_cards SET card_id = ? WHERE id = ?",
                (target_card_id, deck_card_id),
            )
            result_id = deck_card_id
            merged = False

        conn.execute(
            """
            UPDATE decks SET
                commander_card_id = CASE WHEN commander_card_id = ? THEN ? ELSE commander_card_id END,
                partner_card_id = CASE WHEN partner_card_id = ? THEN ? ELSE partner_card_id END,
                cover_card_id = CASE WHEN cover_card_id = ? THEN ? ELSE cover_card_id END,
                updated_at = datetime('now')
             WHERE id = ? AND game_code = ?
            """,
            (
                source["card_id"], target_card_id,
                source["card_id"], target_card_id,
                source["card_id"], target_card_id,
                deck_id, game_code,
            ),
        )
        return {"deck_card_id": result_id, "merged": merged}


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


def get_collection_entry(
    collection_id: int,
    game_code: str | None = None,
) -> dict[str, Any] | None:
    init_db()
    game_clause = " AND c.game_code = ?" if game_code else ""
    values: tuple[Any, ...] = (
        (collection_id, game_code) if game_code else (collection_id,)
    )
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT col.*, c.game_code, c.name AS card_name
              FROM collection col JOIN cards c ON c.id = col.card_id
             WHERE col.id = ?{game_clause}
            """,
            values,
        ).fetchone()
    return dict(row) if row else None


def _merged_notes(first: Any, second: Any) -> str | None:
    values = []
    for value in (first, second):
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in values:
            values.append(cleaned)
    return "\n".join(values) or None


def _weighted_acquisition_price(
    source: sqlite3.Row,
    destination: sqlite3.Row,
) -> tuple[int | None, str]:
    priced = [
        row for row in (source, destination)
        if row["acquired_price_minor"] is not None
    ]
    currencies = {str(row["acquired_currency"]) for row in priced}
    if len(currencies) > 1:
        raise ValueError(
            "existing target collection entry uses a different acquisition currency"
        )
    if not priced:
        return None, str(destination["acquired_currency"] or source["acquired_currency"])
    currency = currencies.pop()
    weighted_total = sum(
        int(row["acquired_price_minor"]) * int(row["qty"]) for row in priced
    )
    priced_qty = sum(int(row["qty"]) for row in priced)
    average = int(
        (Decimal(weighted_total) / Decimal(priced_qty)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    return average, currency


def swap_collection_printing(
    collection_id: int,
    target_card_id: int,
    game_code: str,
) -> dict[str, Any]:
    """Replace a collection row's printing, merging an existing variant row."""
    init_db()
    with transaction() as conn:
        source = conn.execute(
            """
            SELECT col.* FROM collection col
              JOIN cards c ON c.id = col.card_id
             WHERE col.id = ? AND c.game_code = ?
            """,
            (collection_id, game_code),
        ).fetchone()
        if not source:
            raise ValueError("collection entry not found")
        _validate_printing_swap(
            conn, int(source["card_id"]), int(target_card_id), game_code
        )
        if int(source["card_id"]) == int(target_card_id):
            return {"collection_id": collection_id, "merged": False}

        destination = conn.execute(
            """
            SELECT * FROM collection
             WHERE card_id = ? AND foil = ? AND condition = ?
            """,
            (target_card_id, source["foil"], source["condition"]),
        ).fetchone()
        if destination:
            total_qty = int(destination["qty"]) + int(source["qty"])
            if total_qty > 9999:
                raise ValueError("combined collection quantity must be at most 9999")
            price_minor, currency = _weighted_acquisition_price(
                source, destination
            )
            dates = [
                str(value) for value in (
                    source["acquired_date"], destination["acquired_date"]
                ) if value
            ]
            conn.execute(
                """
                UPDATE collection SET qty = ?, acquired_date = ?,
                    acquired_price_minor = ?, acquired_currency = ?, notes = ?
                 WHERE id = ?
                """,
                (
                    total_qty,
                    min(dates) if dates else None,
                    price_minor,
                    currency,
                    _merged_notes(destination["notes"], source["notes"]),
                    destination["id"],
                ),
            )
            conn.execute("DELETE FROM collection WHERE id = ?", (collection_id,))
            result_id = int(destination["id"])
            merged = True
        else:
            conn.execute(
                "UPDATE collection SET card_id = ? WHERE id = ?",
                (target_card_id, collection_id),
            )
            result_id = collection_id
            merged = False
        return {"collection_id": result_id, "merged": merged}


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
        category_groups: dict[str, list[dict[str, Any]]] = {}
        for row in board_rows:
            category_groups.setdefault(
                str(row.get("category") or ""), []
            ).append(row)
        ordered_groups = sorted(
            category_groups.items(),
            key=lambda item: (
                bool(item[0]),
                min(int(row["id"]) for row in item[1]),
            ),
        )
        for category, category_rows in ordered_groups:
            if category:
                sections.append(f"[{category}]")
            for row in category_rows:
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