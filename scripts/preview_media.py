"""Disposable, loopback-only media preview with synthetic records."""
from __future__ import annotations

import argparse
from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
import copy
from datetime import date, timedelta
from functools import lru_cache
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import struct
import sys
import tempfile
from threading import RLock
from unittest.mock import patch
from urllib.parse import urlsplit
import zlib

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_HOST_IMPORTED = False
_CONTEXT_ACTIVE = False
_ORIGIN = ContextVar("media_preview_origin", default="http://127.0.0.1:58107")
_READ_PATHS = re.compile(
    r"/(?:games|shows|media/insights|media/(?:games|shows)/(?:data|detail)|media/games/steam"
    r"|gnw/(?:games|shows)/new|preview-media/covers/(?:[1-9]|1[0-9]|2[0-4]|31|32)\.png)"
)
_WRITE_PATHS = re.compile(
    r"/(?:media/(?:games|shows)/(?:change|refresh|episode|runs|undo|pick)"
    r"|media/games/steam/(?:refresh|save)|gnw/(?:games|shows)/(?:search|add))"
)


class FakeWorksheet:
    """A bounded, header-addressed in-memory substitute for a Sheets worksheet."""

    def __init__(self, headers, records=()):
        self.rows = [list(headers)] + [
            [str(record.get(header, "")) for header in headers] for record in records
        ]
        self.lock = RLock()

    def get_all_values(self):
        with self.lock:
            rows = copy.deepcopy(self.rows)
        if "Cover URL" in rows[0]:
            column = rows[0].index("Cover URL")
            for row in rows[1:]:
                if len(row) > column and row[column].startswith("/preview-media/covers/"):
                    row[column] = _ORIGIN.get() + row[column]
        return rows

    @staticmethod
    def _cell(reference):
        match = re.fullmatch(r"\$?([A-Za-z]+)\$?([1-9][0-9]*)", reference)
        if not match:
            raise ValueError("Invalid preview cell reference")
        column = 0
        for letter in match[1].upper():
            column = column * 26 + ord(letter) - ord("A") + 1
        row = int(match[2])
        if row > 10000 or column > 1024:
            raise ValueError("Preview worksheet limit exceeded")
        return row - 1, column - 1

    @classmethod
    def _apply(cls, rows, entry):
        reference = entry["range"].rsplit("!", 1)[-1]
        bounds = reference.split(":")
        if len(bounds) > 2:
            raise ValueError("Invalid preview range")
        start_row, start_column = cls._cell(bounds[0])
        values = entry["values"]
        if not isinstance(values, list) or any(not isinstance(row, list) for row in values):
            raise ValueError("Invalid preview values")
        end_row, end_column = cls._cell(bounds[-1])
        if end_row < start_row or end_column < start_column:
            raise ValueError("Reversed preview range")
        width = max((len(row) for row in values), default=0)
        if start_row + len(values) > 10000 or start_column + width > 1024:
            raise ValueError("Preview worksheet limit exceeded")
        if len(bounds) == 2 and (len(values) > end_row - start_row + 1 or width > end_column - start_column + 1):
            raise ValueError("Preview values exceed range")
        for row_offset, source in enumerate(values):
            target_row = start_row + row_offset
            while len(rows) <= target_row:
                rows.append([])
            target = rows[target_row]
            target.extend([""] * max(0, start_column + len(source) - len(target)))
            for column_offset, value in enumerate(source):
                target[start_column + column_offset] = "" if value is None else str(value)

    def batch_update(self, data, **kwargs):
        with self.lock:
            rows = copy.deepcopy(self.rows)
            for entry in data:
                self._apply(rows, entry)
            self.rows = rows

    def update(self, range_name=None, values=None, **kwargs):
        if isinstance(range_name, list):
            range_name, values = values, range_name
        self.batch_update([{"range": range_name, "values": values}], **kwargs)


class FakeSheet:
    def __init__(self, worksheets):
        self.worksheets = worksheets

    def worksheet(self, name):
        section = str(name).casefold()
        if section not in self.worksheets:
            raise ValueError("Unknown synthetic worksheet")
        return self.worksheets[section]


def _blocked(*args, **kwargs):
    raise RuntimeError("External services and shared storage are disabled in media preview")


