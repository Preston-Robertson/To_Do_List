"""Game'N'Watch integration for luigi-web.

Reads and writes the same Google Sheet the Game'N'Watch Discord bot uses, so
the web GUI can surface
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
import re
import threading
import time
from datetime import date
from typing import Any

import httpx

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

GAME_STATUSES = [
    "backlog", "playing", "paused", "completed", "achievements", "dropped",
]
SHOW_STATUSES = ["backlog", "watching", "on_hold", "completed", "dropped"]

STATUS_LABELS = {
    "backlog": "Backlog",
    "playing": "Playing",
    "paused": "Paused",
    "watching": "Watching",
    "on_hold": "On Hold",
    "completed": "Completed",
    "achievements": "100% Achievements",
    "dropped": "Dropped",
}

# The "active" status per section (used by the random picker's default pool
# and by status-transition date stamping).
ACTIVE_STATUS = {"games": "playing", "shows": "watching"}

# Fields the GUI edit form may change → the sheet header they map to.
GAME_EDITABLE = {
    "status": "Status", "priority": "Priority", "rating": "Rating",
    "notes": "Notes", "platform": "Platform", "genre": "Genre", "tags": "Tags",
    "hours_played": "Hours Played",
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
_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CREDS_PATH = os.path.join(_REPO_DIR, "gnw-credentials.json")

_SHEET_URL_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")


def _normalize_sheet_id(value: str) -> str:
    """Accept either a bare spreadsheet ID or a normal Google Sheets URL.

    The Admin form historically asked for "the id from the sheet URL", but it
    is natural to paste the full URL. Passing that URL to gspread.open_by_key()
    creates a malformed Sheets API request (and can surface as a misleading
    char-0 JSONDecodeError), so extract the key before making any API call.
    """
    value = (value or "").strip()
    match = _SHEET_URL_ID_RE.search(value)
    return match.group(1) if match else value


def _sheet_id() -> str:
    return _normalize_sheet_id(os.environ.get("LUIGI_WEB_GNW_SHEET_ID", ""))


def _creds_file() -> str:
    """Absolute path to the service-account key.

    Blank ``LUIGI_WEB_GNW_CREDS_FILE`` → the app-managed default. A *relative*
    override (e.g. a bare ``luigi-web-gnw.json``) is resolved against the repo
    dir, NOT the process CWD — otherwise "Save credentials" (which writes here)
    and the reader could land on different files depending on where the service
    happened to be started, leaving an empty/missing key. An absolute override
    is used verbatim.
    """
    configured = os.environ.get("LUIGI_WEB_GNW_CREDS_FILE", "").strip()
    if not configured:
        return DEFAULT_CREDS_PATH
    if not os.path.isabs(configured):
        return os.path.join(_REPO_DIR, configured)
    return configured


def credentials_path() -> str:
    """Resolved path where the service-account key is read from / written to."""
    return _creds_file()


# Fields a Google service-account key must have for us to even attempt a
# connection. Mirrors the validation in save_credentials().
_REQUIRED_CRED_FIELDS = ("client_email", "private_key", "project_id")


def _validate_creds_file(path: str) -> str | None:
    """Return a human-readable reason the credentials file is unusable, or None
    if it parses as a service-account key with the required fields.

    Catches the common "file exists but is empty / half-written / not JSON"
    case up front so callers get an actionable message instead of a raw
    ``JSONDecodeError`` from deep inside google-auth at connect time.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        return f"credentials file at {path} couldn't be read ({exc})"
    if not raw.strip():
        return (f"credentials file at {path} is empty — paste the "
                "service-account key again in Admin → Game'N'Watch credentials.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return (f"credentials file at {path} isn't valid JSON — paste the "
                "service-account key again in Admin → Game'N'Watch credentials.")
    if not isinstance(data, dict):
        return (f"credentials file at {path} isn't a JSON object — paste the "
                "full service-account key in Admin → Game'N'Watch credentials.")
    if data.get("type") != "service_account":
        return (f"credentials file at {path} isn't a service-account key "
                "(missing \"type\": \"service_account\"). Re-download the key "
                "from Google Cloud → Service Accounts → Keys and paste it in Admin.")
    missing = [k for k in _REQUIRED_CRED_FIELDS if not data.get(k)]
    if missing:
        return (f"credentials file at {path} is missing required fields "
                f"({', '.join(missing)}) — re-paste the full key in Admin.")
    return None


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
    bad = _validate_creds_file(_creds_file())
    if bad:
        return bad
    return None


