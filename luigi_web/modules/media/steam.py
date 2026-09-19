"""Explicit Steam refresh, process-local display cache, and bound save validation."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import secrets
from threading import Lock
from time import monotonic
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None


_OWNED_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
_ACHIEVEMENTS_URL = "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/"
_UNAVAILABLE = "Steam statistics are unavailable."
_INVALID_SNAPSHOT = "Steam snapshot is unavailable or expired. Refresh before saving."
_CACHE_LIMIT = 256
_DISPLAY_TTL = 300.0
_SAVE_TTL = 600.0


@dataclass(frozen=True)
class _Entry:
    snapshot: dict[str, Any]
    snapshot_id: str
    fetched_at: str
    fetched_monotonic: float


_cache: OrderedDict[tuple[str, str, str, bytes], _Entry] = OrderedDict()
_lock = Lock()


def _app_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Steam app ID must be a positive numeric ID.")
    text = str(value).strip()
    if not text.isascii() or not text.isdecimal() or len(text) > 10:
        raise ValueError("Steam app ID must be a positive numeric ID.")
    number = int(text)
    if not 0 < number <= 4294967295:
        raise ValueError("Steam app ID must be a positive numeric ID.")
    return str(number)


def _configuration() -> tuple[str, str] | None:
    api_key = os.environ.get("LUIGI_WEB_STEAM_API_KEY", "").strip()
    steam_id = os.environ.get("LUIGI_WEB_STEAM_ID", "").strip()
    return (api_key, steam_id) if api_key and steam_id else None


def _hours(minutes: Any) -> float | None:
    if type(minutes) not in (int, float):
        return None
    try:
        if minutes < 0 or not math.isfinite(minutes):
            return None
        return round(minutes / 60.0, 1)
    except (OverflowError, ValueError):
        return None


def _text(value: Any, fallback: str = "", limit: int = 256) -> str:
    return value[:limit] if isinstance(value, str) and value.strip() else fallback


def _json(client: Any, url: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _playtime(snapshot: dict[str, Any], payload: dict[str, Any]) -> None:
    response = payload.get("response")
    games = response.get("games") if isinstance(response, dict) else None
    if not isinstance(games, list):
        return
    matches = []
    for game in games:
        if not isinstance(game, dict):
            continue
        try:
            if _app_id(game.get("appid")) == snapshot["app_id"]:
                matches.append(game)
        except ValueError:
            continue
    if len(matches) != 1:
        return
    game = matches[0]
    snapshot.update(
        name=_text(game.get("name"), snapshot["name"]),
        hours_played=_hours(game.get("playtime_forever")),
        hours_recent=_hours(game.get("playtime_2weeks")),
    )


def _achievements(snapshot: dict[str, Any], payload: dict[str, Any]) -> None:
    stats = payload.get("playerstats")
    if not isinstance(stats, dict) or stats.get("success") is not True:
        return
    achievements = stats.get("achievements")
    if not isinstance(achievements, list):
        return
    if any(
        not isinstance(entry, dict)
        or type(entry.get("achieved")) is not int
        or entry["achieved"] not in (0, 1)
        for entry in achievements
    ):
        return
    total = len(achievements)
    unlocked = sum(entry["achieved"] for entry in achievements)
    locked = [entry for entry in achievements if entry["achieved"] == 0]
    snapshot.update(
        achievements_unlocked=unlocked,
        achievements_total=total,
        achievement_percent=round(unlocked / total * 100) if total else None,
        complete=bool(total and unlocked == total),
        next_achievements=[
            {
                "name": _text(entry.get("name"), _text(entry.get("apiname"), "Achievement")),
                "description": _text(entry.get("description"), limit=1024),
            }
            for entry in locked[:5]
        ],
    )
    if snapshot["name"] == f"Steam app {snapshot['app_id']}":
        snapshot["name"] = _text(stats.get("gameName"), snapshot["name"])


def _fetch(app_id: str | int) -> dict[str, Any]:
    """Fetch both independent parts; absent/private/malformed parts stay unknown."""
    app_id = _app_id(app_id)
    configuration = _configuration()
    if configuration is None or httpx is None:
        raise RuntimeError(_UNAVAILABLE)
    api_key, steam_id = configuration
    snapshot: dict[str, Any] = {
        "app_id": app_id,
        "name": f"Steam app {app_id}",
        "hours_played": None,
        "hours_recent": None,
        "achievements_unlocked": None,
        "achievements_total": None,
        "achievement_percent": None,
        "complete": False,
        "next_achievements": [],
    }

    class CredentialTransport(httpx.HTTPTransport):
        """Keep credentials off request URLs seen by HTTPX's request logger."""

        def handle_request(self, request):
            public_url = request.url
            request.url = public_url.copy_merge_params({"key": api_key, "steamid": steam_id})
            try:
                return super().handle_request(request)
            finally:
                request.url = public_url

    try:
        with httpx.Client(
            timeout=10.0,
            follow_redirects=False,
            trust_env=False,
            transport=CredentialTransport(retries=0, trust_env=False),
            headers={"User-Agent": "LuigiWeb/1.0"},
        ) as client:
            _playtime(snapshot, _json(client, _OWNED_URL, {
                "include_appinfo": 1,
                "include_played_free_games": 1,
                "appids_filter[0]": int(app_id),
            }))
            _achievements(snapshot, _json(client, _ACHIEVEMENTS_URL, {
                "appid": int(app_id), "l": "english",
            }))
    except Exception:
        pass
    snapshot["playtime_unavailable"] = snapshot["hours_played"] is None
    snapshot["achievements_unavailable"] = snapshot["achievements_total"] is None
    if snapshot["playtime_unavailable"] and snapshot["achievements_unavailable"]:
        raise RuntimeError(_UNAVAILABLE) from None
    return snapshot


