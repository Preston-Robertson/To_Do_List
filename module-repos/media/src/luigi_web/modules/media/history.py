"""Local confirmed web activity; Sheets remain authoritative for current fields."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid4

from luigi_web import clock
from luigi_web.paths import DATA_DIR


class HistoryError(RuntimeError):
    """A local history operation could not be completed."""


class HistoryUnavailable(HistoryError):
    """Local storage is unavailable; do not claim history was saved."""


class HistoryConflict(HistoryError):
    """An operation was reused inconsistently or newer history intervened."""


_APPLICATION_ID = 0x4C574D48
_STATUSES = {
    "games": {"backlog", "playing", "paused", "completed", "achievements", "dropped"},
    "shows": {"backlog", "watching", "on_hold", "completed", "dropped"},
}
_ACTIVE = {"games": "playing", "shows": "watching"}
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS media_subjects (
        id INTEGER PRIMARY KEY AUTOINCREMENT, identity_key TEXT NOT NULL UNIQUE,
        section TEXT NOT NULL, profile_key TEXT NOT NULL, title_key TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS media_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id INTEGER NOT NULL REFERENCES media_subjects(id),
        number INTEGER NOT NULL, started_at TEXT, completed_at TEXT,
        status TEXT NOT NULL, origin TEXT NOT NULL,
        UNIQUE(subject_id, number))""",
    """CREATE TABLE IF NOT EXISTS media_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL UNIQUE,
        subject_id INTEGER NOT NULL REFERENCES media_subjects(id),
        kind TEXT NOT NULL, state TEXT NOT NULL,
        before_json TEXT NOT NULL, before_runs_json TEXT NOT NULL, expected_json TEXT,
        base_event_id INTEGER, undo_event_id INTEGER,
        event_id INTEGER REFERENCES media_events(id), created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS media_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id INTEGER NOT NULL REFERENCES media_subjects(id),
        operation_id TEXT NOT NULL UNIQUE REFERENCES media_operations(operation_id),
        kind TEXT NOT NULL, occurred_at TEXT NOT NULL, occurred_epoch INTEGER NOT NULL,
        changes_json TEXT NOT NULL, undo_json TEXT NOT NULL,
        undone_by INTEGER REFERENCES media_events(id))""",
    """CREATE UNIQUE INDEX IF NOT EXISTS media_pending_identity
        ON media_operations(subject_id) WHERE state IN ('pending', 'uncertain')""",
    """CREATE INDEX IF NOT EXISTS media_activity_identity
        ON media_events(subject_id, id DESC)""",
)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _text(value, limit: int, *, strip: bool = False) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("Invalid or oversized media history text.")
    return value.strip() if strip else value


def _section(section: str) -> str:
    section = _text(section, 32, strip=True).casefold()
    if section not in _STATUSES:
        raise ValueError("Unknown media section.")
    return section


def identity_key(section: str, profile: str, title: str) -> str:
    """Hash the JSON array of trimmed, casefolded section/profile/title values."""
    values = [_section(section), _text(profile, 256, strip=True).casefold(),
              _text(title, 512, strip=True).casefold()]
    if not values[2]:
        raise ValueError("A media title is required.")
    return hashlib.sha256(_json(values).encode("utf-8")).hexdigest()


def _number(value, *, integer: bool = False):
    if value is None or value == "":
        return None if integer else ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("Invalid media history number.")
    if len(str(value)) > 64:
        raise ValueError("Invalid media history number.")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number > 1_000_000_000 or number < 0:
            raise ValueError("Invalid media history number.")
        if integer:
            if number != number.to_integral_value():
                raise ValueError("Invalid media history integer.")
            return int(number)
        exponent = number.as_tuple().exponent
        if not isinstance(exponent, int) or exponent < -6:
            raise ValueError("Media history number has excessive precision.")
        return format(number.normalize(), "f")
    except InvalidOperation:
        raise ValueError("Invalid media history number.") from None


