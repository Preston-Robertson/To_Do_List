"""Game'N'Watch integration for luigi-web.

Reads and writes the same Google Sheet the Game'N'Watch Discord bot uses
(https://github.com/Preston-Robertson/Game-N-Watch) so the web GUI can surface
your games/shows backlog alongside tasks.

Design:
* **Optional + graceful.** If gspread/google-auth aren't installed, or the
  sheet id / service-account credentials aren't configured, every public
  function still returns safely and ``disabled_reason()`` explains why. The
  routes render a friendly "not configured" notice instead of 500-ing.
* **Header-addressed.** We map columns by their header string (row 1), not by
  fixed position, so the bot appending new columns never shifts our reads.
* **Lightly cached.** ``get_all_values`` hits Google over the network, so list
  reads are cached for a few seconds and invalidated on write. Keeps the board
  snappy and stays well under Sheets API quota.

Config (env):
    LUIGI_WEB_GNW_SHEET_ID     Google Sheet ID (same one the bot uses)
    LUIGI_WEB_GNW_CREDS_FILE   path to the service-account credentials.json
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from datetime import date
from typing import Any

# gspread + google-auth are optional. Import lazily-tolerant so the whole app
# doesn't fail to boot on a box where they aren't installed yet.
try:  # pragma: no cover - import shape depends on the environment
    import gspread
    from google.oauth2.service_account import Credentials
    _IMPORT_ERROR: str | None = None
except Exception as exc:  # noqa: BLE001
    gspread = None  # type: ignore[assignment]
    Credentials = None  # type: ignore[assignment]
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# --------------------------------------------------------------------------- #
# Schema (mirrors the bot's cogs/db.py — the first 12 columns are positional
# there, but we address everything by header name so order changes are safe).
# --------------------------------------------------------------------------- #
GAME_HEADERS = [
    "Profile", "Title", "Status", "Priority", "Rating", "Notes", "Platform",
    "Todos", "Release Date", "Price", "Developers", "Is Multiplayer",
    "Date Added", "Date Started", "Date Completed", "Hours Played", "Tags",
    "Genre", "Cover URL", "External ID", "Source", "Last Played", "Times Picked",
]
SHOW_HEADERS = [
    "Profile", "Title", "Status", "Priority", "Rating", "Notes", "Genre",
    "Current Episode", "Current Season", "Total Episodes", "Platform",
    "Premiere Date", "Date Added", "Date Started", "Date Completed", "Tags",
    "Cover URL", "External ID", "Source", "Last Watched", "Episode Notes",
    "Runtime", "Times Picked",
]

GAME_STATUSES = ["backlog", "playing", "completed", "dropped"]
SHOW_STATUSES = ["backlog", "watching", "on_hold", "completed", "dropped"]

STATUS_LABELS = {
    "backlog": "Backlog",
    "playing": "Playing",
    "watching": "Watching",
    "on_hold": "On Hold",
    "completed": "Completed",
    "dropped": "Dropped",
}

# The "active" status per section (used by the random picker's default pool
# and by status-transition date stamping).
ACTIVE_STATUS = {"games": "playing", "shows": "watching"}

# Fields the GUI edit form may change → the sheet header they map to.
GAME_EDITABLE = {
    "status": "Status", "priority": "Priority", "rating": "Rating",
    "notes": "Notes", "platform": "Platform", "genre": "Genre", "tags": "Tags",
}
SHOW_EDITABLE = {
    "status": "Status", "priority": "Priority", "rating": "Rating",
    "notes": "Notes", "genre": "Genre", "platform": "Platform", "tags": "Tags",
    "current_episode": "Current Episode", "current_season": "Current Season",
    "total_episodes": "Total Episodes",
}

# --------------------------------------------------------------------------- #
# Client + cache
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_client = None
_sheet = None
_cache: dict[str, tuple[float, list[list[str]]]] = {}
_CACHE_TTL = 20.0  # seconds


# Default location for the service-account key when LUIGI_WEB_GNW_CREDS_FILE
# isn't set. Lives next to the app code so it's inside the systemd unit's
# ReadWritePaths (=/opt/luigi-web) — meaning the app can WRITE it from the
# Admin page without any host-side file juggling. /etc is read-only to the
# service (ProtectSystem=strict), so we deliberately don't default there.
DEFAULT_CREDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "gnw-credentials.json")


def _sheet_id() -> str:
    return os.environ.get("LUIGI_WEB_GNW_SHEET_ID", "").strip()


def _creds_file() -> str:
    return os.environ.get("LUIGI_WEB_GNW_CREDS_FILE", "").strip() or DEFAULT_CREDS_PATH


def credentials_path() -> str:
    """Resolved path where the service-account key is read from / written to."""
    return _creds_file()


def disabled_reason() -> str | None:
    """Return why the integration is off, or None if it's ready to use."""
    if gspread is None or Credentials is None:
        return f"gspread/google-auth not installed ({_IMPORT_ERROR})"
    if not _sheet_id():
        return "LUIGI_WEB_GNW_SHEET_ID is not set"
    if not _creds_file():
        return "LUIGI_WEB_GNW_CREDS_FILE is not set"
    if not os.path.isfile(_creds_file()):
        return f"credentials file not found: {_creds_file()}"
    return None


