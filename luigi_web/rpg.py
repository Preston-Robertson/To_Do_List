"""App-owned storage and sheet calculations for tabletop RPG characters."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from .paths import RPG_DB_PATH

SCHEMA_VERSION = 1
ABILITY_KEYS = (
    "strength",
    "dexterity",
    "constitution",
    "intelligence",
    "wisdom",
    "charisma",
)
SYSTEMS = {
    "dnd5e_2014": {
        "name": "D&D 5e (2014)",
        "short_name": "5e 2014",
        "ability_mode": "score",
        "rules_url": "https://2014.5e.tools/index.html",
        "official_url": "https://www.dndbeyond.com/srd",
    },
    "pf2e": {
        "name": "Pathfinder 2e",
        "short_name": "PF2e",
        "ability_mode": "modifier",
        "rules_url": "https://2e.aonprd.com/",
        "official_url": "https://paizo.com/pathfinder",
    },
}
ENTRY_KINDS = (
    "action",
    "feature",
    "spell",
    "inventory",
    "resource",
    "proficiency",
    "condition",
)
RESET_OPTIONS = ("", "turn", "encounter", "short_rest", "long_rest", "daily")
DND_SKILLS = (
    ("acrobatics", "Acrobatics", "dexterity"),
    ("animal_handling", "Animal Handling", "wisdom"),
    ("arcana", "Arcana", "intelligence"),
    ("athletics", "Athletics", "strength"),
    ("deception", "Deception", "charisma"),
    ("history", "History", "intelligence"),
    ("insight", "Insight", "wisdom"),
    ("intimidation", "Intimidation", "charisma"),
    ("investigation", "Investigation", "intelligence"),
    ("medicine", "Medicine", "wisdom"),
    ("nature", "Nature", "intelligence"),
    ("perception", "Perception", "wisdom"),
    ("performance", "Performance", "charisma"),
    ("persuasion", "Persuasion", "charisma"),
    ("religion", "Religion", "intelligence"),
    ("sleight_of_hand", "Sleight of Hand", "dexterity"),
    ("stealth", "Stealth", "dexterity"),
    ("survival", "Survival", "wisdom"),
)
PF2_SKILLS = (
    ("acrobatics", "Acrobatics", "dexterity"),
    ("arcana", "Arcana", "intelligence"),
    ("athletics", "Athletics", "strength"),
    ("crafting", "Crafting", "intelligence"),
    ("deception", "Deception", "charisma"),
    ("diplomacy", "Diplomacy", "charisma"),
    ("intimidation", "Intimidation", "charisma"),
    ("medicine", "Medicine", "wisdom"),
    ("nature", "Nature", "wisdom"),
    ("occultism", "Occultism", "intelligence"),
    ("performance", "Performance", "charisma"),
    ("religion", "Religion", "wisdom"),
    ("society", "Society", "intelligence"),
    ("stealth", "Stealth", "dexterity"),
    ("survival", "Survival", "wisdom"),
    ("thievery", "Thievery", "dexterity"),
)
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_STATE_COPY_COLUMNS = (
    "ancestry", "heritage", "background", "class_name", "subclass",
    "alignment", "xp_current", "xp_next", "hp_current", "hp_max", "temp_hp",
    "armor_class", "speed", "initiative", "perception", "class_dc", "spell_dc",
    "spell_attack", "inspiration", "hero_points", "focus_current", "focus_max",
    "death_successes", "death_failures", "abilities_json", "saves_json",
    "skills_json", "languages", "proficiencies", "conditions", "notes",
)
_ENTRY_COPY_COLUMNS = (
    "kind", "name", "summary", "description", "rank", "action_cost", "prepared",
    "quantity", "equipped", "current_uses", "max_uses", "reset_on", "source_url",
    "sort_order",
)


def db_path() -> Path:
    configured = os.environ.get("LUIGI_WEB_RPG_DB", "").strip()
    return Path(configured).expanduser().resolve() if configured else RPG_DB_PATH


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=10000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    """Create the isolated character database."""
    with _connect() as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"RPG database schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
                system_code TEXT NOT NULL CHECK (system_code IN ('dnd5e_2014', 'pf2e')),
                campaign TEXT NOT NULL DEFAULT '',
                concept TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_characters_system
                ON characters(archived, system_code, updated_at DESC);

            CREATE TABLE IF NOT EXISTS character_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
                level INTEGER NOT NULL CHECK (level BETWEEN 1 AND 20),
                label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 120),
                is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
                ancestry TEXT NOT NULL DEFAULT '',
                heritage TEXT NOT NULL DEFAULT '',
                background TEXT NOT NULL DEFAULT '',
                class_name TEXT NOT NULL DEFAULT '',
                subclass TEXT NOT NULL DEFAULT '',
                alignment TEXT NOT NULL DEFAULT '',
                xp_current INTEGER NOT NULL DEFAULT 0 CHECK (xp_current >= 0),
                xp_next INTEGER NOT NULL DEFAULT 0 CHECK (xp_next >= 0),
                hp_current INTEGER NOT NULL DEFAULT 1 CHECK (hp_current >= 0),
                hp_max INTEGER NOT NULL DEFAULT 1 CHECK (hp_max >= 1),
                temp_hp INTEGER NOT NULL DEFAULT 0 CHECK (temp_hp >= 0),
                armor_class INTEGER NOT NULL DEFAULT 10 CHECK (armor_class >= 0),
                speed INTEGER NOT NULL DEFAULT 25 CHECK (speed >= 0),
                initiative INTEGER NOT NULL DEFAULT 0,
                perception INTEGER NOT NULL DEFAULT 0,
                class_dc INTEGER NOT NULL DEFAULT 0 CHECK (class_dc >= 0),
                spell_dc INTEGER NOT NULL DEFAULT 0 CHECK (spell_dc >= 0),
                spell_attack INTEGER NOT NULL DEFAULT 0,
                inspiration INTEGER NOT NULL DEFAULT 0 CHECK (inspiration IN (0, 1)),
                hero_points INTEGER NOT NULL DEFAULT 0 CHECK (hero_points BETWEEN 0 AND 3),
                focus_current INTEGER NOT NULL DEFAULT 0 CHECK (focus_current >= 0),
                focus_max INTEGER NOT NULL DEFAULT 0 CHECK (focus_max BETWEEN 0 AND 3),
                death_successes INTEGER NOT NULL DEFAULT 0 CHECK (death_successes BETWEEN 0 AND 3),
                death_failures INTEGER NOT NULL DEFAULT 0 CHECK (death_failures BETWEEN 0 AND 3),
                abilities_json TEXT NOT NULL DEFAULT '{}',
                saves_json TEXT NOT NULL DEFAULT '{}',
                skills_json TEXT NOT NULL DEFAULT '{}',
                languages TEXT NOT NULL DEFAULT '',
                proficiencies TEXT NOT NULL DEFAULT '',
                conditions TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_character_states_character
                ON character_states(character_id, level, id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_character_states_active
                ON character_states(character_id) WHERE is_active = 1;

            CREATE TABLE IF NOT EXISTS sheet_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                state_id INTEGER NOT NULL REFERENCES character_states(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK (kind IN (
                    'action', 'feature', 'spell', 'inventory', 'resource',
                    'proficiency', 'condition'
                )),
                name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 160),
                summary TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                rank INTEGER NOT NULL DEFAULT 0 CHECK (rank BETWEEN 0 AND 20),
                action_cost TEXT NOT NULL DEFAULT '',
                prepared INTEGER NOT NULL DEFAULT 0 CHECK (prepared IN (0, 1)),
                quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 0),
                equipped INTEGER NOT NULL DEFAULT 0 CHECK (equipped IN (0, 1)),
                current_uses INTEGER CHECK (current_uses IS NULL OR current_uses >= 0),
                max_uses INTEGER CHECK (max_uses IS NULL OR max_uses >= 0),
                reset_on TEXT NOT NULL DEFAULT '' CHECK (
                    reset_on IN ('', 'turn', 'encounter', 'short_rest', 'long_rest', 'daily')
                ),
                source_url TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_sheet_entries_state
                ON sheet_entries(state_id, kind, sort_order, name COLLATE NOCASE);
            """
        )
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _clean_text(
    value: Any,
    field: str,
    *,
    max_length: int,
    required: bool = False,
) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > max_length:
        raise ValueError(f"{field} must be {max_length} characters or fewer")
    return text