def is_enabled() -> bool:
    return disabled_reason() is None


def _load_credentials():
    """Read + parse the service-account key *ourselves* (via
    ``from_service_account_info``) rather than ``from_service_account_file``.

    Two reasons:
    * A bad key file produces a precise, actionable message here instead of a
      raw ``JSONDecodeError`` bubbling up from deep inside google-auth.
    * Because we've fully consumed + parsed the file at this point, any
      ``JSONDecodeError`` raised *later* in ``_get_sheet`` can ONLY come from
      the Google API layer (gspread parsing an empty/non-JSON HTTP response) —
      so the two failure domains stop being ambiguous.
    """
    path = _creds_file()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise RuntimeError(f"credentials file {path} can't be read ({exc})") from exc
    if not raw.strip():
        raise RuntimeError(
            f"credentials file {path} is empty ({len(raw)} bytes) — re-paste the "
            "service-account key in Admin → Game'N'Watch credentials"
        )
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"credentials file {path} isn't valid JSON ({exc}) — re-paste the key "
            "in Admin → Game'N'Watch credentials"
        ) from exc
    try:
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"credentials file {path} isn't a usable service-account key "
            f"({type(exc).__name__}: {exc})"
        ) from exc


def _get_sheet():
    global _client, _sheet
    if _sheet is None:
        creds = _load_credentials()
        try:
            client = gspread.authorize(creds)
            sheet = client.open_by_key(_sheet_id())
        except Exception as exc:  # noqa: BLE001
            # Past _load_credentials the key is known-good, so a failure here is
            # the Google API / network layer — NOT the credentials file. gspread
            # raises a char-0 JSONDecodeError when the API returns an EMPTY body,
            # which on this LAN box most often means egress to googleapis.com is
            # blocked/proxied, or the Sheets API is disabled for the project.
            # A wrong/again-unshared sheet usually raises APIError /
            # SpreadsheetNotFound with detail instead.
            raise RuntimeError(
                f"Google Sheets API call failed ({type(exc).__name__}: {exc}). "
                f"Sheet ID={_sheet_id()!r}. Verify this host can reach "
                "googleapis.com (LAN egress / firewall / DNS), the Sheets API is "
                "enabled for the project, and the sheet is shared with the "
                "service-account email."
            ) from exc
        _client, _sheet = client, sheet
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


def _header_index(headers: list[str]) -> dict[str, int]:
    """Map trimmed header names to zero-based columns."""
    return {str(header).strip(): i for i, header in enumerate(headers)}


def _identity_columns(headers: list[str]) -> tuple[int, int]:
    idx = _header_index(headers)
    missing = [name for name in ("Profile", "Title") if name not in idx]
    if missing:
        raise RuntimeError(
            f"{', '.join(missing)} column missing from the sheet header row"
        )
    return idx["Profile"], idx["Title"]


def _row_value(row: list[str], column: int) -> str:
    return row[column].strip() if column < len(row) else ""


def _a1(row: int, column: int) -> str:
    label = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        label = chr(65 + remainder) + label
    return f"{label}{row}"