def _records(section):
    today = date.today()
    titles = {
        "games": (
            "Prism Circuit", "Signal Atlas", "Glass Meridian", "Copper Constellation",
            "Tidal Geometry", "Paper Satellites", "Echo Foundry", "Mosaic Engines",
            "Velvet Orbit", "Chromatic Passage", "Parallel Gardens", "Neon Archive",
        ),
        "shows": (
            "The Lantern Index", "Station Marigold", "An Unfinished Map", "The Quiet Signal",
            "Paper Horizons", "The Seventh Frequency", "Letters from Tomorrow", "A Map of Rain",
            "The Painted Current", "Rooms of Light", "The Last Diagram", "City of Kites",
        ),
    }
    statuses = {
        "games": ("playing", "backlog", "paused", "completed", "achievements", "dropped"),
        "shows": ("watching", "backlog", "on_hold", "completed", "dropped", "watching"),
    }
    for index, title in enumerate(titles[section]):
        status = statuses[section][index % 6]
        cover_id = index + (1 if section == "games" else 13)
        record = {
            "Profile": "Example profile" if index < 9 else "Second example profile",
            "Title": title, "Status": status, "Priority": index % 5 + 1,
            "Rating": index % 5 + 5 if index % 3 else "", "Notes": "Synthetic library entry.",
            "Platform": ("Desktop" if index % 2 else "Handheld") if section == "games" else "Example channel",
            "Genre": ("Puzzle", "Adventure", "Strategy")[index % 3] if section == "games" else ("Drama", "Mystery", "Animation")[index % 3],
            "Tags": ("short sessions, favorites", "weekend", "shared pick")[index % 3],
            "Date Added": (today - timedelta(days=20 + index * 9)).isoformat(),
            "Date Started": (today - timedelta(days=10 + index)).isoformat() if status != "backlog" else "",
            "Date Completed": (today - timedelta(days=2)).isoformat() if status in {"completed", "achievements"} else "",
            "Cover URL": f"/preview-media/covers/{cover_id}.png",
            "Source": "steam" if section == "games" and index == 0 else "manual",
            "External ID": "9000042" if section == "games" and index == 0 else "",
            "Times Picked": index % 4,
        }
        if section == "games":
            record.update({"Hours Played": 6 + index * 3, "Last Played": (today - timedelta(days=index)).isoformat()})
        else:
            record.update({"Current Season": 1 + index % 3, "Current Episode": 12 if status == "completed" else index % 8 + 1,
                           "Total Episodes": 12, "Runtime": 24, "Last Watched": (today - timedelta(days=index)).isoformat()})
        yield record


def _catalog(section):
    if section not in {"games", "shows"}:
        raise ValueError("Unknown synthetic section")
    return {
        "title": "Vector Orchard" if section == "games" else "The Folded Sky",
        "source": "steam" if section == "games" else "tvmaze",
        "external_id": "9000043" if section == "games" else "9000044",
        "cover_url": f"{_ORIGIN.get()}/preview-media/covers/{31 if section == 'games' else 32}.png",
        "genre": "Puzzle" if section == "games" else "Mystery",
        "platform": "Desktop" if section == "games" else "Example channel",
        "total_episodes": 12,
    }


def _catalog_lookup(section, source, external_id):
    result = _catalog(section)
    if source == result["source"] and str(external_id) == result["external_id"]:
        return result
    return None


def _steam_snapshot(app_id):
    if app_id not in {"9000042", "9000043"}:
        raise ValueError("Unknown synthetic Steam title")
    return {
        "app_id": app_id, "name": "Prism Circuit" if app_id == "9000042" else "Vector Orchard",
        "hours_played": 18.5, "hours_recent": 2.5, "achievements_unlocked": 7,
        "achievements_total": 12, "achievement_percent": 58, "complete": False,
        "next_achievements": [{"name": "Complete the pattern", "description": "Solve the final synthetic circuit."}],
        "playtime_unavailable": False, "achievements_unavailable": False,
    }


@lru_cache(maxsize=32)
def cover_png(cover_id):
    if cover_id not in {*range(1, 25), 31, 32}:
        raise ValueError("Unknown synthetic cover")
    width, height = 240, 320
    palettes = (
        ((18, 77, 72), (87, 207, 178), (255, 193, 79)),
        ((103, 36, 57), (242, 109, 115), (249, 217, 156)),
        ((36, 55, 94), (89, 170, 207), (250, 171, 83)),
        ((68, 63, 40), (193, 213, 110), (240, 116, 71)),
    )
    background, foreground, accent = palettes[cover_id % len(palettes)]
    pixels = bytearray()
    for vertical in range(height):
        pixels.append(0)
        for horizontal in range(width):
            color = background
            diagonal = (horizontal + vertical // 2 + cover_id * 11) % 100
            if 24 < horizontal < 216 and 40 < vertical < 254 and diagonal < 54:
                color = foreground
            if abs(horizontal - 120) + abs(vertical - 140) < 46:
                color = accent
            if 30 < horizontal < 175 and 274 < vertical < 282:
                color = accent
            pixels.extend(color)

    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(pixels))) + chunk(b"IEND", b"")