def _bounded_int(
    value: Any,
    field: str,
    minimum: int,
    maximum: int,
    *,
    allow_none: bool = False,
) -> int | None:
    if allow_none and value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a whole number") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return number


def _bool_int(value: Any) -> int:
    return int(value is True or str(value).strip().lower() in {"1", "true", "yes", "on"})


def _source_url(value: Any) -> str:
    url = _clean_text(value, "Source URL", max_length=1000)
    if not url:
        return ""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Source URL must be an http or https link")
    if parsed.username or parsed.password:
        raise ValueError("Source URL cannot contain credentials")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Source URL has an invalid port") from exc
    return url


def _json_map(value: Any, field: str) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("archived", "is_active", "inspiration", "prepared", "equipped"):
        if key in result:
            result[key] = bool(result[key])
    for key, public_key in (
        ("abilities_json", "abilities"),
        ("saves_json", "saves"),
        ("skills_json", "skills"),
    ):
        if key in result:
            try:
                result[public_key] = json.loads(result.pop(key) or "{}")
            except (TypeError, json.JSONDecodeError):
                result[public_key] = {}
    return result


def _require_system(system_code: Any) -> str:
    code = str(system_code or "").strip().lower()
    if code not in SYSTEMS:
        raise ValueError("Choose D&D 5e (2014) or Pathfinder 2e")
    return code