def _item(section: str, item: Mapping) -> dict:
    if not isinstance(item, Mapping):
        raise ValueError("A normalized media item is required.")
    result: dict = {field: _text(item.get(field, ""), limit, strip=field != "notes")
              for field, limit in (("profile", 256), ("title", 512), ("status", 32),
                                   ("platform", 512), ("genre", 512),
                                   ("date_started", 64), ("date_completed", 64), ("notes", 4096))}
    identity_key(section, result["profile"], result["title"])
    result["status"] = result["status"].casefold() or "backlog"
    if result["status"] not in _STATUSES[section]:
        raise ValueError("Unknown media status.")
    for field in ("current_episode", "current_season", "total_episodes", "priority", "rating"):
        result[field] = _number(item.get(field), integer=True)
    result["hours_played"] = _number(item.get("hours_played"))
    tags = item.get("tags")
    if tags is None:
        tags = []
    if not isinstance(tags, (list, tuple)) or len(tags) > 64:
        raise ValueError("Invalid or oversized media tags.")
    result["tags"] = [_text(tag, 128, strip=True) for tag in tags]
    return result


def _operation_id(value: str | None) -> str:
    if value is None:
        return str(uuid4())
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("A UUID operation identifier is required.") from None


@contextmanager
def _database():
    connection = None
    try:
        path = Path(os.environ.get("LUIGI_WEB_MEDIA_DB") or DATA_DIR / "media.sqlite3").expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        tables = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if application_id not in (0, _APPLICATION_ID) or (application_id == 0 and tables):
            raise HistoryUnavailable("Media history requires its own database.")
        connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
        for statement in _SCHEMA:
            connection.execute(statement)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(media_operations)")}
        if "expected_json" not in columns:
            connection.execute("ALTER TABLE media_operations ADD COLUMN expected_json TEXT")
        yield connection
        connection.commit()
    except (sqlite3.Error, OSError):
        raise HistoryUnavailable("Media history storage is unavailable.") from None
    finally:
        if connection is not None:
            connection.close()


def _subject(connection, section: str, item: dict) -> int:
    key = identity_key(section, item["profile"], item["title"])
    connection.execute(
        "INSERT OR IGNORE INTO media_subjects(identity_key, section, profile_key, title_key) VALUES (?, ?, ?, ?)",
        (key, section, item["profile"].casefold(), item["title"].casefold()),
    )
    return connection.execute("SELECT id FROM media_subjects WHERE identity_key = ?", (key,)).fetchone()[0]


def _latest(connection, subject_id: int):
    row = connection.execute(
        "SELECT id FROM media_events WHERE subject_id = ? ORDER BY id DESC LIMIT 1", (subject_id,)
    ).fetchone()
    return row[0] if row else None


def _runs(connection, subject_id: int, limit: int = -1) -> list[dict]:
    return [dict(row) for row in connection.execute(
        "SELECT id, number, started_at, completed_at, status, origin FROM media_runs "
        "WHERE subject_id = ? ORDER BY number DESC LIMIT ?", (subject_id, limit)
    )]


def _undo_target(connection, subject_id: int, event_id: int):
    row = connection.execute(
        "SELECT * FROM media_events WHERE id = ? AND subject_id = ?", (event_id, subject_id)
    ).fetchone()
    if row is None or row["kind"] == "undo" or row["undone_by"] is not None or _latest(connection, subject_id) != event_id:
        raise HistoryConflict("Undo requires the latest confirmed event for this item.")
    return row