def _row_to_item(section: str, headers: list[str], row: list[str]) -> dict[str, Any]:
    idx = _header_index(headers)

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
        if not values:
            continue
        profile_col, _ = _identity_columns(values[0])
        for row in values[1:] if values else []:
            profile = _row_value(row, profile_col)
            if profile:
                names.add(profile)
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
    profile_col, title_col = _identity_columns(headers)
    want = (profile or "").strip().lower()
    out: list[dict[str, Any]] = []
    for row in values[1:]:
        row_profile = _row_value(row, profile_col)
        row_title = _row_value(row, title_col)
        if not row_profile or not row_title:
            continue
        if want and want != "all" and row_profile.lower() != want:
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
    profile_col, title_col = _identity_columns(headers)
    for row in values[1:]:
        if (_row_value(row, profile_col).lower() == profile.lower()
                and _row_value(row, title_col).lower() == title.lower()):
            return _row_to_item(section, headers, row)
    return None


def statuses_for(section: str) -> list[str]:
    return GAME_STATUSES if section == "games" else SHOW_STATUSES


# --------------------------------------------------------------------------- #
# Catalog search + creation (mirrors Game'N'Watch /newgame and /newshow)
# --------------------------------------------------------------------------- #

_STEAM_APP_RE = re.compile(r"(?:store\.steampowered\.com/app/)?(\d{2,})")
_YOUTUBE_PLAYLIST_RE = re.compile(r"(?:[?&]list=|^)([A-Za-z0-9_-]{10,})")


def _steam_lookup(app_id: str) -> dict[str, Any] | None:
    url = "https://store.steampowered.com/api/appdetails"
    with httpx.Client(timeout=15, headers={"User-Agent": "LuigiWeb/1.0"}) as client:
        response = client.get(url, params={"appids": app_id, "cc": "us", "l": "en"})
        response.raise_for_status()
        entry = response.json().get(str(app_id), {})
    if not entry.get("success"):
        return None
    data = entry.get("data") or {}
    multiplayer_ids = {1, 9, 36, 37, 38, 39}
    categories = data.get("categories") or []
    return {
        "source": "steam",
        "external_id": str(app_id),
        "title": data.get("name") or f"Steam app {app_id}",
        "cover_url": data.get("header_image") or "",
        "platform": "Steam",
        "release_date": (data.get("release_date") or {}).get("date") or "",
        "price": (data.get("price_overview") or {}).get("final_formatted") or "",
        "developers": ", ".join(data.get("developers") or []),
        "is_multiplayer": any(c.get("id") in multiplayer_ids for c in categories),
        "genre": ", ".join(g.get("description", "") for g in data.get("genres") or [] if g.get("description")),
    }


def _tvmaze_lookup(show_id: str) -> dict[str, Any] | None:
    with httpx.Client(timeout=15, headers={"User-Agent": "LuigiWeb/1.0"}) as client:
        response = client.get(
            f"https://api.tvmaze.com/shows/{show_id}",
            params={"embed": "episodes"},
        )
        response.raise_for_status()
        data = response.json()
    episodes = ((data.get("_embedded") or {}).get("episodes") or [])
    image = data.get("image") or {}
    network = data.get("network") or data.get("webChannel") or {}
    return {
        "source": "tvmaze",
        "external_id": str(show_id),
        "title": data.get("name") or f"TVMaze show {show_id}",
        "cover_url": image.get("original") or image.get("medium") or "",
        "genre": ", ".join(data.get("genres") or []),
        "total_episodes": len(episodes) or None,
        "platform": network.get("name") or "TV",
        "premiere_date": data.get("premiered") or "",
        "runtime": data.get("averageRuntime") or data.get("runtime") or "",
    }


def _anilist_request(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=15, headers={"User-Agent": "LuigiWeb/1.0"}) as client:
        response = client.post(
            "https://graphql.anilist.co",
            json={"query": query, "variables": variables},
        )
        response.raise_for_status()
        return response.json().get("data") or {}