def create_character(
    name: Any,
    system_code: Any,
    *,
    campaign: Any = "",
    concept: Any = "",
    level: Any = 1,
) -> dict[str, Any]:
    clean_name = _clean_text(name, "Character name", max_length=120, required=True)
    clean_system = _require_system(system_code)
    clean_campaign = _clean_text(campaign, "Campaign", max_length=160)
    clean_concept = _clean_text(concept, "Concept", max_length=500)
    clean_level = _bounded_int(level, "Level", 1, 20)
    default_value = 10 if clean_system == "dnd5e_2014" else 0
    abilities = json.dumps({key: default_value for key in ABILITY_KEYS})
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO characters (name, system_code, campaign, concept)
            VALUES (?, ?, ?, ?)
            """,
            (clean_name, clean_system, clean_campaign, clean_concept),
        )
        character_id = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO character_states (
                character_id, level, label, is_active, speed, abilities_json
            ) VALUES (?, ?, ?, 1, ?, ?)
            """,
            (
                character_id,
                clean_level,
                f"Level {clean_level}",
                30 if clean_system == "dnd5e_2014" else 25,
                abilities,
            ),
        )
    character = get_character(character_id)
    if character is None:  # pragma: no cover - the insert and read share one database
        raise RuntimeError("Character was not created")
    return character


def get_character(character_id: int) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT c.*,
                   s.id AS active_state_id,
                   s.level AS active_level,
                   s.label AS active_state_label,
                   s.class_name AS active_class_name,
                   s.ancestry AS active_ancestry
            FROM characters c
            LEFT JOIN character_states s
              ON s.character_id = c.id AND s.is_active = 1
            WHERE c.id = ?
            """,
            (int(character_id),),
        ).fetchone()
    return _row_dict(row)


def list_characters(
    *,
    system_code: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    clauses = ["1 = 1"]
    params: list[Any] = []
    if not include_archived:
        clauses.append("c.archived = 0")
    if system_code:
        clauses.append("c.system_code = ?")
        params.append(_require_system(system_code))
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT c.*,
                   s.id AS active_state_id,
                   s.level AS active_level,
                   s.label AS active_state_label,
                   s.class_name AS active_class_name,
                   s.ancestry AS active_ancestry,
                   s.hp_current,
                   s.hp_max,
                   (SELECT COUNT(*) FROM character_states cs
                    WHERE cs.character_id = c.id) AS state_count
            FROM characters c
            LEFT JOIN character_states s
              ON s.character_id = c.id AND s.is_active = 1
            WHERE {' AND '.join(clauses)}
            ORDER BY c.updated_at DESC, c.name COLLATE NOCASE, c.id DESC
            """,
            params,
        ).fetchall()
    return [_row_dict(row) or {} for row in rows]


