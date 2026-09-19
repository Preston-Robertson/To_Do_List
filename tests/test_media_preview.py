"""Offline preview contracts; never load deployment configuration or data."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from functools import wraps
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import zlib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.preview_media import FakeWorksheet, cover_png, preview_context, smoke_check

_ISOLATED = False


def isolated(method):
    @wraps(method)
    def run(self):
        if _ISOLATED:
            return method(self)
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--isolated", f"{type(self).__name__}.{method.__name__}"],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    return run


@contextmanager
def preview_client(base_url="http://testserver"):
    from fastapi.testclient import TestClient

    with preview_context() as app:
        client = TestClient(app, base_url=base_url)
        try:
            yield app, client
        finally:
            client.close()


def csrf_headers(client, **extra):
    return {"x-csrf-token": client.cookies["luigi_csrf"], **extra}


class LocalAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and attributes.get("src"):
            self.urls.append(attributes["src"])
        elif tag == "link" and attributes.get("rel") == "stylesheet":
            self.urls.append(attributes["href"])


class FakeWorksheetTests(unittest.TestCase):
    def test_headers_and_reads_are_independent(self):
        sheet = FakeWorksheet(["Profile", "Title"], [{"Title": "Prism Circuit"}])
        values = sheet.get_all_values()
        values[1][1] = "Changed copy"
        self.assertEqual(sheet.get_all_values(), [["Profile", "Title"], ["", "Prism Circuit"]])

    def test_ranges_past_z_and_rows_past_nine(self):
        sheet = FakeWorksheet(["Title"])
        sheet.batch_update([{"range": "'Example Games'!$AA$12:AB13", "values": [[1, 2], [3, 4]]}])
        self.assertEqual(sheet.get_all_values()[11][26:28], ["1", "2"])
        self.assertEqual(sheet.get_all_values()[12][26:28], ["3", "4"])
        sheet.update(values=[["Prism Circuit"]], range_name="A2")
        sheet.update([["Signal Atlas"]], "A3")
        self.assertEqual(sheet.get_all_values()[1:3], [["Prism Circuit"], ["Signal Atlas"]])

    def test_invalid_batch_does_not_partially_write(self):
        sheet = FakeWorksheet(["Title"])
        with self.assertRaises(ValueError):
            sheet.batch_update([
                {"range": "A2", "values": [["Prism Circuit"]]},
                {"range": "B2:A1", "values": [["Invalid"]]},
            ])
        self.assertEqual(sheet.get_all_values(), [["Title"]])
        for reference in ("A0", "A10001", "AAAA1", "A1:B2:C3"):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                sheet.update(reference, [["Invalid"]])


class BitmapTests(unittest.TestCase):
    def test_generated_png_has_pixels_and_valid_chunks(self):
        bitmap = cover_png(1)
        self.assertTrue(bitmap.startswith(b"\x89PNG\r\n\x1a\n"))
        offset, compressed = 8, bytearray()
        while offset < len(bitmap):
            size = struct.unpack(">I", bitmap[offset:offset + 4])[0]
            kind = bitmap[offset + 4:offset + 8]
            payload = bitmap[offset + 8:offset + 8 + size]
            checksum = struct.unpack(">I", bitmap[offset + 8 + size:offset + 12 + size])[0]
            self.assertEqual(checksum, zlib.crc32(kind + payload))
            if kind == b"IHDR":
                self.assertEqual(struct.unpack(">IIBBBBB", payload), (240, 320, 8, 2, 0, 0, 0))
            elif kind == b"IDAT":
                compressed.extend(payload)
            offset += size + 12
        pixels = zlib.decompress(compressed)
        self.assertEqual(len(pixels), 320 * (1 + 240 * 3))
        self.assertGreater(len(set(pixels)), 4)
        self.assertNotEqual(bitmap, cover_png(2))
        with self.assertRaises(ValueError):
            cover_png(99999)


class MediaPreviewTests(unittest.TestCase):
    @isolated
    def test_smoke_uses_actual_routes_without_startup(self):
        with preview_context() as app:
            def unexpected_startup():
                self.fail("Preview must not execute startup or shutdown hooks")

            app.router.on_startup.append(unexpected_startup)
            app.router.on_shutdown.append(unexpected_startup)
            self.assertEqual(smoke_check(app), {"endpoints": 9, "mutations": 12})
            self.assertEqual([module.id for module in app.state.modules.enabled], ["media"])
            app.state.preview_engine_guard.assert_not_called()

    @isolated
    def test_bootstrap_preserves_main_session_and_scopes_preview_per_port(self):
        from fastapi.testclient import TestClient

        with preview_client("http://127.0.0.1:58107") as (app, client):
            client.cookies.set("luigi_session", "synthetic-existing-main-session")
            client.cookies.set("luigi_csrf", "synthetic-existing-csrf")
            response = client.get("/", follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/games")
            cookies = response.headers.get_list("set-cookie")
            self.assertTrue(any("luigi_media_preview_session_58107=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie for cookie in cookies))
            self.assertFalse(any(cookie.startswith("luigi_session=") for cookie in cookies))
            self.assertEqual(client.cookies["luigi_session"], "synthetic-existing-main-session")
            self.assertEqual(client.cookies["luigi_csrf"], "synthetic-existing-csrf")
            other = TestClient(app, base_url="http://127.0.0.1:58108", cookies=client.cookies)
            try:
                self.assertEqual(other.post("/media/games/refresh", headers=csrf_headers(client)).status_code, 401)
            finally:
                other.close()

    @isolated
    def test_every_write_requires_scoped_session_and_core_csrf(self):
        with preview_client() as (app, client):
            for path in ("/media/games/refresh", "/media/shows/episode", "/media/games/steam/refresh", "/gnw/games/search"):
                self.assertEqual(client.post(path).status_code, 401)
            client.get("/games")
            self.assertEqual(client.post("/media/games/refresh").status_code, 403)
            self.assertEqual(client.post("/media/games/refresh", headers={"x-csrf-token": "wrong"}).status_code, 403)
            self.assertEqual(client.post("/media/games/refresh", headers=csrf_headers(client)).status_code, 200)
            internal_token = os.environ["LUIGI_WEB_UI_TOKEN"]
            client.cookies.clear()
            client.cookies.set("luigi_session", internal_token)
            self.assertEqual(client.post("/media/games/refresh", headers={"Authorization": f"Bearer {internal_token}"}, params={"token": internal_token}).status_code, 401)
            app.state.preview_engine_guard.assert_not_called()

    @isolated
    def test_origin_and_referer_cannot_bypass_csrf(self):
        with preview_client() as (app, client):
            client.get("/games")
            for extra in (
                {"origin": "https://example.invalid"}, {"origin": "null"},
                {"origin": "http://testserver:58108"}, {"referer": "https://example.invalid/games"},
                {"referer": "http://testserver.example.invalid/games"},
                {"referer": "http://example.invalid@testserver/games"},
                {"referer": "http://[malformed"}, {"referer": "//testserver/games"},
                {"origin": "http://testserver", "referer": "https://example.invalid/games"},
            ):
                with self.subTest(headers=extra):
                    self.assertEqual(client.post("/media/games/refresh", headers=csrf_headers(client, **extra)).status_code, 403)
            self.assertEqual(client.post("/media/games/refresh", headers=csrf_headers(client, origin="http://testserver", referer="http://testserver/games?view=grid")).status_code, 200)

    @isolated
    def test_exact_route_allowlist_blocks_other_modules_and_legacy_writes(self):
        with preview_client() as (app, client):
            client.get("/games")
            for path in ("/admin", "/admin/env", "/finance", "/tasks", "/feedback", "/modules", "/docs", "/openapi.json", "/chat/panel", "/media/music/data", "/module-assets/finance/anything"):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertIn(response.status_code, (403, 404))
                    self.assertEqual(response.headers["cache-control"], "no-store")
            for path in ("/admin/env", "/logout", "/login", "/modules", "/gnw/games/status", "/gnw/shows/update", "/media/music/change", "/media/games/refresh/", "/media/shows/steam/save", "/media/games/delete"):
                with self.subTest(path=path):
                    self.assertEqual(client.post(path, headers=csrf_headers(client)).status_code, 403)
            for method in ("PUT", "PATCH", "DELETE", "OPTIONS"):
                self.assertEqual(client.request(method, "/media/games/change", headers=csrf_headers(client)).status_code, 403)

    @isolated
    def test_host_and_peer_must_be_loopback(self):
        from fastapi.testclient import TestClient

        with preview_client() as (app, client):
            for host in ("example.invalid", "localhost.example.invalid", "192.0.2.8", "127.0.0.1.example.invalid"):
                self.assertEqual(client.get("/games", headers={"host": host}).status_code, 403)
            self.assertEqual(client.get("/games", headers={"host": "localhost:58107"}).status_code, 200)

            async def remote(scope, receive, send):
                await app({**scope, "client": ("192.0.2.8", 12345)}, receive, send)

            remote_client = TestClient(remote)
            try:
                self.assertEqual(remote_client.get("/games", headers={"x-forwarded-for": "127.0.0.1"}).status_code, 403)
            finally:
                remote_client.close()

    @isolated
    def test_library_assets_and_covers_are_local_and_disposable(self):
        with preview_client() as (app, client):
            for path, title in (("/games", "Prism Circuit"), ("/shows", "The Lantern Index")):
                page = client.get(path)
                self.assertEqual(page.status_code, 200)
                self.assertIn(title, page.text)
                self.assertIn("img-src 'self' data:", page.headers["content-security-policy"])
                parser = LocalAssets()
                parser.feed(page.text)
                self.assertTrue(parser.urls)
                for url in parser.urls:
                    self.assertFalse(urlsplit(url).netloc)
                    self.assertTrue(url.startswith(("/static/", "/module-assets/media/")))
                    self.assertEqual(client.get(url).status_code, 200, url)
            for section in ("games", "shows"):
                state = client.get(f"/media/{section}/data").json()
                self.assertEqual(len(state["items"]), 12)
                for item in state["items"]:
                    self.assertTrue(item["cover_url"].startswith("http://testserver/preview-media/covers/"))
                    response = client.get(item["cover_url"])
                    self.assertEqual(response.headers["content-type"], "image/png")
                    self.assertTrue(response.content.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertIn(client.get("/preview-media/covers/99999.png").status_code, (403, 404))
            self.assertFalse(list(app.state.preview_directory.rglob("*.png")))

    @isolated
    def test_cover_urls_follow_the_current_loopback_port(self):
        from fastapi.testclient import TestClient

        with preview_client("http://127.0.0.1:58107") as (app, client):
            first = client.get("/media/games/data").json()["items"][0]["cover_url"]
            self.assertTrue(first.startswith("http://127.0.0.1:58107/preview-media/covers/"))
            alternate = TestClient(app, base_url="http://localhost:58108")
            try:
                second = alternate.get("/media/games/data").json()["items"][0]["cover_url"]
                self.assertTrue(second.startswith("http://localhost:58108/preview-media/covers/"))
                self.assertEqual(alternate.get(second).status_code, 200)
            finally:
                alternate.close()

    @isolated
    def test_environment_temporary_storage_and_cached_globals_restore(self):
        with tempfile.TemporaryDirectory(prefix="media-preview-test-") as temporary:
            forbidden = Path(temporary) / "untouched"
            overrides = {
                "LUIGI_WEB_DATA_DIR": str(forbidden), "LUIGI_WEB_MEDIA_DB": str(forbidden / "media.sqlite3"),
                "LUIGI_WEB_MODULES_FILE": str(forbidden / "modules.json"),
                "LUIGI_WEB_MODULES": "finance,admin", "LUIGI_WEB_EXTERNAL_MODULES": "synthetic-disabled-module",
                "LUIGI_WEB_STEAM_API_KEY": "synthetic-not-a-credential", "LUIGI_WEB_GNW_CREDS_FILE": str(forbidden / "absent.json"),
            }
            with patch.dict(os.environ, overrides):
                before = dict(os.environ)
                with preview_client() as (app, client):
                    directory = app.state.preview_directory
                    self.assertNotEqual(directory, forbidden)
                    self.assertEqual(Path(os.environ["LUIGI_WEB_MEDIA_DB"]).parent, directory)
                    self.assertFalse(Path(os.environ["LUIGI_WEB_MODULES_FILE"]).exists())
                    self.assertNotIn("LUIGI_WEB_STEAM_API_KEY", os.environ)
                    self.assertNotIn("LUIGI_WEB_GNW_CREDS_FILE", os.environ)
                    self.assertNotIn("LUIGI_WEB_EXTERNAL_MODULES", os.environ)
                    self.assertEqual(client.get("/games").status_code, 200)
                self.assertFalse(directory.exists())
                self.assertFalse(forbidden.exists())
                self.assertEqual(dict(os.environ), before)
            from luigi_web.modules.media import service, steam, workspace

            sentinel = object()
            old_cache = {"synthetic-sentinel": sentinel}
            old_steam = OrderedDict({"synthetic-sentinel": sentinel})
            old_undo = OrderedDict({"synthetic-sentinel": sentinel})
            with patch.object(service, "_sheet", sentinel), patch.object(service, "_client", sentinel), patch.object(service, "_cache", old_cache), patch.object(steam, "_cache", old_steam), patch.object(workspace, "_undo", old_undo):
                with preview_client() as (app, client):
                    self.assertNotIn("synthetic-sentinel", service._cache)
                    self.assertEqual(len(client.get("/media/games/data").json()["items"]), 12)
                    self.assertIsNone(client.get("/media/games/steam", params={"profile": "Example profile", "title": "Prism Circuit"}).json()["snapshot"])
                self.assertIs(service._sheet, sentinel)
                self.assertIs(service._client, sentinel)
                self.assertIs(service._cache, old_cache)
                self.assertEqual(old_cache, {"synthetic-sentinel": sentinel})
                self.assertIs(steam._cache, old_steam)
                self.assertIs(workspace._undo, old_undo)

    @isolated
    def test_network_credentials_and_nonpreview_databases_are_blocked(self):
        with preview_context() as app:
            from luigi_web.modules.media import service
            from luigi_web.modules.tasks import repository
            import sqlalchemy

            with self.assertRaises(RuntimeError):
                repository.get_engine()
            with self.assertRaises(RuntimeError):
                sqlalchemy.create_engine("sqlite://")
            with self.assertRaises(RuntimeError):
                sqlite3.connect(app.state.preview_directory.parent / "forbidden-preview-test.sqlite3")
            with self.assertRaises(RuntimeError):
                service._load_credentials()
            with self.assertRaises(RuntimeError):
                socket.create_connection(("example.invalid", 443))
            with self.assertRaises(RuntimeError):
                socket.getaddrinfo("example.invalid", 443)
            for address in (("127.0.0.1", 9), ("192.0.2.8", 443)):
                with socket.socket() as connection, self.assertRaises(RuntimeError):
                    connection.connect(address)

    @isolated
    def test_catalog_creation_verifies_synthetic_metadata_without_http(self):
        with preview_context() as app:
            from luigi_web.modules.media import service

            for section, source, external_id in (
                ("games", "steam", "9000043"),
                ("shows", "tvmaze", "9000044"),
            ):
                with self.subTest(section=section):
                    metadata = service.catalog_lookup(section, source, external_id)
                    self.assertIsNotNone(metadata)
                    self.assertEqual(
                        service.add_catalog_item(section, "Example profile", metadata),
                        (True, metadata["title"]),
                    )
                    values = app.state.preview_worksheets[section].get_all_values()
                    self.assertEqual(values[-1][values[0].index("Cover URL")], metadata["cover_url"])
            app.state.preview_engine_guard.assert_not_called()

    @isolated
    def test_catalog_search_and_add_use_only_synthetic_metadata(self):
        for base_url in ("http://testserver", "http://127.0.0.1:58107", "http://localhost:58108"):
            with self.subTest(base_url=base_url), preview_client(base_url) as (app, client):
                client.get("/games")
                for section, title, source, external_id, cover_id in (
                    ("games", "Vector Orchard", "steam", "9000043", 31),
                    ("shows", "The Folded Sky", "tvmaze", "9000044", 32),
                ):
                    with self.subTest(section=section):
                        cover_url = f"{base_url}/preview-media/covers/{cover_id}.png"
                        self.assertEqual(client.get(f"/gnw/{section}/new").status_code, 200)
                        response = client.post(f"/gnw/{section}/search", data={"query": "Synthetic catalog", "profile": "Example profile"}, headers=csrf_headers(client))
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(title, response.text)
                        self.assertIn(cover_url, response.text)
                        response = client.post(f"/gnw/{section}/add", data={"source": source, "external_id": external_id, "profile": "Example profile"}, headers=csrf_headers(client))
                        self.assertEqual(response.status_code, 204)
                        items = client.get(f"/media/{section}/data").json()["items"]
                        self.assertEqual(len(items), 13)
                        added = next(item for item in items if item["title"] == title)
                        self.assertEqual(added["cover_url"], cover_url)
                        cover = client.get(added["cover_url"])
                        self.assertEqual(cover.status_code, 200)
                        self.assertEqual(cover.headers["content-type"], "image/png")
                app.state.preview_engine_guard.assert_not_called()

    @isolated
    def test_unknown_catalog_selection_is_rejected_without_mutation(self):
        with preview_client() as (app, client):
            client.get("/games")
            for section, source in (("games", "steam"), ("shows", "tvmaze")):
                with self.subTest(section=section):
                    worksheet = app.state.preview_worksheets[section]
                    before = worksheet.get_all_values()
                    response = client.post(f"/gnw/{section}/add", data={"source": source, "external_id": "unknown", "profile": "Example profile"}, headers=csrf_headers(client))
                    self.assertEqual(worksheet.get_all_values(), before)
                    self.assertNotIn("HX-Trigger", response.headers)
                    self.assertEqual(response.status_code, 422)
            app.state.preview_engine_guard.assert_not_called()

    @isolated
    def test_stale_mutation_is_rejected_and_original_history_is_preserved(self):
        with preview_client() as (app, client):
            item = client.get("/media/games/data").json()["items"][0]
            identity = {key: item[key] for key in ("profile", "title")}
            before = client.get("/media/games/detail", params=identity).json()
            changed = client.post("/media/games/change", data={**identity, "expected_version": item["version"], "fields": json.dumps({"notes": "Synthetic changed note"})}, headers=csrf_headers(client))
            self.assertEqual(changed.status_code, 200)
            stale = client.post("/media/games/change", data={**identity, "expected_version": item["version"], "fields": json.dumps({"notes": "Synthetic stale note"})}, headers=csrf_headers(client))
            self.assertEqual(stale.status_code, 409)
            restored = client.post("/media/games/undo", data={"token": changed.json()["undo_token"]}, headers=csrf_headers(client))
            self.assertEqual(restored.status_code, 200)
            self.assertEqual(restored.json()["item"]["notes"], item["notes"])
            detail = client.get("/media/games/detail", params=identity).json()
            self.assertEqual(len(detail["activity"]), len(before["activity"]) + 2)
            self.assertEqual(detail["runs"], before["runs"])
            self.assertIsNone(detail["history_warning"])


if __name__ == "__main__":
    if sys.argv[1:2] == ["--isolated"]:
        _ISOLATED = True
        del sys.argv[1]
    unittest.main()