def is_enabled() -> bool:
    return disabled_reason() is None


def _get_sheet():
    global _client, _sheet
    if _sheet is None:
        creds = Credentials.from_service_account_file(_creds_file(), scopes=SCOPES)
        _client = gspread.authorize(creds)
        _sheet = _client.open_by_key(_sheet_id())
    return _sheet


def reset_client() -> None:
    """Drop the cached client/sheet handle + row cache. Call after the sheet
    id or credentials path changes (e.g. via the Admin env editor)."""
    global _client, _sheet
    with _lock:
        _client = None
        _sheet = None
        _cache.clear()


def save_credentials(json_text: str) -> tuple[bool, str]:
    """Validate + persist a pasted service-account ``credentials.json``.

    Writes atomically (0600) to ``credentials_path()`` and resets the client so
    the new key takes effect immediately — no restart needed as long as the
    Sheet ID is already in the process environment. Returns ``(ok, message)``:
    on success ``message`` is the service-account email (for confirmation), on
    failure it's a human-readable reason.
    """
    text = (json_text or "").strip()
    if not text:
        return False, "Paste the credentials.json contents first."
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, f"That isn't valid JSON: {exc}"
    if not isinstance(data, dict):
        return False, "Expected a JSON object (the service-account key file)."
    if data.get("type") != "service_account":
        return False, ('This doesn\'t look like a service-account key '
                       '(missing "type": "service_account"). Download the key '
                       'from Google Cloud → Service Accounts → Keys.')
    missing = [k for k in ("client_email", "private_key", "project_id")
               if not data.get(k)]
    if missing:
        return False, f"Key file is missing required fields: {', '.join(missing)}."

    path = credentials_path()
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False, (f"Couldn't write {path}: {exc}. The service may lack "
                       "write access there — leave LUIGI_WEB_GNW_CREDS_FILE "
                       "unset so it uses the app-managed path.")

    reset_client()
    return True, str(data.get("client_email") or "")


def credentials_status() -> dict[str, Any]:
    """Snapshot for the Admin page: where creds live, the account email, and
    whether the integration is fully wired up."""
    path = credentials_path()
    exists = os.path.isfile(path)
    email = None
    if exists:
        try:
            with open(path, encoding="utf-8") as fh:
                email = json.load(fh).get("client_email")
        except Exception:  # noqa: BLE001
            email = None
    return {
        "path": path,
        "exists": exists,
        "client_email": email,
        "sheet_id_set": bool(_sheet_id()),
        "enabled": is_enabled(),
        "reason": disabled_reason(),
    }


def _tab(section: str) -> str:
    return "Games" if section == "games" else "Shows"


def _ws(section: str):
    return _get_sheet().worksheet(_tab(section))


def _all_values(section: str, force: bool = False) -> list[list[str]]:
    now = time.monotonic()
    if not force:
        with _lock:
            cached = _cache.get(section)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]
    values = _ws(section).get_all_values()
    with _lock:
        _cache[section] = (now, values)
    return values