def update_character(
    character_id: int,
    *,
    name: Any,
    campaign: Any = "",
    concept: Any = "",
) -> dict[str, Any]:
    values = (
        _clean_text(name, "Character name", max_length=120, required=True),
        _clean_text(campaign, "Campaign", max_length=160),
        _clean_text(concept, "Concept", max_length=500),
        int(character_id),
    )
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE characters
            SET name = ?, campaign = ?, concept = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            values,
        )
        if cursor.rowcount != 1:
            raise ValueError("Character not found")
    character = get_character(character_id)
    if character is None:  # pragma: no cover
        raise RuntimeError("Character update could not be read")
    return character


def set_character_archived(character_id: int, archived: Any) -> None:
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE characters
            SET archived = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (_bool_int(archived), int(character_id)),
        )
        if cursor.rowcount != 1:
            raise ValueError("Character not found")


def delete_character(character_id: int) -> None:
    with _connect() as connection:
        cursor = connection.execute("DELETE FROM characters WHERE id = ?", (int(character_id),))
        if cursor.rowcount != 1:
            raise ValueError("Character not found")


def list_states(character_id: int) -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM character_states
            WHERE character_id = ?
            ORDER BY level, id
            """,
            (int(character_id),),
        ).fetchall()
    return [_row_dict(row) or {} for row in rows]


def get_state(state_id: int, character_id: int | None = None) -> dict[str, Any] | None:
    clauses = ["s.id = ?"]
    params: list[Any] = [int(state_id)]
    if character_id is not None:
        clauses.append("s.character_id = ?")
        params.append(int(character_id))
    with _connect() as connection:
        row = connection.execute(
            f"""
            SELECT s.*, c.system_code
            FROM character_states s
            JOIN characters c ON c.id = s.character_id
            WHERE {' AND '.join(clauses)}
            """,
            params,
        ).fetchone()
    return _row_dict(row)


def create_state(
    character_id: int,
    *,
    level: Any,
    label: Any = "",
    clone_from_id: int | None = None,
    make_active: bool = True,
) -> dict[str, Any]:
    clean_level = _bounded_int(level, "Level", 1, 20)
    clean_label = _clean_text(label, "State label", max_length=120) or f"Level {clean_level}"
    character = get_character(character_id)
    if character is None:
        raise ValueError("Character not found")
    with _connect() as connection:
        if clone_from_id is not None:
            copy_columns = ", ".join(_STATE_COPY_COLUMNS)
            cursor = connection.execute(
                f"""
                INSERT INTO character_states (
                    character_id, level, label, is_active, {copy_columns}
                )
                SELECT ?, ?, ?, 0, {copy_columns}
                FROM character_states
                WHERE id = ? AND character_id = ?
                """,
                (
                    int(character_id),
                    clean_level,
                    clean_label,
                    int(clone_from_id),
                    int(character_id),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Level state to copy was not found")
        else:
            default_value = 10 if character["system_code"] == "dnd5e_2014" else 0
            cursor = connection.execute(
                """
                INSERT INTO character_states (
                    character_id, level, label, is_active, speed, abilities_json
                ) VALUES (?, ?, ?, 0, ?, ?)
                """,
                (
                    int(character_id),
                    clean_level,
                    clean_label,
                    30 if character["system_code"] == "dnd5e_2014" else 25,
                    json.dumps({key: default_value for key in ABILITY_KEYS}),
                ),
            )
        new_state_id = int(cursor.lastrowid)
        if clone_from_id is not None:
            entry_columns = ", ".join(_ENTRY_COPY_COLUMNS)
            connection.execute(
                f"""
                INSERT INTO sheet_entries (state_id, {entry_columns})
                SELECT ?, {entry_columns}
                FROM sheet_entries
                WHERE state_id = ?
                """,
                (new_state_id, int(clone_from_id)),
            )
        if make_active:
            connection.execute(
                "UPDATE character_states SET is_active = 0 WHERE character_id = ?",
                (int(character_id),),
            )
            connection.execute(
                "UPDATE character_states SET is_active = 1 WHERE id = ?",
                (new_state_id,),
            )
        connection.execute(
            "UPDATE characters SET updated_at = datetime('now') WHERE id = ?",
            (int(character_id),),
        )
    state = get_state(new_state_id, character_id)
    if state is None:  # pragma: no cover
        raise RuntimeError("Level state was not created")
    return state


def make_state_active(character_id: int, state_id: int) -> None:
    with _connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM character_states WHERE id = ? AND character_id = ?",
            (int(state_id), int(character_id)),
        ).fetchone()
        if exists is None:
            raise ValueError("Level state not found")
        connection.execute(
            "UPDATE character_states SET is_active = 0 WHERE character_id = ?",
            (int(character_id),),
        )
        connection.execute(
            "UPDATE character_states SET is_active = 1 WHERE id = ?",
            (int(state_id),),
        )
        connection.execute(
            "UPDATE characters SET updated_at = datetime('now') WHERE id = ?",
            (int(character_id),),
        )


def delete_state(character_id: int, state_id: int) -> None:
    with _connect() as connection:
        states = connection.execute(
            "SELECT id, is_active FROM character_states WHERE character_id = ? ORDER BY level DESC, id DESC",
            (int(character_id),),
        ).fetchall()
        target = next((row for row in states if int(row["id"]) == int(state_id)), None)
        if target is None:
            raise ValueError("Level state not found")
        if len(states) == 1:
            raise ValueError("A character must keep at least one level state")
        connection.execute("DELETE FROM character_states WHERE id = ?", (int(state_id),))
        if bool(target["is_active"]):
            replacement = next(row for row in states if int(row["id"]) != int(state_id))
            connection.execute(
                "UPDATE character_states SET is_active = 1 WHERE id = ?",
                (int(replacement["id"]),),
            )
        connection.execute(
            "UPDATE characters SET updated_at = datetime('now') WHERE id = ?",
            (int(character_id),),
        )


def _clean_rank_map(value: Any, field: str) -> dict[str, dict[str, int]]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    result: dict[str, dict[str, int]] = {}
    for raw_key, raw_details in value.items():
        key = str(raw_key).strip().lower()
        if not _SLUG_RE.fullmatch(key) or not isinstance(raw_details, dict):
            raise ValueError(f"{field} contains an invalid entry")
        rank = _bounded_int(raw_details.get("rank", 0), f"{field} rank", 0, 4)
        misc = _bounded_int(raw_details.get("misc", 0), f"{field} modifier", -50, 50)
        result[key] = {"rank": int(rank), "misc": int(misc)}
    return result


def update_state(state_id: int, **values: Any) -> dict[str, Any]:
    state = get_state(state_id)
    if state is None:
        raise ValueError("Level state not found")
    cleaners: dict[str, Any] = {
        "label": lambda value: _clean_text(value, "State label", max_length=120, required=True),
        "level": lambda value: _bounded_int(value, "Level", 1, 20),
        "ancestry": lambda value: _clean_text(value, "Ancestry or species", max_length=120),
        "heritage": lambda value: _clean_text(value, "Heritage", max_length=120),
        "background": lambda value: _clean_text(value, "Background", max_length=120),
        "class_name": lambda value: _clean_text(value, "Class", max_length=160),
        "subclass": lambda value: _clean_text(value, "Subclass or archetype", max_length=160),
        "alignment": lambda value: _clean_text(value, "Alignment", max_length=80),
        "xp_current": lambda value: _bounded_int(value, "Current XP", 0, 2_000_000_000),
        "xp_next": lambda value: _bounded_int(value, "Next-level XP", 0, 2_000_000_000),
        "hp_current": lambda value: _bounded_int(value, "Current HP", 0, 1_000_000),
        "hp_max": lambda value: _bounded_int(value, "Maximum HP", 1, 1_000_000),
        "temp_hp": lambda value: _bounded_int(value, "Temporary HP", 0, 1_000_000),
        "armor_class": lambda value: _bounded_int(value, "Armor Class", 0, 1000),
        "speed": lambda value: _bounded_int(value, "Speed", 0, 10_000),
        "initiative": lambda value: _bounded_int(value, "Initiative", -1000, 1000),
        "perception": lambda value: _bounded_int(value, "Perception", -1000, 1000),
        "class_dc": lambda value: _bounded_int(value, "Class DC", 0, 1000),
        "spell_dc": lambda value: _bounded_int(value, "Spell DC", 0, 1000),
        "spell_attack": lambda value: _bounded_int(value, "Spell attack", -1000, 1000),
        "inspiration": _bool_int,
        "hero_points": lambda value: _bounded_int(value, "Hero Points", 0, 3),
        "focus_current": lambda value: _bounded_int(value, "Current Focus Points", 0, 3),
        "focus_max": lambda value: _bounded_int(value, "Maximum Focus Points", 0, 3),
        "death_successes": lambda value: _bounded_int(value, "Death save successes", 0, 3),
        "death_failures": lambda value: _bounded_int(value, "Death save failures", 0, 3),
        "languages": lambda value: _clean_text(value, "Languages", max_length=2000),
        "proficiencies": lambda value: _clean_text(value, "Proficiencies", max_length=5000),
        "conditions": lambda value: _clean_text(value, "Conditions", max_length=2000),
        "notes": lambda value: _clean_text(value, "Notes", max_length=20_000),
    }
    assignments: list[str] = []
    params: list[Any] = []
    for key, value in values.items():
        if key == "abilities":
            if not isinstance(value, dict) or set(value) - set(ABILITY_KEYS):
                raise ValueError("Abilities contain an invalid entry")
            abilities = {
                ability: int(_bounded_int(value.get(ability, state["abilities"].get(ability, 0)), ability, -10, 30))
                for ability in ABILITY_KEYS
            }
            assignments.append("abilities_json = ?")
            params.append(_json_map(abilities, "Abilities"))
            continue
        if key in {"saves", "skills"}:
            assignments.append(f"{key}_json = ?")
            params.append(_json_map(_clean_rank_map(value, key.title()), key.title()))
            continue
        cleaner = cleaners.get(key)
        if cleaner is None:
            raise ValueError(f"Cannot update unknown state field: {key}")
        assignments.append(f"{key} = ?")
        params.append(cleaner(value))
    if not assignments:
        return state
    params.append(int(state_id))
    with _connect() as connection:
        connection.execute(
            f"UPDATE character_states SET {', '.join(assignments)}, updated_at = datetime('now') WHERE id = ?",
            params,
        )
        connection.execute(
            "UPDATE characters SET updated_at = datetime('now') WHERE id = ?",
            (int(state["character_id"]),),
        )
    updated = get_state(state_id)
    if updated is None:  # pragma: no cover
        raise RuntimeError("Level state update could not be read")
    return updated


def list_entries(state_id: int, kind: str | None = None) -> list[dict[str, Any]]:
    clauses = ["state_id = ?"]
    params: list[Any] = [int(state_id)]
    if kind is not None:
        if kind not in ENTRY_KINDS:
            raise ValueError("Unknown sheet section")
        clauses.append("kind = ?")
        params.append(kind)
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM sheet_entries
            WHERE {' AND '.join(clauses)}
            ORDER BY kind, sort_order, name COLLATE NOCASE, id
            """,
            params,
        ).fetchall()
    return [_row_dict(row) or {} for row in rows]