def _anilist_lookup(media_id: str) -> dict[str, Any] | None:
    data = _anilist_request(
        """query ($id: Int) { Media(id: $id, type: ANIME) {
          id title { english romaji } episodes genres format seasonYear
          coverImage { extraLarge large } duration
        }}""",
        {"id": int(media_id)},
    ).get("Media")
    if not data:
        return None
    title = data.get("title") or {}
    cover = data.get("coverImage") or {}
    return {
        "source": "anilist",
        "external_id": str(media_id),
        "title": title.get("english") or title.get("romaji") or f"AniList {media_id}",
        "cover_url": cover.get("extraLarge") or cover.get("large") or "",
        "genre": ", ".join(data.get("genres") or []),
        "total_episodes": data.get("episodes"),
        "platform": "Anime",
        "premiere_date": str(data.get("seasonYear") or ""),
        "runtime": data.get("duration") or "",
    }


def _youtube_lookup(playlist_id: str) -> dict[str, Any] | None:
    api_key = os.environ.get("LUIGI_WEB_YOUTUBE_API_KEY", "").strip()
    if not api_key:
        return None
    with httpx.Client(timeout=15, headers={"User-Agent": "LuigiWeb/1.0"}) as client:
        response = client.get(
            "https://www.googleapis.com/youtube/v3/playlists",
            params={"part": "snippet,contentDetails", "id": playlist_id, "key": api_key},
        )
        response.raise_for_status()
        items = response.json().get("items") or []
    if not items:
        return None
    item = items[0]
    snippet = item.get("snippet") or {}
    thumbs = snippet.get("thumbnails") or {}
    thumb = thumbs.get("maxres") or thumbs.get("high") or thumbs.get("medium") or {}
    return {
        "source": "youtube",
        "external_id": playlist_id,
        "title": snippet.get("title") or f"YouTube playlist {playlist_id}",
        "cover_url": thumb.get("url") or "",
        "genre": "YouTube",
        "total_episodes": (item.get("contentDetails") or {}).get("itemCount"),
        "platform": "YouTube",
        "premiere_date": str(snippet.get("publishedAt") or "")[:10],
        "runtime": "",
    }


def search_catalog(section: str, query: str) -> list[dict[str, Any]]:
    """Search the same public sources used by the Game'N'Watch bot."""
    query = (query or "").strip()
    if not query:
        return []
    results: list[dict[str, Any]] = []
    if section == "games":
        direct = _STEAM_APP_RE.search(query)
        if direct and (query.isdigit() or "steampowered.com/app/" in query.lower()):
            item = _steam_lookup(direct.group(1))
            return [item] if item else []
        with httpx.Client(timeout=15, headers={"User-Agent": "LuigiWeb/1.0"}) as client:
            response = client.get(
                "https://store.steampowered.com/api/storesearch/",
                params={"term": query, "l": "english", "cc": "US"},
            )
            response.raise_for_status()
            for item in (response.json().get("items") or [])[:8]:
                results.append({
                    "source": "steam",
                    "external_id": str(item.get("id") or ""),
                    "title": item.get("name") or "Untitled",
                    "cover_url": item.get("tiny_image") or "",
                    "detail": "Steam",
                })
        return results

    playlist_match = _YOUTUBE_PLAYLIST_RE.search(query)
    if playlist_match and ("youtube.com" in query.lower() or "youtu.be" in query.lower()):
        playlist = _youtube_lookup(playlist_match.group(1))
        return [playlist] if playlist else []

    try:
        with httpx.Client(timeout=15, headers={"User-Agent": "LuigiWeb/1.0"}) as client:
            response = client.get("https://api.tvmaze.com/search/shows", params={"q": query})
            response.raise_for_status()
            for hit in response.json()[:5]:
                show = hit.get("show") or {}
                image = show.get("image") or {}
                results.append({
                    "source": "tvmaze",
                    "external_id": str(show.get("id") or ""),
                    "title": show.get("name") or "Untitled",
                    "cover_url": image.get("medium") or "",
                    "detail": f"TV · {show.get('premiered') or 'unknown year'}",
                })
    except Exception:  # noqa: BLE001 — other sources may still work
        pass
    try:
        anime = _anilist_request(
            """query ($search: String) { Page(page: 1, perPage: 5) {
              media(search: $search, type: ANIME) { id title { english romaji }
                seasonYear format episodes coverImage { large } }
            }}""",
            {"search": query},
        )
        for item in ((anime.get("Page") or {}).get("media") or []):
            title = item.get("title") or {}
            results.append({
                "source": "anilist",
                "external_id": str(item.get("id") or ""),
                "title": title.get("english") or title.get("romaji") or "Untitled",
                "cover_url": (item.get("coverImage") or {}).get("large") or "",
                "detail": f"Anime · {item.get('seasonYear') or 'unknown year'}",
            })
    except Exception:  # noqa: BLE001 — TVMaze/YouTube may still work
        pass
    youtube_key = os.environ.get("LUIGI_WEB_YOUTUBE_API_KEY", "").strip()
    if youtube_key:
        try:
            with httpx.Client(timeout=15, headers={"User-Agent": "LuigiWeb/1.0"}) as client:
                response = client.get(
                    "https://www.googleapis.com/youtube/v3/search",
                    params={
                        "part": "snippet", "type": "playlist", "maxResults": 3,
                        "q": query, "key": youtube_key,
                    },
                )
                response.raise_for_status()
                for item in response.json().get("items") or []:
                    playlist_id = (item.get("id") or {}).get("playlistId")
                    snippet = item.get("snippet") or {}
                    thumbs = snippet.get("thumbnails") or {}
                    if playlist_id:
                        results.append({
                            "source": "youtube",
                            "external_id": playlist_id,
                            "title": snippet.get("title") or "Untitled playlist",
                            "cover_url": (thumbs.get("high") or thumbs.get("medium") or {}).get("url") or "",
                            "detail": "YouTube playlist",
                        })
        except Exception:  # noqa: BLE001 — optional source
            pass
    return results[:10]