def _invalidate(section: str | None = None) -> None:
    with _lock:
        if section:
            _cache.pop(section, None)
        else:
            _cache.clear()


# --------------------------------------------------------------------------- #
# Row → dict
# --------------------------------------------------------------------------- #
def _to_int(v: Any, default: int | None = None) -> int | None:
    try:
        s = str(v).strip()
        return int(float(s)) if s else default
    except (ValueError, TypeError):
        return default


def _bool(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "yes", "1", "on")


def _build_link(source: str, ext_id: str) -> str | None:
    source = (source or "").strip().lower()
    ext_id = (ext_id or "").strip()
    if not ext_id:
        return None
    if source == "steam":
        return f"https://store.steampowered.com/app/{ext_id}"
    if source == "tvmaze":
        return f"https://www.tvmaze.com/shows/{ext_id}"
    if source == "anilist":
        return f"https://anilist.co/anime/{ext_id}"
    if source == "youtube":
        return f"https://www.youtube.com/playlist?list={ext_id}"
    return None


def _row_to_item(section: str, headers: list[str], row: list[str]) -> dict[str, Any]:
    idx = {h: i for i, h in enumerate(headers)}

    def g(header: str, default: str = "") -> str:
        i = idx.get(header)
        if i is None or i >= len(row):
            return default
        val = row[i]
        return val if val != "" else default

    source = g("Source")
    ext_id = g("External ID")
    item: dict[str, Any] = {
        "section": section,
        "profile": g("Profile"),
        "title": g("Title"),
        "status": g("Status", "backlog"),
        "priority": _to_int(g("Priority"), 3),
        "rating": _to_int(g("Rating"), None),
        "notes": g("Notes"),
        "platform": g("Platform"),
        "genre": g("Genre"),
        "tags": [t.strip() for t in g("Tags").split(",") if t.strip()],
        "cover_url": g("Cover URL"),
        "source": source,
        "external_id": ext_id,
        "link": _build_link(source, ext_id),
        "times_picked": _to_int(g("Times Picked"), 0),
    }
    if section == "games":
        item.update({
            "is_multiplayer": _bool(g("Is Multiplayer")),
            "price": g("Price"),
            "developers": g("Developers"),
            "release_date": g("Release Date"),
            "hours_played": g("Hours Played"),
        })
    else:
        item.update({
            "current_episode": _to_int(g("Current Episode"), 0),
            "current_season": _to_int(g("Current Season"), 1),
            "total_episodes": _to_int(g("Total Episodes"), None),
            "premiere_date": g("Premiere Date"),
            "runtime": g("Runtime"),
        })
    return item


# --------------------------------------------------------------------------- #
# Public reads
# --------------------------------------------------------------------------- #
def list_profiles() -> list[str]:
    """Distinct profile names across both sheets, sorted."""
    if not is_enabled():
        return []
    names: set[str] = set()
    for section in ("games", "shows"):
        values = _all_values(section)
        for row in values[1:] if values else []:
            if row and row[0].strip():
                names.add(row[0].strip())
    return sorted(names, key=str.lower)


def list_items(section: str, profile: str | None = None) -> list[dict[str, Any]]:
    """All items for a section, optionally filtered to one profile.
    ``profile=None`` (or "" / "all") returns every profile's items."""
    if not is_enabled():
        return []
    values = _all_values(section)
    if not values:
        return []
    headers = values[0]
    want = (profile or "").strip().lower()
    out: list[dict[str, Any]] = []
    for row in values[1:]:
        if not row or len(row) < 2 or not row[0].strip() or not row[1].strip():
            continue
        if want and want != "all" and row[0].strip().lower() != want:
            continue
        out.append(_row_to_item(section, headers, row))
    out.sort(key=lambda d: (-(d.get("priority") or 0), d["title"].lower()))
    return out


def get_item(section: str, profile: str, title: str) -> dict[str, Any] | None:
    if not is_enabled():
        return None
    values = _all_values(section)
    if not values:
        return None
    headers = values[0]
    for row in values[1:]:
        if (len(row) >= 2 and row[0].strip().lower() == profile.lower()
                and row[1].strip().lower() == title.lower()):
            return _row_to_item(section, headers, row)
    return None