def _binding(profile: str, title: str, app_id: str | int) -> tuple[str, str, str, bytes] | None:
    app_id = _app_id(app_id)
    if not isinstance(profile, str) or not profile.strip() or not isinstance(title, str) or not title.strip():
        raise ValueError("A profile and title are required.")
    configuration = _configuration()
    if configuration is None:
        return None
    identity = hashlib.sha256(json.dumps(configuration).encode("utf-8")).digest()
    return profile.strip(), title.strip(), app_id, identity


def _envelope(entry: _Entry | None) -> dict[str, Any]:
    if entry is None:
        return {"snapshot": None, "snapshot_id": None, "fetched_at": None, "stale": True}
    age = monotonic() - entry.fetched_monotonic
    return {
        "snapshot": deepcopy(entry.snapshot),
        "snapshot_id": entry.snapshot_id,
        "fetched_at": entry.fetched_at,
        "stale": age < 0 or age >= _DISPLAY_TTL,
    }


def get_snapshot(profile: str, title: str, app_id: str | int) -> dict[str, Any]:
    """Read only cached data; misses (including missing credentials) never fetch.

    Stale snapshots remain displayable until replaced or evicted. Reading does
    not renew their age or their save eligibility.
    """
    binding = _binding(profile, title, app_id)
    with _lock:
        entry = _cache.get(binding) if binding is not None else None
        if binding is not None and entry is not None:
            _cache.move_to_end(binding)
        return _envelope(entry)


def refresh(profile: str, title: str, app_id: str | int) -> dict[str, Any]:
    """Explicitly fetch and replace this binding's snapshot with a new opaque ID."""
    binding = _binding(profile, title, app_id)
    if binding is None:
        raise RuntimeError(_UNAVAILABLE)
    try:
        snapshot = _fetch(binding[2])
    except Exception:
        raise RuntimeError(_UNAVAILABLE) from None
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("app_id") != binding[2]
        or _binding(profile, title, app_id) != binding
    ):
        raise RuntimeError(_UNAVAILABLE)
    entry = _Entry(
        snapshot=deepcopy(snapshot),
        snapshot_id=secrets.token_urlsafe(32),
        fetched_at=datetime.now(timezone.utc).isoformat(),
        fetched_monotonic=monotonic(),
    )
    with _lock:
        _cache[binding] = entry
        _cache.move_to_end(binding)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)
        return _envelope(entry)


def saved_hours(profile: str, title: str, app_id: str | int, snapshot_id: str) -> float:
    """Return validated cached hours for the caller to persist; never fetch/write."""
    binding = _binding(profile, title, app_id)
    with _lock:
        entry = _cache.get(binding) if binding is not None else None
        if (
            binding is None
            or entry is None
            or not isinstance(snapshot_id, str)
            or not snapshot_id.isascii()
            or not secrets.compare_digest(entry.snapshot_id, snapshot_id)
            or not 0 <= monotonic() - entry.fetched_monotonic < _SAVE_TTL
            or entry.snapshot.get("app_id") != binding[2]
            or entry.snapshot.get("playtime_unavailable")
        ):
            raise RuntimeError(_INVALID_SNAPSHOT)
        hours = entry.snapshot.get("hours_played")
        if hours is None or type(hours) not in (int, float):
            raise RuntimeError(_INVALID_SNAPSHOT)
        try:
            value = float(hours)
        except (OverflowError, ValueError):
            raise RuntimeError(_INVALID_SNAPSHOT) from None
        if not math.isfinite(value) or value < 0:
            raise RuntimeError(_INVALID_SNAPSHOT)
        return value