def configure_security(app, internal_token, session_token):
    from luigi_web import auth

    @app.middleware("http")
    async def preview_security(request: Request, call_next):
        def deny(status, message):
            return JSONResponse({"detail": message}, status_code=status, headers={"Cache-Control": "no-store"})

        try:
            port = request.url.port or 80
            hostname = request.url.hostname
        except ValueError:
            return deny(403, "Loopback only")
        testing = request.client is not None and request.client.host == "testclient"
        hosts = {"127.0.0.1", "localhost", "::1"} | ({"testserver"} if testing else set())
        if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"} or hostname not in hosts:
            return deny(403, "Loopback only")
        path = request.url.path
        bootstrap = request.method in {"GET", "HEAD"}
        static = path.startswith(("/static/", "/module-assets/media/"))
        if bootstrap:
            if path not in {"/", "/login"} and not static and not _READ_PATHS.fullmatch(path):
                return deny(403, "Unavailable in media preview")
        elif request.method != "POST" or not _WRITE_PATHS.fullmatch(path):
            return deny(403, "Unavailable in media preview")
        session_name = f"luigi_media_preview_session_{port}"
        candidate = request.cookies.get(session_name, "")
        valid_session = secrets.compare_digest(candidate.encode(), session_token.encode())
        if not bootstrap:
            if not valid_session:
                return deny(401, "Preview session required")
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            if origin is not None and origin != str(request.base_url).rstrip("/"):
                return deny(403, "Same-origin requests only")
            if referer is not None:
                try:
                    source = urlsplit(referer)
                    if source.username or source.password or (source.scheme, source.netloc) != (request.url.scheme, request.url.netloc):
                        return deny(403, "Same-origin requests only")
                except ValueError:
                    return deny(403, "Same-origin requests only")
            csrf_cookie = request.cookies.get(auth.CSRF_COOKIE_NAME)
            csrf_header = request.headers.get("x-csrf-token")
            if not (csrf_cookie and csrf_cookie.isascii() and csrf_header and csrf_header.isascii()
                    and auth.csrf_matches(csrf_cookie, csrf_header)):
                return deny(403, "CSRF validation failed")
        forwarded = SimpleCookie()
        forwarded[auth.COOKIE_NAME] = internal_token
        request.scope["headers"] = [
            (name, value) for name, value in request.scope["headers"]
            if name.lower() not in {b"cookie", b"authorization"}
        ] + [(b"cookie", forwarded.output(header="", sep=";").strip().encode("ascii"))]
        origin_context = _ORIGIN.set(str(request.base_url).rstrip("/"))
        try:
            response = RedirectResponse("/games", status_code=303) if path in {"/", "/login"} else await call_next(request)
        finally:
            _ORIGIN.reset(origin_context)
        if bootstrap and not valid_session:
            response.set_cookie(session_name, session_token, httponly=True, samesite="strict")
        if bootstrap and not request.cookies.get(auth.CSRF_COOKIE_NAME):
            response.set_cookie(auth.CSRF_COOKIE_NAME, auth.csrf_token(), samesite="strict")
        response.headers.update({
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        })
        return response


