"""Bounded, local deck snapshots in the existing Trading Cards database."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from . import repository as cards

SNAPSHOT_VERSION = 1
MAX_SNAPSHOT_BYTES = 2_000_000
MAX_VERSIONS = 100
DECK_FIELDS = (
    "name", "format", "description", "notes_xml", "archived",
    "commander_card_id", "partner_card_id", "cover_card_id",
)
REFERENCE_FIELDS = ("commander_card_id", "partner_card_id", "cover_card_id")


class VersionError(ValueError):
    """A public, data-free validation message."""


class VersionNotFound(VersionError):
    """A deck or snapshot is not available in the requested scope."""


class VersionConflict(VersionError):
    """The operation needs a new preview or cannot safely proceed."""


def ensure_schema() -> None:
    cards.init_db()
    with cards._connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deck_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
                game_code TEXT NOT NULL REFERENCES games(code),
                label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 80),
                snapshot_version INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL
                    CHECK (length(CAST(snapshot_json AS BLOB)) <= 2000000),
                snapshot_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deck_versions_scope "
            "ON deck_versions(deck_id, game_code, id DESC)"
        )


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()


def _text(value: Any, maximum: int, *, required: bool = False) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise VersionError("Invalid deck snapshot text.")
    if required and not value.strip():
        raise VersionError("A name is required.")


def _positive(value: Any) -> bool:
    return type(value) is int and 0 < value <= 9_223_372_036_854_775_807


def _validate(snapshot: dict[str, Any], game_code: str, deck_id: int) -> None:
    try:
        if (
            set(snapshot) != {"schema_version", "game_code", "deck_id", "deck", "slots", "tags"}
            or snapshot["schema_version"] != SNAPSHOT_VERSION
            or snapshot["game_code"] != game_code
            or snapshot["deck_id"] != deck_id
        ):
            raise VersionError("Invalid deck snapshot scope or format.")
        deck = snapshot["deck"]
        if set(deck) != set(DECK_FIELDS):
            raise VersionError("Invalid deck snapshot fields.")
        _text(deck["name"], 200, required=True)
        _text(deck["format"], 80)
        _text(deck["description"], 10000)
        _text(deck["notes_xml"], 1_000_000)
        cards._validated_notes_xml(deck["notes_xml"])
        if type(deck["archived"]) is not int or deck["archived"] not in (0, 1):
            raise VersionError("Invalid archive state.")
        for field in REFERENCE_FIELDS:
            if deck[field] is not None and not _positive(deck[field]):
                raise VersionError("Invalid card reference.")
        if not isinstance(snapshot["slots"], list):
            raise VersionError("Invalid deck slots.")
        identities = set()
        for slot in snapshot["slots"]:
            if set(slot) != {"card_id", "qty", "board", "category"}:
                raise VersionError("Invalid deck slot fields.")
            if not _positive(slot["card_id"]) or not _positive(slot["qty"]):
                raise VersionError("Invalid deck slot quantity or card.")
            if slot["board"] not in cards.BOARDS:
                raise VersionError("Invalid deck board.")
            _text(slot["category"], 100)
            if slot["category"] is None:
                raise VersionError("Invalid deck category.")
            identity = (slot["card_id"], slot["board"], slot["category"])
            if identity in identities:
                raise VersionError("Duplicate deck slot.")
            identities.add(identity)
        if not isinstance(snapshot["tags"], list) or len(snapshot["tags"]) > 20:
            raise VersionError("Invalid deck tags.")
        names = set()
        for tag in snapshot["tags"]:
            if set(tag) != {"name", "color"}:
                raise VersionError("Invalid deck tag fields.")
            _text(tag["name"], 40, required=True)
            if not isinstance(tag["color"], str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", tag["color"]):
                raise VersionError("Invalid deck tag color.")
            if tag["name"].lower() in names:
                raise VersionError("Duplicate deck tag.")
            names.add(tag["name"].lower())
        if len(_encode(snapshot).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise VersionError("A deck version must be at most 2 MB.")
    except VersionError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise VersionError("Invalid deck snapshot.") from exc


def _capture(conn: sqlite3.Connection, game_code: str, deck_id: int) -> tuple[dict[str, Any], str]:
    row = conn.execute(
        "SELECT * FROM decks WHERE id = ? AND game_code = ?", (deck_id, game_code)
    ).fetchone()
    if row is None:
        raise VersionNotFound("Deck not found.")
    slots = [dict(slot) for slot in conn.execute(
        "SELECT * FROM deck_cards WHERE deck_id = ? ORDER BY id", (deck_id,)
    )]
    tags = [dict(tag) for tag in conn.execute(
        "SELECT t.* FROM tags t JOIN deck_tags dt ON dt.tag_id = t.id "
        "WHERE dt.deck_id = ? ORDER BY t.name COLLATE NOCASE, t.id", (deck_id,)
    )]
    snapshot = {
        "schema_version": SNAPSHOT_VERSION, "game_code": game_code, "deck_id": deck_id,
        "deck": {field: row[field] for field in DECK_FIELDS},
        "slots": [{key: slot[key] for key in ("card_id", "qty", "board", "category")} for slot in slots],
        "tags": [{key: tag[key] for key in ("name", "color")} for tag in tags],
    }
    _validate(snapshot, game_code, deck_id)
    return snapshot, _hash({"row": dict(row), "slots": slots, "tags": tags})


def _require_cards(conn: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    card_ids = {slot["card_id"] for slot in snapshot["slots"]}
    card_ids.update(snapshot["deck"][field] for field in REFERENCE_FIELDS if snapshot["deck"][field] is not None)
    ordered_ids = sorted(card_ids)
    for offset in range(0, len(ordered_ids), 500):
        batch = ordered_ids[offset:offset + 500]
        placeholders = ",".join("?" for _ in batch)
        count = conn.execute(
            f"SELECT COUNT(*) FROM cards WHERE game_code = ? AND id IN ({placeholders})",
            (snapshot["game_code"], *batch),
        ).fetchone()[0]
        if count != len(batch):
            raise VersionConflict("Required cards are unavailable for this game.")


def _insert_version(conn: sqlite3.Connection, snapshot: dict[str, Any], label: str) -> int:
    count = conn.execute(
        "SELECT COUNT(*) FROM deck_versions WHERE deck_id = ? AND game_code = ?",
        (snapshot["deck_id"], snapshot["game_code"]),
    ).fetchone()[0]
    if count >= MAX_VERSIONS:
        raise VersionConflict("This deck has reached the 100-version limit; no changes were made.")
    result = conn.execute(
        "INSERT INTO deck_versions(deck_id, game_code, label, snapshot_version, snapshot_json, snapshot_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (snapshot["deck_id"], snapshot["game_code"], label, SNAPSHOT_VERSION, _encode(snapshot), _hash(snapshot)),
    )
    return int(result.lastrowid or 0)


def _load_version(conn: sqlite3.Connection, game_code: str, deck_id: int, version_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM deck_versions WHERE id = ? AND deck_id = ? AND game_code = ?",
        (version_id, deck_id, game_code),
    ).fetchone()
    if row is None:
        raise VersionNotFound("Deck version not found.")
    if row["snapshot_version"] != SNAPSHOT_VERSION or len(row["snapshot_json"].encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise VersionError("Unsupported or oversized deck version.")
    try:
        snapshot = json.loads(row["snapshot_json"])
    except (ValueError, RecursionError) as exc:
        raise VersionError("Invalid deck snapshot.") from exc
    _validate(snapshot, game_code, deck_id)
    if _hash(snapshot) != row["snapshot_hash"]:
        raise VersionConflict("The saved version has changed. Open a new preview.")
    return {key: row[key] for key in ("id", "deck_id", "game_code", "label", "created_at", "snapshot_hash")} | {"snapshot": snapshot}


def get_version(game_code: str, deck_id: int, version_id: int) -> dict[str, Any]:
    ensure_schema()
    with cards._connect() as conn:
        conn.execute("BEGIN")
        if not conn.execute("SELECT 1 FROM decks WHERE id = ? AND game_code = ?", (deck_id, game_code)).fetchone():
            raise VersionNotFound("Deck not found.")
        return _load_version(conn, game_code, deck_id, version_id)


def save_version(game_code: str, deck_id: int, label: str) -> dict[str, Any]:
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80 or "\x00" in label:
        raise VersionError("Version labels must contain 1 to 80 characters.")
    ensure_schema()
    with cards.transaction() as conn:
        snapshot = _capture(conn, game_code, deck_id)[0]
        _require_cards(conn, snapshot)
        version_id = _insert_version(conn, snapshot, label.strip())
    return get_version(game_code, deck_id, version_id)


def _write_deck(conn: sqlite3.Connection, game_code: str, deck_id: int, snapshot: dict[str, Any]) -> None:
    _require_tags(conn, snapshot)
    fields = ", ".join(f"{field} = ?" for field in DECK_FIELDS)
    conn.execute(
        f"UPDATE decks SET {fields}, updated_at = datetime('now') WHERE id = ? AND game_code = ?",
        (*[snapshot["deck"][field] for field in DECK_FIELDS], deck_id, game_code),
    )
    conn.execute("DELETE FROM deck_cards WHERE deck_id = ?", (deck_id,))
    conn.executemany(
        "INSERT INTO deck_cards(deck_id, card_id, qty, board, category) VALUES (?, ?, ?, ?, ?)",
        [(deck_id, slot["card_id"], slot["qty"], slot["board"], slot["category"]) for slot in snapshot["slots"]],
    )
    conn.execute("DELETE FROM deck_tags WHERE deck_id = ?", (deck_id,))
    for tag in snapshot["tags"]:
        conn.execute("INSERT INTO tags(name, color) VALUES (?, ?) ON CONFLICT(name) DO NOTHING", (tag["name"], tag["color"]))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (tag["name"],)).fetchone()[0]
        conn.execute("INSERT INTO deck_tags(deck_id, tag_id) VALUES (?, ?)", (deck_id, tag_id))


def duplicate_deck(game_code: str, deck_id: int, name: str | None = None) -> dict[str, Any]:
    ensure_schema()
    with cards.transaction() as conn:
        snapshot = _capture(conn, game_code, deck_id)[0]
        _require_cards(conn, snapshot)
        clone_name = name.strip() if name else ""
        if not clone_name:
            clone_name = snapshot["deck"]["name"][:193] + " (copy)"
        _text(clone_name, 200, required=True)
        clone_id = cards.create_deck(game_code, clone_name, conn=conn)
        snapshot["deck"]["name"] = clone_name
        _write_deck(conn, game_code, clone_id, snapshot)
    snapshot["deck_id"] = clone_id
    _verify_deck(game_code, clone_id, snapshot)
    clone = cards.get_deck(clone_id, game_code)
    if clone is None:
        raise VersionConflict("The duplicate could not be verified.")
    return clone


def _require_tags(conn: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    for tag in snapshot["tags"]:
        existing = conn.execute("SELECT name, color FROM tags WHERE name = ? COLLATE NOCASE", (tag["name"],)).fetchone()
        if existing and dict(existing) != tag:
            raise VersionConflict("Shared tags have changed. Restore cannot modify tags used by other decks.")


def _verify_deck(game_code: str, deck_id: int, expected: dict[str, Any]) -> None:
    with cards._connect() as conn:
        conn.execute("BEGIN")
        actual = _capture(conn, game_code, deck_id)[0]
        if actual != expected:
            raise VersionConflict("The committed deck could not be verified. Reload the deck before continuing.")


def _list_versions(conn: sqlite3.Connection, game_code: str, deck_id: int) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        "SELECT id, deck_id, game_code, label, created_at FROM deck_versions "
        "WHERE deck_id = ? AND game_code = ? ORDER BY id DESC LIMIT ?",
        (deck_id, game_code, MAX_VERSIONS),
    )]


def list_versions(game_code: str, deck_id: int) -> list[dict[str, Any]]:
    ensure_schema()
    with cards._connect() as conn:
        conn.execute("BEGIN")
        if not conn.execute("SELECT 1 FROM decks WHERE id = ? AND game_code = ?", (deck_id, game_code)).fetchone():
            raise VersionNotFound("Deck not found.")
        return _list_versions(conn, game_code, deck_id)


def _totals(snapshot: dict[str, Any]) -> dict[str, int]:
    totals = {board: 0 for board in cards.BOARDS}
    for slot in snapshot["slots"]:
        totals[slot["board"]] += slot["qty"]
    totals["deck"] = totals["main"] + totals["commander"]
    totals["all"] = sum(totals[board] for board in cards.BOARDS)
    return totals


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    def keyed(snapshot: dict[str, Any]) -> dict[tuple[int, str, str], dict[str, Any]]:
        return {(slot["card_id"], slot["board"], slot["category"]): slot for slot in snapshot["slots"]}

    before_slots, after_slots = keyed(before), keyed(after)
    changes = []
    removed: dict[int, list[dict[str, Any]]] = {}
    added: dict[int, list[dict[str, Any]]] = {}
    for identity in sorted(before_slots.keys() | after_slots.keys()):
        old, new = before_slots.get(identity), after_slots.get(identity)
        if old is None and new is not None:
            added.setdefault(identity[0], []).append(new)
        elif new is None:
            if old is not None:
                removed.setdefault(identity[0], []).append(old)
        elif old is not None and old["qty"] != new["qty"]:
            changes.append({"kind": "quantity", "card_id": identity[0], "before": old, "after": new})
    for card_id in sorted(removed.keys() | added.keys()):
        old_slots = removed.get(card_id, [])
        new_slots = added.get(card_id, [])
        if len(old_slots) == len(new_slots) == 1:
            changes.append({"kind": "moved", "card_id": card_id, "before": old_slots[0], "after": new_slots[0]})
        else:
            changes.extend({"kind": "removed", "card_id": card_id, "before": slot, "after": None} for slot in old_slots)
            changes.extend({"kind": "added", "card_id": card_id, "before": None, "after": slot} for slot in new_slots)
    metadata = [
        {"field": field, "before": before["deck"][field], "after": after["deck"][field]}
        for field in DECK_FIELDS if before["deck"][field] != after["deck"][field]
    ]
    for change in metadata:
        if change["field"] == "notes_xml":
            change["before"] = "Diagram saved" if change["before"] else "No diagram"
            change["after"] = "Diagram changed" if change["after"] else "No diagram"
    return {
        "slots": changes, "metadata": metadata,
        "tags_added": [tag for tag in after["tags"] if tag not in before["tags"]],
        "tags_removed": [tag for tag in before["tags"] if tag not in after["tags"]],
        "before_totals": _totals(before), "after_totals": _totals(after),
        "changed": before["deck"] != after["deck"] or before["slots"] != after["slots"] or before["tags"] != after["tags"],
    }


def _comparison(conn: sqlite3.Connection, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    _require_cards(conn, before)
    _require_cards(conn, after)
    result = _diff(before, after)
    card_ids = {change["card_id"] for change in result["slots"]}
    for snapshot in (before, after):
        card_ids.update(snapshot["deck"][field] for field in REFERENCE_FIELDS if snapshot["deck"][field] is not None)
    names = {}
    ordered_ids = sorted(card_ids)
    for offset in range(0, len(ordered_ids), 500):
        batch = ordered_ids[offset:offset + 500]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"SELECT id, name, set_code, collector_number FROM cards WHERE game_code = ? AND id IN ({placeholders})",
            (before["game_code"], *batch),
        ):
            names[row["id"]] = dict(row)
    result["cards"] = names
    return result


def version_page(game_code: str, deck_id: int, before_id: int | None = None, after_id: int | None = None) -> dict[str, Any]:
    ensure_schema()
    with cards._connect() as conn:
        conn.execute("BEGIN")
        current = _capture(conn, game_code, deck_id)[0]
        _require_cards(conn, current)
        saved_versions = _list_versions(conn, game_code, deck_id)
        if before_id is None and saved_versions:
            before_id = saved_versions[0]["id"]
        before = _load_version(conn, game_code, deck_id, before_id) if before_id is not None else None
        after = _load_version(conn, game_code, deck_id, after_id) if after_id is not None else None
        return {
            "versions": saved_versions, "deck": current["deck"] | {"id": deck_id},
            "before_id": before_id, "after_id": after_id,
            "before_label": before["label"] if before else "",
            "after_label": after["label"] if after else "Current deck",
            "comparison": _comparison(conn, before["snapshot"], after["snapshot"] if after else current) if before else None,
        }


def preview_restore(game_code: str, deck_id: int, version_id: int) -> dict[str, Any]:
    ensure_schema()
    with cards._connect() as conn:
        conn.execute("BEGIN")
        current, current_hash = _capture(conn, game_code, deck_id)
        version = _load_version(conn, game_code, deck_id, version_id)
        _require_tags(conn, version["snapshot"])
        comparison = _comparison(conn, current, version["snapshot"])
        count = conn.execute("SELECT COUNT(*) FROM deck_versions WHERE deck_id = ? AND game_code = ?", (deck_id, game_code)).fetchone()[0]
        if count >= MAX_VERSIONS:
            raise VersionConflict("Restoration requires room for a Before restore version; this deck has reached 100 versions.")
        return {
            "version": {key: value for key, value in version.items() if key != "snapshot"},
            "deck": current["deck"] | {"id": deck_id}, "comparison": comparison,
            "expected_current_hash": current_hash,
            "expected_version_hash": version["snapshot_hash"],
            "before_label": "Current deck", "after_label": version["label"],
        }


def restore_version(
    game_code: str, deck_id: int, version_id: int, *,
    expected_current_hash: str, expected_version_hash: str, confirm: bool = False,
) -> dict[str, Any]:
    if confirm is not True:
        raise VersionError("Confirm restoration after reviewing the preview.")
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in (expected_current_hash, expected_version_hash)):
        raise VersionError("A restoration preview is required.")
    ensure_schema()
    with cards.transaction() as conn:
        current, current_hash = _capture(conn, game_code, deck_id)
        version = _load_version(conn, game_code, deck_id, version_id)
        if current_hash != expected_current_hash or version["snapshot_hash"] != expected_version_hash:
            raise VersionConflict("The deck or version changed after preview. Open a new preview.")
        _require_cards(conn, current)
        _require_cards(conn, version["snapshot"])
        _require_tags(conn, version["snapshot"])
        backup_id = _insert_version(conn, current, "Before restore")
        _write_deck(conn, game_code, deck_id, version["snapshot"])
    _verify_deck(game_code, deck_id, version["snapshot"])
    backup = get_version(game_code, deck_id, backup_id)
    return {"deck_id": deck_id, "restored_version_id": version_id, "backup_version_id": backup["id"]}