def catalog_lookup(section: str, source: str, external_id: str) -> dict[str, Any] | None:
    if section == "games" and source == "steam":
        return _steam_lookup(external_id)
    if section == "shows" and source == "tvmaze":
        return _tvmaze_lookup(external_id)
    if section == "shows" and source == "anilist":
        return _anilist_lookup(external_id)
    if section == "shows" and source == "youtube":
        return _youtube_lookup(external_id)
    return None


def add_catalog_item(
    section: str,
    profile: str,
    metadata: dict[str, Any],
    *,
    status: str = "backlog",
    priority: int = 3,
) -> tuple[bool, str]:
    """Insert a metadata-backed item into the live Games/Shows worksheet."""
    if status not in statuses_for(section):
        return False, f"invalid status: {status}"
    title = str(metadata.get("title") or "").strip()
    profile = (profile or "").strip()
    if not profile or not title:
        return False, "profile and title are required"
    values = _all_values(section, force=True)
    if not values:
        return False, f"{_tab(section)} sheet has no header row"
    headers = values[0]
    profile_col, title_col = _identity_columns(headers)
    target_row: int | None = None
    for row_number, row in enumerate(values[1:], start=2):
        row_profile = _row_value(row, profile_col)
        row_title = _row_value(row, title_col)
        if row_profile.lower() == profile.lower() and row_title.lower() == title.lower():
            return False, f"{title} already exists for {profile}"
        if row_profile.lower() == profile.lower() and not row_title and target_row is None:
            target_row = row_number
    target_row = target_row or (len(values) + 1)
    row = ["" for _ in headers]
    idx = _header_index(headers)

    def put(header: str, value: Any) -> None:
        column = idx.get(header)
        if column is not None and value not in (None, ""):
            row[column] = str(value)

    put("Profile", profile)
    put("Title", title)
    put("Status", status)
    put("Priority", max(1, min(int(priority), 5)))
    put("Date Added", date.today().isoformat())
    put("Cover URL", metadata.get("cover_url"))
    put("External ID", metadata.get("external_id"))
    put("Source", metadata.get("source"))
    put("Platform", metadata.get("platform"))
    put("Genre", metadata.get("genre"))
    if section == "games":
        put("Release Date", metadata.get("release_date"))
        put("Price", metadata.get("price"))
        put("Developers", metadata.get("developers"))
        put("Is Multiplayer", "TRUE" if metadata.get("is_multiplayer") else "FALSE")
    else:
        put("Total Episodes", metadata.get("total_episodes"))
        put("Premiere Date", metadata.get("premiere_date"))
        put("Runtime", metadata.get("runtime"))
        put("Current Season", 1)
        put("Current Episode", 0)
    start = _a1(target_row, 1)
    end = _a1(target_row, len(headers))
    _ws(section).update([row], f"{start}:{end}")
    _invalidate(section)
    return True, title