def statuses_for(section: str) -> list[str]:
    return GAME_STATUSES if section == "games" else SHOW_STATUSES


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def _find_row_idx(section: str, profile: str, title: str) -> tuple[int | None, list[str]]:
    """1-based row index of (profile, title) + the header row. Forces a fresh
    read so we never write to a stale row position."""
    values = _all_values(section, force=True)
    headers = values[0] if values else []
    for i, row in enumerate(values[1:], start=2):
        if (len(row) >= 2 and row[0].strip().lower() == profile.lower()
                and row[1].strip().lower() == title.lower()):
            return i, headers
    return None, headers


def update_item(section: str, profile: str, title: str,
                fields: dict[str, Any]) -> bool:
    """Write editable fields back to the sheet. ``fields`` keys are the GUI
    dict keys (see GAME_EDITABLE / SHOW_EDITABLE); unknown keys are ignored.
    Mirrors the bot's status-transition date stamping."""
    if not is_enabled():
        return False
    editable = GAME_EDITABLE if section == "games" else SHOW_EDITABLE
    row_idx, headers = _find_row_idx(section, profile, title)
    if not row_idx:
        return False
    hidx = {h: i + 1 for i, h in enumerate(headers)}  # 1-based columns
    ws = _ws(section)

    old_status = (get_item(section, profile, title) or {}).get("status")

    for key, val in fields.items():
        header = editable.get(key)
        if not header or header not in hidx:
            continue
        cell = "" if val is None else str(val)
        ws.update_cell(row_idx, hidx[header], cell)

    # Date stamping on status change (only if the target cell is empty, to
    # preserve the earliest timestamp — same rule as the bot).
    new_status = fields.get("status", old_status)
    if new_status and new_status != old_status:
        if new_status == ACTIVE_STATUS[section]:
            _stamp_if_empty(ws, row_idx, hidx, "Date Started")
        elif new_status == "completed":
            _stamp_if_empty(ws, row_idx, hidx, "Date Completed")

    _invalidate(section)
    return True


def _stamp_if_empty(ws, row_idx: int, hidx: dict[str, int], header: str) -> None:
    col = hidx.get(header)
    if not col:
        return
    try:
        existing = ws.cell(row_idx, col).value
    except Exception:  # noqa: BLE001
        existing = None
    if existing and str(existing).strip():
        return
    try:
        ws.update_cell(row_idx, col, date.today().isoformat())
    except Exception:  # noqa: BLE001
        pass


def set_status(section: str, profile: str, title: str, status: str) -> bool:
    if status not in statuses_for(section):
        raise ValueError(f"invalid status for {section}: {status}")
    return update_item(section, profile, title, {"status": status})


# --------------------------------------------------------------------------- #
# Weighted random picker (priority-weighted, like the bot's /random)
# --------------------------------------------------------------------------- #
def random_pick(section: str, profile: str | None = None,
                statuses: list[str] | None = None,
                bump: bool = True) -> dict[str, Any] | None:
    """Priority-weighted random pick from the pool. Priority 5 items are 5×
    as likely as priority 1. Defaults to the backlog + active pool."""
    if not is_enabled():
        return None
    if statuses is None:
        statuses = ["backlog", ACTIVE_STATUS[section]]
    pool = [i for i in list_items(section, profile) if i["status"] in statuses]
    if not pool:
        return None
    weights = [max(1, int(i.get("priority") or 1)) for i in pool]
    choice = random.choices(pool, weights=weights, k=1)[0]
    if bump:
        _bump_times_picked(section, choice["profile"], choice["title"])
    return choice


def _bump_times_picked(section: str, profile: str, title: str) -> None:
    try:
        row_idx, headers = _find_row_idx(section, profile, title)
        if not row_idx:
            return
        hidx = {h: i + 1 for i, h in enumerate(headers)}
        col = hidx.get("Times Picked")
        if not col:
            return
        ws = _ws(section)
        current = _to_int(ws.cell(row_idx, col).value, 0) or 0
        ws.update_cell(row_idx, col, current + 1)
        _invalidate(section)
    except Exception:  # noqa: BLE001
        pass