@contextmanager
def preview_context():
    global _HOST_IMPORTED, _CONTEXT_ACTIVE
    if _CONTEXT_ACTIVE or ("luigi_web.application" in sys.modules and not _HOST_IMPORTED):
        raise RuntimeError("Run media preview in a fresh process without an imported host")
    with tempfile.TemporaryDirectory(prefix="luigi-media-preview-") as temporary, ExitStack() as stack:
        directory = Path(temporary)
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("LUIGI_WEB_")}
        internal_token, session_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        environment.update({
            "LUIGI_WEB_UI_TOKEN": internal_token, "LUIGI_WEB_DATA_DIR": str(directory),
            "LUIGI_WEB_MEDIA_DB": str(directory / "media.sqlite3"),
            "LUIGI_WEB_MODULES_FILE": str(directory / "absent-modules.json"),
            "LUIGI_WEB_MODULES": "media", "LUIGI_WEB_LLM_PROVIDER": "disabled",
            "LUIGI_WEB_SECURE_COOKIES": "0", "LUIGI_WEB_TIMEZONE": "UTC",
        })
        stack.enter_context(patch.dict(os.environ, environment, clear=True))
        stack.enter_context(patch.object(socket, "create_connection", _blocked))
        stack.enter_context(patch.object(socket, "getaddrinfo", _blocked))
        original_connect = socket.socket.connect
        socketpair_code = getattr(socket.socketpair, "__code__", None)

        def guarded_connect(connection, address):
            if (socketpair_code is not None and sys._getframe(1).f_code is socketpair_code
                    and isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}):
                return original_connect(connection, address)
            return _blocked()

        stack.enter_context(patch.object(socket.socket, "connect", guarded_connect))
        stack.enter_context(patch.object(socket.socket, "connect_ex", _blocked))
        import sqlalchemy

        stack.enter_context(patch.object(sqlalchemy, "create_engine", _blocked))
        connect = sqlite3.connect

        def temporary_sqlite(database, *args, **kwargs):
            if not Path(database).resolve().is_relative_to(directory.resolve()) or kwargs.get("uri"):
                return _blocked()
            return connect(database, *args, **kwargs)

        stack.enter_context(patch.object(sqlite3, "connect", temporary_sqlite))
        from luigi_web.modules.tasks import repository

        engine_guard = stack.enter_context(patch.object(repository, "get_engine", side_effect=_blocked))
        from luigi_web import application
        from luigi_web.core.module_registry import ModuleRegistry, mount_modules
        from luigi_web.modules.media import service, steam, workspace
        from luigi_web.modules.media.manifest import module
        from luigi_web.paths import STATIC_DIR

        _HOST_IMPORTED = True
        _CONTEXT_ACTIVE = True
        worksheets = {section: FakeWorksheet(headers, _records(section)) for section, headers in (
            ("games", service.GAME_HEADERS), ("shows", service.SHOW_HEADERS),
        )}
        sheet = FakeSheet(worksheets)
        stack.enter_context(patch.object(service, "_cache", {}))
        stack.enter_context(patch.object(service, "_CACHE_TTL", 0))
        service._cache.clear()
        stack.enter_context(patch.object(service, "_client", None))
        stack.enter_context(patch.object(service, "_sheet", sheet))
        stack.enter_context(patch.object(service, "_ws", sheet.worksheet))
        stack.enter_context(patch.object(service, "_get_sheet", lambda: sheet))
        stack.enter_context(patch.object(service, "_load_credentials", _blocked))
        stack.enter_context(patch.object(service, "is_enabled", lambda: True))
        stack.enter_context(patch.object(service, "disabled_reason", lambda: None))
        stack.enter_context(patch.object(service, "search_catalog", lambda section, query: [_catalog(section)]))
        stack.enter_context(patch.object(service, "catalog_lookup", _catalog_lookup))
        stack.enter_context(patch.object(service, "steam_stats", _steam_snapshot))
        stack.enter_context(patch.object(steam, "_configuration", lambda: ("synthetic-preview", "synthetic-profile")))
        stack.enter_context(patch.object(steam, "_fetch", _steam_snapshot))
        stack.enter_context(patch.object(steam, "_cache", OrderedDict()))
        stack.enter_context(patch.object(workspace, "_undo", OrderedDict()))
        stack.enter_context(patch.object(workspace, "_picks", OrderedDict()))
        try:
            app = FastAPI(title="Synthetic media preview", docs_url=None, redoc_url=None, openapi_url=None)
            app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
            mount_modules(app, ModuleRegistry([module], "media"))

            @app.get("/preview-media/covers/{cover_id}.png")
            def cover(cover_id: int):
                try:
                    return Response(cover_png(cover_id), media_type="image/png")
                except ValueError:
                    return Response(status_code=404)

            configure_security(app, internal_token, session_token)
            app.state.preview_directory = directory
            app.state.preview_worksheets = worksheets
            app.state.preview_engine_guard = engine_guard
            for section, title in (("games", "Prism Circuit"), ("shows", "The Lantern Index")):
                workspace.change(section, "Example profile", title, {"status": "completed"}, None)
                workspace.change(section, "Example profile", title, {}, None, kind="replay")
            workspace.change("games", "Example profile", "Prism Circuit", {"hours_played": 8}, None)
            workspace.change("shows", "Example profile", "The Lantern Index", {}, None, kind="episode")
            workspace._undo.clear()
            yield app
        finally:
            service._cache.clear()
            steam._cache.clear()
            workspace._undo.clear()
            workspace._picks.clear()
            cover_png.cache_clear()
            _CONTEXT_ACTIVE = False