def get_entry(entry_id: int, state_id: int | None = None) -> dict[str, Any] | None:
    clauses = ["id = ?"]
    params: list[Any] = [int(entry_id)]
    if state_id is not None:
        clauses.append("state_id = ?")
        params.append(int(state_id))
    with _connect() as connection:
        row = connection.execute(
            f"SELECT * FROM sheet_entries WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
    return _row_dict(row)


def _entry_values(values: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    base = current or {}
    kind = str(values.get("kind", base.get("kind", "feature"))).strip().lower()
    if kind not in ENTRY_KINDS:
        raise ValueError("Choose a valid sheet section")
    reset_on = str(values.get("reset_on", base.get("reset_on", ""))).strip().lower()
    if reset_on not in RESET_OPTIONS:
        raise ValueError("Choose a valid reset schedule")
    current_uses = _bounded_int(
        values.get("current_uses", base.get("current_uses")),
        "Current uses",
        0,
        1_000_000,
        allow_none=True,
    )
    max_uses = _bounded_int(
        values.get("max_uses", base.get("max_uses")),
        "Maximum uses",
        0,
        1_000_000,
        allow_none=True,
    )
    if current_uses is not None and max_uses is not None and current_uses > max_uses:
        raise ValueError("Current uses cannot exceed maximum uses")
    return {
        "kind": kind,
        "name": _clean_text(values.get("name", base.get("name")), "Name", max_length=160, required=True),
        "summary": _clean_text(values.get("summary", base.get("summary")), "Summary", max_length=500),
        "description": _clean_text(values.get("description", base.get("description")), "Description", max_length=20_000),
        "rank": _bounded_int(values.get("rank", base.get("rank", 0)), "Rank or level", 0, 20),
        "action_cost": _clean_text(values.get("action_cost", base.get("action_cost")), "Action cost", max_length=80),
        "prepared": _bool_int(values.get("prepared", base.get("prepared", False))),
        "quantity": _bounded_int(values.get("quantity", base.get("quantity", 1)), "Quantity", 0, 1_000_000),
        "equipped": _bool_int(values.get("equipped", base.get("equipped", False))),
        "current_uses": current_uses,
        "max_uses": max_uses,
        "reset_on": reset_on,
        "source_url": _source_url(values.get("source_url", base.get("source_url"))),
        "sort_order": _bounded_int(values.get("sort_order", base.get("sort_order", 0)), "Sort order", -1_000_000, 1_000_000),
    }


def add_entry(state_id: int, **values: Any) -> dict[str, Any]:
    if get_state(state_id) is None:
        raise ValueError("Level state not found")
    cleaned = _entry_values(values)
    columns = tuple(cleaned)
    with _connect() as connection:
        cursor = connection.execute(
            f"""
            INSERT INTO sheet_entries (state_id, {', '.join(columns)})
            VALUES (?, {', '.join('?' for _ in columns)})
            """,
            (int(state_id), *(cleaned[column] for column in columns)),
        )
        entry_id = int(cursor.lastrowid)
        connection.execute(
            """
            UPDATE characters SET updated_at = datetime('now')
            WHERE id = (SELECT character_id FROM character_states WHERE id = ?)
            """,
            (int(state_id),),
        )
    entry = get_entry(entry_id, state_id)
    if entry is None:  # pragma: no cover
        raise RuntimeError("Sheet entry was not created")
    return entry


def update_entry(entry_id: int, **values: Any) -> dict[str, Any]:
    current = get_entry(entry_id)
    if current is None:
        raise ValueError("Sheet entry not found")
    cleaned = _entry_values(values, current)
    assignments = ", ".join(f"{column} = ?" for column in cleaned)
    with _connect() as connection:
        connection.execute(
            f"UPDATE sheet_entries SET {assignments}, updated_at = datetime('now') WHERE id = ?",
            (*(cleaned[column] for column in cleaned), int(entry_id)),
        )
        connection.execute(
            """
            UPDATE characters SET updated_at = datetime('now')
            WHERE id = (SELECT character_id FROM character_states WHERE id = ?)
            """,
            (int(current["state_id"]),),
        )
    entry = get_entry(entry_id)
    if entry is None:  # pragma: no cover
        raise RuntimeError("Sheet entry update could not be read")
    return entry


def set_entry_uses(entry_id: int, current_uses: Any) -> dict[str, Any]:
    entry = get_entry(entry_id)
    if entry is None:
        raise ValueError("Sheet entry not found")
    maximum = entry["max_uses"] if entry["max_uses"] is not None else 1_000_000
    uses = _bounded_int(current_uses, "Current uses", 0, int(maximum))
    with _connect() as connection:
        connection.execute(
            "UPDATE sheet_entries SET current_uses = ?, updated_at = datetime('now') WHERE id = ?",
            (uses, int(entry_id)),
        )
    updated = get_entry(entry_id)
    if updated is None:  # pragma: no cover
        raise RuntimeError("Sheet entry update could not be read")
    return updated


def delete_entry(entry_id: int, state_id: int | None = None) -> None:
    entry = get_entry(entry_id, state_id)
    if entry is None:
        raise ValueError("Sheet entry not found")
    with _connect() as connection:
        connection.execute("DELETE FROM sheet_entries WHERE id = ?", (int(entry_id),))


def ability_modifier(score: int) -> int:
    return (int(score) - 10) // 2


def dnd_proficiency_bonus(level: int) -> int:
    return 2 + (int(level) - 1) // 4


def _rank_details(raw: Any) -> tuple[int, int]:
    if not isinstance(raw, dict):
        return 0, 0
    try:
        return int(raw.get("rank", 0)), int(raw.get("misc", 0))
    except (TypeError, ValueError):
        return 0, 0


def sheet_view(character_id: int, state_id: int | None = None) -> dict[str, Any]:
    character = get_character(character_id)
    if character is None:
        raise ValueError("Character not found")
    states = list_states(character_id)
    selected_id = int(state_id or character["active_state_id"] or 0)
    state = get_state(selected_id, character_id)
    if state is None:
        raise ValueError("Level state not found")
    is_dnd = character["system_code"] == "dnd5e_2014"
    proficiency = dnd_proficiency_bonus(state["level"])
    abilities = []
    for key in ABILITY_KEYS:
        value = int(state["abilities"].get(key, 10 if is_dnd else 0))
        abilities.append({
            "key": key,
            "label": key.title(),
            "short": key[:3].upper(),
            "value": value,
            "modifier": ability_modifier(value) if is_dnd else value,
        })
    ability_values = {item["key"]: item["modifier"] for item in abilities}
    saves = []
    if is_dnd:
        save_defs = ((key, key.title(), key) for key in ABILITY_KEYS)
    else:
        save_defs = (
            ("fortitude", "Fortitude", "constitution"),
            ("reflex", "Reflex", "dexterity"),
            ("will", "Will", "wisdom"),
        )
    for key, label, ability in save_defs:
        rank, misc = _rank_details(state["saves"].get(key))
        rank_bonus = rank * proficiency if is_dnd else (state["level"] + rank * 2 if rank else 0)
        saves.append({
            "key": key,
            "label": label,
            "ability": ability,
            "rank": rank,
            "misc": misc,
            "total": ability_values[ability] + rank_bonus + misc,
        })
    skill_defs = DND_SKILLS if is_dnd else PF2_SKILLS
    skills = []
    for key, label, ability in skill_defs:
        rank, misc = _rank_details(state["skills"].get(key))
        rank_bonus = rank * proficiency if is_dnd else (state["level"] + rank * 2 if rank else 0)
        skills.append({
            "key": key,
            "label": label,
            "ability": ability,
            "ability_short": ability[:3].upper(),
            "rank": rank,
            "misc": misc,
            "total": ability_values[ability] + rank_bonus + misc,
        })
    entries = list_entries(selected_id)
    entries_by_kind = {kind: [] for kind in ENTRY_KINDS}
    for entry in entries:
        entries_by_kind[entry["kind"]].append(entry)
    return {
        "character": character,
        "system": SYSTEMS[character["system_code"]],
        "states": states,
        "state": state,
        "abilities": abilities,
        "saves": saves,
        "skills": skills,
        "entries": entries,
        "entries_by_kind": entries_by_kind,
        "proficiency_bonus": proficiency if is_dnd else None,
    }