def prepare_change(section: str, before: Mapping, kind: str = "update",
                   operation_id: str | None = None, *, undo_event_id: int | None = None,
                   expected_fields: dict | None = None) -> str:
    """Reserve an item before writing, including normalized intended fields and dates.

    Expected keys use the _item projection, never sheet column names, key or version.
    Include any date stamps in the write. Unknown keys and identity changes fail
    before storage; omitted intent remains blocked unless the full item is unchanged.
    """
    section = _section(section)
    before = _item(section, before)
    expected_json = None
    if expected_fields is not None:
        if not isinstance(expected_fields, dict) or not expected_fields.keys() <= before.keys():
            raise ValueError("Expected media fields must use known normalized field names.")
        desired = _item(section, {**before, **expected_fields})
        if identity_key(section, desired["profile"], desired["title"]) != identity_key(
                section, before["profile"], before["title"]):
            raise ValueError("Media history cannot change an item's identity.")
        expected_json = _json({field: desired[field] for field in expected_fields})
    if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", kind):
        raise ValueError("Invalid media activity kind.")
    if (kind == "undo") != (undo_event_id is not None):
        raise ValueError("Undo requires its original event identifier.")
    if undo_event_id is not None and (type(undo_event_id) is not int or undo_event_id < 1):
        raise ValueError("Invalid media event identifier.")
    operation_id = _operation_id(operation_id)
    with _database() as connection:
        subject_id = _subject(connection, section, before)
        existing = connection.execute(
            "SELECT * FROM media_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if existing:
            if (existing["subject_id"] != subject_id or existing["kind"] != kind
                    or _item(section, json.loads(existing["before_json"])) != before
                    or existing["expected_json"] != expected_json or existing["undo_event_id"] != undo_event_id):
                raise HistoryConflict("The operation identifier already belongs to a different change.")
            if existing["state"] == "unconfirmed":
                raise HistoryConflict("A known failed operation cannot be reused.")
            return operation_id
        if connection.execute(
            "SELECT 1 FROM media_operations WHERE subject_id = ? AND state IN ('pending', 'uncertain')",
            (subject_id,),
        ).fetchone():
            raise HistoryConflict("This item has an unresolved media write.")
        if undo_event_id is not None:
            _undo_target(connection, subject_id, undo_event_id)
        connection.execute(
            "INSERT INTO media_operations(operation_id, subject_id, kind, state, before_json, "
              "before_runs_json, expected_json, base_event_id, undo_event_id, created_at) "
              "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
            (operation_id, subject_id, kind, _json(before), _json(_runs(connection, subject_id)),
               expected_json, _latest(connection, subject_id), undo_event_id, clock.local_now().isoformat()),
        )
    return operation_id


def _changes(before: dict, item: dict) -> dict:
    return {field: {"before": before[field], "after": item[field]}
            for field in before if before[field] != item[field]}


def _completed(section: str, item: dict) -> bool:
    return item["status"] in ({"completed", "achievements"} if section == "games" else {"completed"})


def _legacy_date(value: str) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(value).isoformat()
        except ValueError:
            return None


def _insert_run(connection, subject_id, started_at, completed_at, status, origin):
    connection.execute(
        "INSERT INTO media_runs(subject_id, number, started_at, completed_at, status, origin) "
        "VALUES (?, (SELECT COALESCE(MAX(number), 0) + 1 FROM media_runs WHERE subject_id = ?), ?, ?, ?, ?)",
        (subject_id, subject_id, started_at, completed_at, status, origin),
    )


def _apply_runs(connection, subject_id: int, section: str, before: dict, item: dict, kind: str, now: str):
    runs = _runs(connection, subject_id, 1)
    if not runs and (_completed(section, before) or _legacy_date(before["date_completed"])):
        _insert_run(connection, subject_id, _legacy_date(before["date_started"]),
                    _legacy_date(before["date_completed"]), "completed", "legacy_snapshot")
    active = runs[0] if runs and runs[0]["origin"] == "web" and runs[0]["status"] != "completed" else None
    if kind == "replay":
        if item["status"] != _ACTIVE[section]:
            raise HistoryConflict("A replay must confirm an active media status.")
        _insert_run(connection, subject_id, now, None, item["status"], "web")
    elif _completed(section, item) and not _completed(section, before):
        if active:
            connection.execute(
                "UPDATE media_runs SET completed_at = ?, status = 'completed' WHERE id = ? AND subject_id = ?",
                (now, active["id"], subject_id),
            )
        else:
            started_at = _legacy_date(before["date_started"]) or _legacy_date(item["date_started"])
            _insert_run(connection, subject_id, started_at, now, "completed", "web")
    elif item["status"] == _ACTIVE[section] and before["status"] != item["status"] and not active:
        _insert_run(connection, subject_id, now, None, item["status"], "web")
    elif active and before["status"] != item["status"] and not _completed(section, item):
        connection.execute("UPDATE media_runs SET status = ? WHERE id = ? AND subject_id = ?",
                           (item["status"], active["id"], subject_id))


def _restore_runs(connection, subject_id: int, snapshot: str):
    connection.execute("DELETE FROM media_runs WHERE subject_id = ?", (subject_id,))
    for run in json.loads(snapshot):
        connection.execute(
            "INSERT INTO media_runs(id, subject_id, number, started_at, completed_at, status, origin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run["id"], subject_id, run["number"], run["started_at"], run["completed_at"], run["status"], run["origin"]),
        )


def finish_change(operation_id: str, item: Mapping) -> int:
    """Confirm only a verified external result; retry the same UUID after a local failure."""
    operation_id = _operation_id(operation_id)
    with _database() as connection:
        operation = connection.execute(
            "SELECT media_operations.*, media_subjects.section, media_subjects.identity_key "
            "FROM media_operations JOIN media_subjects ON media_subjects.id = subject_id WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if operation is None:
            raise HistoryConflict("Unknown media operation.")
        section = operation["section"]
        item = _item(section, item)
        if identity_key(section, item["profile"], item["title"]) != operation["identity_key"]:
            raise HistoryConflict("Media history cannot change an item's identity.")
        before = json.loads(operation["before_json"])
        changes = _changes(before, item)
        if operation["state"] == "confirmed":
            existing = connection.execute("SELECT changes_json FROM media_events WHERE id = ?", (operation["event_id"],)).fetchone()
            if existing[0] != _json(changes):
                raise HistoryConflict("The operation was already confirmed with a different result.")
            return operation["event_id"]
        if operation["state"] == "unconfirmed":
            raise HistoryConflict("A known failed operation cannot be confirmed.")
        subject_id = operation["subject_id"]
        if (_latest(connection, subject_id) != operation["base_event_id"]
                or _json(_runs(connection, subject_id)) != operation["before_runs_json"]):
            raise HistoryConflict("Media history changed after this operation was prepared.")
        now = clock.local_now()
        if operation["kind"] == "undo":
            target = _undo_target(connection, subject_id, operation["undo_event_id"])
            inverse = {field: {"before": values["after"], "after": values["before"]}
                       for field, values in json.loads(target["changes_json"]).items()}
            if changes != inverse:
                raise HistoryConflict("The confirmed result does not exactly undo the original change.")
            _restore_runs(connection, subject_id, target["undo_json"])
        else:
            _apply_runs(connection, subject_id, section, before, item, operation["kind"], now.isoformat())
        cursor = connection.execute(
            "INSERT INTO media_events(subject_id, operation_id, kind, occurred_at, occurred_epoch, changes_json, undo_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (subject_id, operation_id, operation["kind"], now.isoformat(), int(now.timestamp()),
             _json(changes), operation["before_runs_json"]),
        )
        event_id = cursor.lastrowid
        if event_id is None:
            raise HistoryUnavailable("Media history confirmation did not produce an event.")
        if operation["kind"] == "undo":
            connection.execute("UPDATE media_events SET undone_by = ? WHERE id = ? AND subject_id = ?",
                               (event_id, operation["undo_event_id"], subject_id))
        connection.execute("UPDATE media_operations SET state = 'confirmed', event_id = ? WHERE operation_id = ?",
                           (event_id, operation_id))
        return event_id


def fail_change(operation_id: str, uncertain: bool = False) -> None:
    """Retain a warning; uncertain writes block new writes until explicitly reconciled.

    Use uncertain=False only when the external write is known not to have happened.
    A confirmed operation is never demoted, including after a lost response.
    """
    operation_id = _operation_id(operation_id)
    if type(uncertain) is not bool:
        raise ValueError("The uncertain flag must be a boolean.")
    with _database() as connection:
        operation = connection.execute("SELECT state FROM media_operations WHERE operation_id = ?", (operation_id,)).fetchone()
        if operation is None:
            raise HistoryConflict("Unknown media operation.")
        if operation["state"] != "confirmed":
            connection.execute("UPDATE media_operations SET state = ? WHERE operation_id = ?",
                               ("uncertain" if uncertain else "unconfirmed", operation_id))


def reconcile(section: str, item: Mapping) -> dict:
    """Reconcile an interrupted write against a fresh authoritative normalized item.

    Call only under the parent's mutation lock, with no active write for this item.
    The result contains resolved (a reservation was released), pending_count
    (including retained unconfirmed history), and a generic warning or None.
    Recovery preserves the operation kind; event/run web timestamps record this
    confirmation, not a reconstructed external write time. Confirmation failures
    leave the reservation blocked. Storage failures before inspection raise.
    """
    section = _section(section)
    item = _item(section, item)
    key = identity_key(section, item["profile"], item["title"])
    with _database() as connection:
        subject = connection.execute("SELECT id FROM media_subjects WHERE identity_key = ?", (key,)).fetchone()
        if subject is None:
            return {"resolved": False, "pending_count": 0, "warning": None}
        pending_count = connection.execute(
            "SELECT COUNT(*) FROM media_operations WHERE subject_id = ? AND state != 'confirmed'", (subject[0],)
        ).fetchone()[0]
        operation = connection.execute(
            "SELECT * FROM media_operations WHERE subject_id = ? AND state IN ('pending', 'uncertain')", (subject[0],)
        ).fetchone()
    result = {"resolved": False, "pending_count": pending_count,
              "warning": "A recorded write was not confirmed." if pending_count else None}
    if operation is None:
        return result
    before = _item(section, json.loads(operation["before_json"]))
    expected = json.loads(operation["expected_json"]) if operation["expected_json"] is not None else None
    intended_keys = expected if expected is not None else before
    try:
        if (expected is not None and all(item[field] == value for field, value in expected.items())
                and any(before[field] != value for field, value in expected.items())):
            finish_change(operation["operation_id"], item)
            result["pending_count"] -= 1
            result["warning"] = "A recorded write was not confirmed." if result["pending_count"] else None
        elif all(item[field] == before[field] for field in intended_keys):
            fail_change(operation["operation_id"], uncertain=False)
        elif expected is not None:
            with _database() as connection:
                connection.execute(
                    "UPDATE media_operations SET state = 'unconfirmed' "
                    "WHERE operation_id = ? AND state IN ('pending', 'uncertain')", (operation["operation_id"],)
                )
            result["warning"] = "History could not reconstruct an interrupted change"
        else:
            result["warning"] = "History could not reconstruct an interrupted change"
            return result
    except HistoryError:
        result["warning"] = "History could not confirm an interrupted change"
        return result
    result["resolved"] = True
    return result


def record_change(section: str, before: Mapping, item: Mapping, kind: str = "update",
                  operation_id: str | None = None) -> int:
    """Record an already-confirmed change. Replay callers must prepare BEFORE Sheets."""
    operation_id = prepare_change(section, before, kind, operation_id)
    return finish_change(operation_id, item)


def undo_change(section: str, event_id: int, before: Mapping, item: Mapping, *,
                operation_id: str | None = None) -> int:
    """Record an exact confirmed undo; use prepare_change(kind='undo') before its write."""
    operation_id = prepare_change(section, before, "undo", operation_id, undo_event_id=event_id)
    return finish_change(operation_id, item)


def detail(section: str, profile: str, title: str) -> dict:
    """Return at most 200 events and 100 runs, newest first, plus unresolved count."""
    key = identity_key(section, profile, title)
    with _database() as connection:
        subject = connection.execute("SELECT id FROM media_subjects WHERE identity_key = ?", (key,)).fetchone()
        if subject is None:
            return {"activity": [], "runs": [], "pending_count": 0}
        subject_id = subject[0]
        activity = [{"id": row["id"], "kind": row["kind"], "occurred_at": row["occurred_at"],
                     "changes": json.loads(row["changes_json"])} for row in connection.execute(
                         "SELECT * FROM media_events WHERE subject_id = ? ORDER BY id DESC LIMIT 200", (subject_id,))]
        pending = connection.execute("SELECT COUNT(*) FROM media_operations WHERE subject_id = ? AND state != 'confirmed'",
                                     (subject_id,)).fetchone()[0]
        return {"activity": activity, "runs": _runs(connection, subject_id, 100), "pending_count": pending}


def insights(section: str, profile: str = "") -> dict:
    """Count retained completions and non-undone web changes, never lifetime averages.

    Recent completions exclude legacy snapshots. Positive counter changes exclude
    replay resets; episode increments count only within the same season.
    """
    section = _section(section)
    profile = _text(profile, 256, strip=True).casefold()
    now = clock.local_now()
    cutoff = int((now - timedelta(days=30)).timestamp())
    end = int(now.timestamp())
    with _database() as connection:
        scope = "subject_id IN (SELECT id FROM media_subjects WHERE section = ? AND (? = '' OR profile_key = ?))"
        parameters = (section, profile, profile)
        counts = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(origin = 'web'), 0), COALESCE(SUM(origin = 'legacy_snapshot'), 0) "
            "FROM media_runs WHERE status = 'completed' AND " + scope, parameters,
        ).fetchone()
        recent_completions = connection.execute(
            "SELECT COUNT(*) FROM media_runs WHERE status = 'completed' AND origin = 'web' "
            "AND unixepoch(completed_at) BETWEEN ? AND ? AND " + scope, (cutoff, end, *parameters),
        ).fetchone()[0]
        event_filter = "undone_by IS NULL AND kind != 'undo' AND (changes_json != '{}' OR kind = 'replay') AND " + scope
        confirmed = connection.execute("SELECT COUNT(*) FROM media_events WHERE " + event_filter, parameters).fetchone()[0]
        recent = connection.execute(
            "SELECT kind, changes_json FROM media_events WHERE occurred_epoch BETWEEN ? AND ? AND " + event_filter,
            (cutoff, end, *parameters),
        )
        hours = Decimal(0)
        episodes = 0
        recent_count = 0
        for event in recent:
            recent_count += 1
            if event["kind"] == "replay":
                continue
            changes = json.loads(event["changes_json"])
            progress = changes.get("hours_played")
            if section == "games" and progress and progress["before"] != "" and progress["after"] != "":
                hours += max(Decimal(0), Decimal(progress["after"]) - Decimal(progress["before"]))
            progress = changes.get("current_episode")
            if section == "shows" and progress and "current_season" not in changes:
                if progress["before"] is not None and progress["after"] is not None:
                    episodes += max(0, progress["after"] - progress["before"])
        pending = connection.execute("SELECT COUNT(*) FROM media_operations WHERE state != 'confirmed' AND " + scope,
                                     parameters).fetchone()[0]
        return {"completed_runs": counts[0], "recorded_completed_runs": counts[1], "legacy_completed_runs": counts[2],
                "confirmed_changes": confirmed, "pending_count": pending,
                "last_30_days": {"completed_runs": recent_completions, "confirmed_changes": recent_count,
                                 "hours_added": float(hours), "episodes_added": episodes}}