def smoke_check(app):
    from fastapi.testclient import TestClient
    from luigi_web import auth

    counts = {"endpoints": 0, "mutations": 0}
    client = TestClient(app)

    def checked(response):
        if response.status_code != 200:
            raise RuntimeError(f"Synthetic media check failed ({response.status_code})")
        return response

    def get(path, **kwargs):
        response = checked(client.get(path, **kwargs))
        counts["endpoints"] += 1
        return response

    def post(path, data):
        response = checked(client.post(path, data=data, headers={
            "x-csrf-token": client.cookies[auth.CSRF_COOKIE_NAME], "origin": "http://testserver",
            "referer": "http://testserver/games",
        }))
        counts["mutations"] += 1
        return response.json()

    def expect(condition):
        if not condition:
            raise RuntimeError("Synthetic media round trip failed")

    try:
        for path in ("/games", "/shows", "/media/insights", "/preview-media/covers/1.png"):
            get(path)
        games = get("/media/games/data").json()["items"]
        shows = get("/media/shows/data").json()["items"]
        game = next(item for item in games if item["title"] == "Prism Circuit")
        show = next(item for item in shows if item["title"] == "The Lantern Index")
        identities = {"games": {key: game[key] for key in ("profile", "title")},
                      "shows": {key: show[key] for key in ("profile", "title")}}
        for section in ("games", "shows"):
            detail = get(f"/media/{section}/detail", params=identities[section]).json()
            expect(bool(detail["activity"]) and bool(detail["runs"]))
        expect(get("/media/games/steam", params=identities["games"]).json()["snapshot"] is None)
        changed = post("/media/games/change", {**identities["games"], "expected_version": game["version"], "fields": json.dumps({"rating": 9})})
        expect(changed["item"]["rating"] == 9 and changed["undo_token"])
        restored = post("/media/games/undo", {"token": changed["undo_token"]})["item"]
        expect(restored["rating"] == game["rating"])
        advanced = post("/media/shows/episode", {**identities["shows"], "expected_version": show["version"]})
        expect(advanced["item"]["current_episode"] == show["current_episode"] + 1)
        expect(post("/media/shows/undo", {"token": advanced["undo_token"]})["item"]["current_episode"] == show["current_episode"])
        finished = next(item for item in shows if item["status"] == "completed")
        replay = post("/media/shows/runs", {"profile": finished["profile"], "title": finished["title"], "expected_version": finished["version"]})
        expect(replay["item"]["status"] == "watching" and replay["item"]["current_episode"] == 0)
        expect(post("/media/shows/undo", {"token": replay["undo_token"]})["item"]["status"] == "completed")
        for section in ("games", "shows"):
            expect(len(post(f"/media/{section}/refresh", {})["items"]) == 12)
        picked = post("/media/games/pick", {"keys": json.dumps([game["key"]])})
        expect(picked["item"]["title"] == game["title"])
        snapshot = post("/media/games/steam/refresh", identities["games"])
        expect(snapshot["snapshot"]["hours_played"] == 18.5)
        saved = post("/media/games/steam/save", {**identities["games"], "expected_version": restored["version"], "snapshot_id": snapshot["snapshot_id"]})
        expect(saved["item"]["hours_played"] == "18.5")
        expect(post("/media/games/undo", {"token": saved["undo_token"]})["item"]["hours_played"] == game["hours_played"])
        expect(app.state.preview_engine_guard.call_count == 0)
        return counts
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=58107)
    parser.add_argument("--check", action="store_true", help="Run offline HTTP and mutation checks without startup or a server")
    arguments = parser.parse_args()
    if not 0 <= arguments.port <= 65535:
        parser.error("port must be between 0 and 65535")
    with preview_context() as app:
        if arguments.check:
            counts = smoke_check(app)
            print(f"Validated {counts['endpoints']} synthetic endpoints and {counts['mutations']} mutation round trips; no shared engine or external services.")
            return
        import uvicorn

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", arguments.port))
            port = listener.getsockname()[1]
            print(f"Synthetic media preview: http://127.0.0.1:{port}/games", flush=True)
            uvicorn.Server(uvicorn.Config(
                app, host="127.0.0.1", port=port, lifespan="off", access_log=False,
                log_level="critical", proxy_headers=False,
            )).run(sockets=[listener])


if __name__ == "__main__":
    main()