def add_manual_item(
    section: str, profile: str, title: str, *, status: str = "backlog", priority: int = 3,
) -> tuple[bool, str]:
    return add_catalog_item(
        section,
        profile,
        {"title": title, "source": "manual"},
        status=status,
        priority=priority,
    )


def steam_stats(app_id: str) -> dict[str, Any]:
    """Live playtime + achievement progress for the configured Steam user."""
    api_key = os.environ.get("LUIGI_WEB_STEAM_API_KEY", "").strip()
    steam_id = os.environ.get("LUIGI_WEB_STEAM_ID", "").strip()
    if not api_key or not steam_id:
        raise RuntimeError("Set LUIGI_WEB_STEAM_API_KEY and LUIGI_WEB_STEAM_ID in Admin")
    with httpx.Client(timeout=20, headers={"User-Agent": "LuigiWeb/1.0"}) as client:
        owned = client.get(
            "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
            params={
                "key": api_key,
                "steamid": steam_id,
                "include_appinfo": 1,
                "appids_filter[0]": int(app_id),
            },
        )
        owned.raise_for_status()
        games = (owned.json().get("response") or {}).get("games") or []
        game = games[0] if games else {}
        achievements_response = client.get(
            "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/",
            params={"key": api_key, "steamid": steam_id, "appid": int(app_id), "l": "english"},
        )
        achievements_response.raise_for_status()
        stats = achievements_response.json().get("playerstats") or {}
    achievements = stats.get("achievements") or []
    unlocked = sum(1 for achievement in achievements if achievement.get("achieved"))
    total = len(achievements)
    locked = [
        {
            "name": achievement.get("name") or achievement.get("apiname") or "Achievement",
            "description": achievement.get("description") or "",
        }
        for achievement in achievements
        if not achievement.get("achieved")
    ]
    return {
        "app_id": str(app_id),
        "name": game.get("name") or stats.get("gameName") or f"Steam app {app_id}",
        "hours_played": round(float(game.get("playtime_forever") or 0) / 60, 1),
        "hours_recent": round(float(game.get("playtime_2weeks") or 0) / 60, 1),
        "achievements_unlocked": unlocked,
        "achievements_total": total,
        "achievement_percent": round((unlocked / total) * 100) if total else None,
        "complete": bool(total and unlocked == total),
        "next_achievements": locked[:5],
    }


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def _find_row_idx(section: str, profile: str, title: str) -> tuple[int | None, list[str]]:
    """1-based row index of (profile, title) + the header row. Forces a fresh
    read so we never write to a stale row position."""
    values = _all_values(section, force=True)
    headers = values[0] if values else []
    profile_col, title_col = _identity_columns(headers)
    for i, row in enumerate(values[1:], start=2):
        if (_row_value(row, profile_col).lower() == profile.lower()
                and _row_value(row, title_col).lower() == title.lower()):
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
    hidx = {h: i + 1 for h, i in _header_index(headers).items()}
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
        hidx = {h: i + 1 for h, i in _header_index(headers).items()}
        col = hidx.get("Times Picked")
        if not col:
            return
        ws = _ws(section)
        current = _to_int(ws.cell(row_idx, col).value, 0) or 0
        ws.update_cell(row_idx, col, current + 1)
        _invalidate(section)
    except Exception:  # noqa: BLE001
        pass
