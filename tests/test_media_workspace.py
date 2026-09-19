"""Offline HTTP integration of the media library, Sheets, and SQLite history."""
from __future__ import annotations

import builtins
from collections import OrderedDict
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


def import_without_google(name, globals=None, locals=None, fromlist=(), level=0, *, importer=builtins.__import__):
    """The optional Google client is outside this fake-worksheet integration."""
    if name == "gspread":
        raise ImportError("Google client is disabled for offline worksheet tests")
    return importer(name, globals, locals, fromlist, level)


class FakeWorksheet:
    def __init__(self, headers, records):
        self.values = [list(headers)]
        self.values.extend([[record.get(header, "") for header in headers] for record in records])
        self.batches = []
        self.fail_after = None
        self.ignore_writes = False

    def set(self, header, value, row=1):
        self.values[row][self.values[0].index(header)] = value

    def append(self, **record):
        self.values.append([record.get(header, "") for header in self.values[0]])

    def batch_update(self, batch, **kwargs):
        self.batches.append((deepcopy(batch), kwargs))
        if self.ignore_writes:
            return
        for index, update in enumerate(batch):
            address = update["range"].split(":", 1)[0]
            match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", address)
            if match is None:
                raise AssertionError(f"Unexpected cell address: {address}")
            column = 0
            for letter in match[1]:
                column = column * 26 + ord(letter) - ord("A") + 1
            row = int(match[2]) - 1
            for row_offset, cells in enumerate(update["values"]):
                for column_offset, value in enumerate(cells):
                    self.values[row + row_offset][column - 1 + column_offset] = value
            if self.fail_after == index + 1:
                raise RuntimeError("Synthetic worksheet response lost")
        if self.fail_after == "all":
            raise RuntimeError("Synthetic worksheet response lost")


class MediaWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "media.sqlite3"
        self.enterContext(patch.dict(os.environ, {
            "LUIGI_WEB_MODULES": "tasks,discipline,planning,media",
            "LUIGI_WEB_DATA_DIR": temporary.name,
            "LUIGI_WEB_MEDIA_DB": str(self.database),
            "LUIGI_WEB_UI_TOKEN": "synthetic-media-token",
            "LUIGI_WEB_SECURE_COOKIES": "0",
            "LUIGI_WEB_STEAM_API_KEY": "synthetic-api-key",
            "LUIGI_WEB_STEAM_ID": "00000000000000001",
        }, clear=True))
        self.enterContext(patch("dotenv.load_dotenv", return_value=False))
        with patch("builtins.__import__", side_effect=import_without_google):
            from luigi_web.modules.media import history, routes, service, steam, workspace
        from luigi_web.modules.tasks import repository

        self.history, self.service, self.steam, self.workspace = history, service, steam, workspace
        self.enterContext(patch.object(history, "DATA_DIR", self.root))
        self.enterContext(patch.object(repository, "get_engine", side_effect=AssertionError("Live database forbidden")))
        self.enterContext(patch.object(workspace, "_undo", OrderedDict()))
        self.enterContext(patch.object(workspace, "_picks", OrderedDict()))
        self.enterContext(patch.object(service, "_cache", {}))
        self.enterContext(patch.object(steam, "_cache", OrderedDict()))
        self.enterContext(patch.object(history.clock, "local_today", return_value=date(2026, 9, 19)))
        self.enterContext(patch.object(history.clock, "local_now", return_value=datetime(2026, 9, 19, 12, tzinfo=timezone.utc)))
        self.fetch_steam = self.enterContext(patch.object(steam, "_fetch", side_effect=AssertionError("Steam network forbidden")))
        self.legacy_steam = self.enterContext(patch.object(service, "steam_stats", side_effect=AssertionError("Legacy Steam fetch forbidden")))
        self.enterContext(patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("Network forbidden")))
        self.enterContext(patch("httpx.AsyncHTTPTransport.handle_async_request", side_effect=AssertionError("Network forbidden")))
        self.enterContext(patch.object(service, "is_enabled", return_value=True))
        self.enterContext(patch.object(service, "disabled_reason", return_value=None))
        self.sheets = {
            "games": FakeWorksheet(service.GAME_HEADERS, [{
                "Profile": "Example profile", "Title": "Example game", "Status": "backlog",
                "Priority": "3", "Rating": "", "Hours Played": "2.5",
                "Date Started": "2024-01-02", "Date Completed": "",
                "Source": "steam", "External ID": "123", "Notes": "Example notes",
            }]),
            "shows": FakeWorksheet(service.SHOW_HEADERS, [{
                "Profile": "Example profile", "Title": "Example show", "Status": "watching",
                "Priority": "3", "Current Season": "1", "Current Episode": "2",
                "Total Episodes": "3", "Date Started": "2024-03-04", "Date Completed": "",
            }]),
        }
        self.reads = self.enterContext(patch.object(service, "_all_values", side_effect=self.read_sheet))
        self.worksheet = self.enterContext(patch.object(service, "_ws", side_effect=lambda section: self.sheets[section]))
        from luigi_web import application as host

        self.host = host
        app = FastAPI()
        app.state.modules = SimpleNamespace(
            is_enabled=lambda module: module in {"tasks", "discipline", "planning", "media"},
            navigation=lambda: [], enabled={"tasks", "discipline", "planning", "media"},
            landing_path="/games",
        )
        app.middleware("http")(host.csrf_middleware)
        app.include_router(routes.router)
        self.routes = routes
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer synthetic-media-token"}

    def read_sheet(self, section, *, force=False):
        return deepcopy(self.sheets[section].values)

    def get(self, path, **params):
        return self.client.get(path, params=params, headers=self.headers)

    def post(self, path, **fields):
        return self.client.post(path, data=fields, headers=self.headers)

    def item(self, section="games"):
        response = self.get(f"/media/{section}/data")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"][0]

    def change(self, section="games", action="change", item=None, **fields):
        item = self.item(section) if item is None else item
        return self.post(f"/media/{section}/{action}", profile=item["profile"], title=item["title"],
                         expected_version=item["version"], fields=json.dumps(fields))

    def recorded(self, section="games", title=None):
        return self.history.detail(section, "Example profile", title or f"Example {section[:-1]}")

    def assert_api(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    def detail(self, section="games", **identity):
        return self.get(f"/media/{section}/detail", **{
            "profile": "Example profile", "title": f"Example {section[:-1]}", **identity,
        })

    def assert_no_writes(self):
        self.assertTrue(all(not sheet.batches for sheet in self.sheets.values()))

    def mutation_paths(self):
        return sorted({route.path.replace("{section}", section)
                       for route in self.routes.router.routes
                       if "POST" in getattr(route, "methods", set())
                       for section in ("games", "shows")})

    def sql(self, statement, parameters=()):
        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(statement, parameters).fetchall()
            connection.commit()
            return rows

    def refresh_steam(self, hours=12.5):
        self.fetch_steam.side_effect = None
        self.fetch_steam.return_value = {
            "app_id": "123", "name": "Example game", "hours_played": hours,
            "hours_recent": 0, "achievements_total": 2, "achievements_unlocked": 2,
            "achievement_percent": 100, "complete": True, "next_achievements": [],
            "playtime_unavailable": hours is None, "achievements_unavailable": False,
        }
        return self.assert_api(self.post("/media/games/steam/refresh",
                                         profile="Example profile", title="Example game"))

    def test_data_has_stable_identity_and_version_without_writes(self):
        for section in ("games", "shows"):
            with self.subTest(section=section):
                response = self.get(f"/media/{section}/data")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                item = response.json()["items"][0]
                self.assertRegex(item["key"], r"^[a-f0-9]{64}$")
                self.assertRegex(item["version"], r"^[a-f0-9]{64}$")
                self.assertEqual(item, self.item(section))
                self.assertEqual(self.sheets[section].batches, [])

    def test_changed_field_is_verified_and_recorded_in_real_sqlite(self):
        before = self.item()
        response = self.change(item=before, rating=8)
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()
        self.assertEqual(saved["item"]["rating"], 8)
        self.assertNotEqual(saved["item"]["version"], before["version"])
        self.assertEqual(saved["item"]["key"], before["key"])
        self.assertIsNone(saved["history_warning"])
        self.assertTrue(saved["undo_token"])
        self.assertEqual(len(self.sheets["games"].batches), 1)
        self.assertEqual(self.item()["rating"], 8)
        self.assertEqual(len(self.recorded()["activity"]), 1)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_events").fetchone()[0], 1)

    def test_pages_render_library_and_embed_data_keys_and_versions(self):
        with patch.object(self.host.templates, "TemplateResponse", wraps=self.host.templates.TemplateResponse) as render:
            for section in ("games", "shows"):
                with self.subTest(section=section):
                    response = self.get(f"/{section}")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertEqual(render.call_args.args[0], "library.html")
                    self.assertIn('id="media-library"', response.text)
                    seed = re.search(r'<script[^>]*id="library-seed"[^>]*>(.*?)</script>', response.text, re.DOTALL)
                    self.assertIsNotNone(seed)
                    state = json.loads(seed[1])
                    self.assertEqual(state["section"], section)
                    self.assertEqual(state["items"], self.get(f"/media/{section}/data").json()["items"])
                    self.assertRegex(state["items"][0]["key"], r"^[a-f0-9]{64}$")
                    self.assertRegex(state["items"][0]["version"], r"^[a-f0-9]{64}$")
        self.assert_no_writes()

    def test_all_get_routes_and_mutations_require_authentication(self):
        for route in self.routes.router.routes:
            for method in sorted(getattr(route, "methods", set()) & {"GET", "POST"}):
                for section in ("games", "shows"):
                    path = route.path.replace("{section}", section)
                    with self.subTest(method=method, path=path):
                        response = self.client.request(method, path)
                        self.assertEqual(response.status_code, 401, response.text)
        self.reads.assert_not_called()
        self.fetch_steam.assert_not_called()
        self.assert_no_writes()
        self.assertFalse(self.database.exists())

    def test_cookie_csrf_blocks_every_mutation_before_domain_access(self):
        self.client.cookies.set("luigi_session", "synthetic-media-token")
        self.get("/media/games/data")
        self.reads.reset_mock()
        for path in self.mutation_paths():
            for headers in ({}, {"X-CSRF-Token": "synthetic-wrong-token"}):
                with self.subTest(path=path, headers=headers):
                    response = self.client.post(path, headers=headers)
                    self.assertEqual(response.status_code, 403, response.text)
        self.reads.assert_not_called()
        self.fetch_steam.assert_not_called()
        self.assert_no_writes()
        self.assertFalse(self.database.exists())

    def test_valid_csrf_reaches_each_workspace_mutation_and_bearer_bypasses(self):
        self.client.cookies.set("luigi_session", "synthetic-media-token")
        self.get("/media/games/data")
        csrf = {"X-CSRF-Token": self.client.cookies["luigi_csrf"]}
        statuses = {"refresh": 200, "pick": 200, "change": 422, "episode": 422,
                    "runs": 422, "undo": 409, "steam/refresh": 422, "steam/save": 422}
        for path in self.mutation_paths():
            if not path.startswith("/media/"):
                continue
            action = "/".join(path.split("/")[3:])
            for headers in (csrf, self.headers):
                with self.subTest(path=path, bearer=headers is self.headers):
                    self.assert_api(self.client.post(path, headers=headers), statuses[action])
        item = self.item()
        response = self.client.post("/media/games/change", data={
            "profile": item["profile"], "title": item["title"],
            "expected_version": item["version"], "fields": json.dumps({"rating": 5}),
        }, headers=csrf)
        self.assertEqual(self.assert_api(response)["item"]["rating"], 5)

    def test_only_changed_cells_are_written_even_beyond_column_z(self):
        sheet = self.sheets["games"]
        record = dict(zip(sheet.values[0], sheet.values[1]))
        headers = [header for header in self.service.GAME_HEADERS if header not in {"Notes", "Rating"}]
        headers += [f"Synthetic extra {index}" for index in range(26 - len(headers))]
        headers += ["Notes", "Rating"]
        self.sheets["games"] = sheet = FakeWorksheet(headers, [record])
        before = deepcopy(sheet.values)
        saved = self.assert_api(self.change(notes="Example changed notes", rating=7, priority=3))
        self.assertEqual(saved["item"]["notes"], "Example changed notes")
        self.assertEqual(sheet.batches, [([
            {"range": "AA2", "values": [["Example changed notes"]]},
            {"range": "AB2", "values": [["7"]]},
        ], {"value_input_option": "RAW"})])
        self.assertEqual(sheet.values[1][:26], before[1][:26])
        event = self.recorded()["activity"][0]
        self.assertEqual(event["kind"], "update")
        self.assertEqual(len(self.recorded()["activity"]), 1)

    def test_completion_undo_restores_all_original_cells_and_timestamps(self):
        sheet = self.sheets["games"]
        sheet.set("Date Started", "2024-01-02T09:10:11+00:00")
        sheet.set("Last Played", "2024-02-03T12:13:14+00:00")
        before = deepcopy(sheet.values)
        original = self.item()
        saved = self.assert_api(self.change(status="completed", rating=9, notes="Example completed"))
        self.assertEqual(saved["item"]["date_started"], original["date_started"])
        self.assertEqual(saved["item"]["date_completed"], "2026-09-19")
        self.assertEqual(saved["undo_ttl_ms"], 12000)
        undone = self.assert_api(self.post("/media/games/undo", token=saved["undo_token"]))
        self.assertEqual(sheet.values, before)
        self.assertEqual(undone["item"], original)
        self.assertIsNone(undone["undo_token"])
        self.assertIsNone(undone["history_warning"])
        self.assertEqual([event["kind"] for event in self.recorded()["activity"]], ["undo", "update"])
        self.assert_api(self.post("/media/games/undo", token=saved["undo_token"]), 409)

    def test_stale_change_and_undo_do_not_overwrite_external_edits(self):
        before = self.item()
        saved = self.assert_api(self.change(item=before, rating=5))
        self.assert_api(self.change(item=before, rating=7), 409)
        self.sheets["games"].set("Notes", "Example external edit")
        self.assert_api(self.post("/media/games/undo", token=saved["undo_token"]), 409)
        self.assertEqual(self.item()["notes"], "Example external edit")
        self.assertEqual(self.item()["rating"], 5)
        self.assertEqual(len(self.sheets["games"].batches), 1)
        self.assertEqual(len(self.recorded()["activity"]), 1)

    def test_undo_is_section_bound_and_expires_without_writing(self):
        saved = self.assert_api(self.change(rating=5))
        self.assert_api(self.post("/media/shows/undo", token=saved["undo_token"]), 409)
        self.workspace._undo[saved["undo_token"]]["expires"] = 0
        self.assert_api(self.post("/media/games/undo", token=saved["undo_token"]), 409)
        self.assertEqual(len(self.sheets["games"].batches), 1)

    def test_episode_never_rolls_season_or_auto_completes_at_or_over_total(self):
        for season in (1, 2, 5):
            self.sheets["shows"].set("Current Season", str(season))
            self.sheets["shows"].set("Current Episode", "2")
            for expected_episode in (3, 4):
                with self.subTest(season=season, episode=expected_episode):
                    result = self.assert_api(self.change("shows", "episode"))
                    self.assertEqual(result["item"]["current_episode"], expected_episode)
                    self.assertEqual(result["item"]["current_season"], season)
                    self.assertEqual(result["item"]["total_episodes"], 3)
                    self.assertEqual(result["item"]["status"], "watching")
                    self.assertFalse(result["item"]["date_completed"])
        self.assertEqual([event["kind"] for event in self.recorded("shows")["activity"]], ["episode"] * 6)

    def test_episode_rejects_games_and_finished_or_dropped_shows(self):
        self.assert_api(self.change(action="episode"), 422)
        for status in ("completed", "dropped"):
            with self.subTest(status=status):
                self.sheets["shows"].set("Status", status)
                self.assert_api(self.change("shows", "episode"), 422)
        self.assert_no_writes()
        self.assertEqual(self.recorded("shows")["activity"], [])

    def test_replay_adds_run_preserves_dates_and_undo_restores_previous_runs(self):
        for section in ("games", "shows"):
            with self.subTest(section=section):
                sheet = self.sheets[section]
                sheet.set("Status", "completed")
                sheet.set("Date Started", "2024-01-02T09:10:11+00:00")
                sheet.set("Date Completed", "2024-02-03T12:13:14+00:00")
                if section == "shows":
                    sheet.set("Current Season", "4")
                    sheet.set("Current Episode", "12")
                self.assert_api(self.change(section, rating=6))
                before_cells = deepcopy(sheet.values)
                before_runs = self.recorded(section)["runs"]
                self.assertEqual(len(before_runs), 1)
                replayed = self.assert_api(self.change(section, "runs"))
                self.assertEqual(replayed["item"]["status"], self.service.ACTIVE_STATUS[section])
                self.assertEqual(replayed["item"]["date_started"], "2024-01-02T09:10:11+00:00")
                self.assertEqual(replayed["item"]["date_completed"], "2024-02-03T12:13:14+00:00")
                if section == "shows":
                    self.assertEqual(replayed["item"]["current_season"], 1)
                    self.assertEqual(replayed["item"]["current_episode"], 0)
                runs = self.recorded(section)["runs"]
                self.assertEqual([run["number"] for run in runs], [2, 1])
                self.assertEqual(runs[1], before_runs[0])
                self.assertEqual(runs[0]["origin"], "web")
                self.assertIsNone(runs[0]["completed_at"])
                self.assert_api(self.post(f"/media/{section}/undo", token=replayed["undo_token"]))
                self.assertEqual(sheet.values, before_cells)
                self.assertEqual(self.recorded(section)["runs"], before_runs)

    def test_replay_rejects_active_or_unstarted_items(self):
        for section in ("games", "shows"):
            for status in ("backlog", self.service.ACTIVE_STATUS[section]):
                with self.subTest(section=section, status=status):
                    self.sheets[section].set("Status", status)
                    self.assert_api(self.change(section, "runs"), 422)
        self.assert_no_writes()

    def test_invalid_fields_and_forged_sheet_headers_are_rejected(self):
        cases = [
            ("games", {"priority": value}) for value in (0, 6, -1, "invalid", 1.5, True, None)
        ] + [("games", {"rating": value}) for value in (-1, 11, "invalid", 1.5, True)]
        cases += [("shows", {field: value})
                  for field in ("current_episode", "current_season", "total_episodes")
                  for value in (-1, "invalid", 1.5, 100001)]
        cases += [("games", {"hours_played": value}) for value in (-1, "NaN", "Infinity", "invalid", 1000001)]
        cases += [("games", {"notes": "x" * 4097}), ("shows", {"notes": "x" * 4097}),
                  ("games", {"status": "watching"}), ("shows", {"status": "playing"}),
                  ("games", {"current_episode": 3}), ("shows", {"hours_played": 3})]
        cases += [("games", {field: "forged"}) for field in (
            "Status", "Priority", "Rating", "Notes", "Date Started", "Date Completed", "Profile", "Title",
            "Source", "External ID", "date_started", "date_completed", "profile", "title", "source", "key", "version",
        )]
        for section, fields in cases:
            with self.subTest(section=section, fields={key: str(value)[:40] for key, value in fields.items()}):
                self.assert_api(self.change(section, **fields), 422)
        self.assert_no_writes()
        self.assertEqual(self.recorded()["activity"], [])
        self.assertEqual(self.recorded("shows")["activity"], [])

    def test_valid_numeric_and_notes_boundaries(self):
        for fields in ({"priority": 1, "rating": 0, "hours_played": 0},
                       {"priority": 5, "rating": 10, "hours_played": 1000000, "notes": "x" * 4096},
                       {"rating": None}):
            with self.subTest(fields=list(fields)):
                item = self.assert_api(self.change(**fields))["item"]
                for key, value in fields.items():
                    if key == "hours_played":
                        self.assertEqual(float(item[key]), value)
                    else:
                        self.assertEqual(item[key], value)

    def test_malformed_payloads_versions_and_identity_limits(self):
        item = self.item()
        base = {"profile": item["profile"], "title": item["title"],
                "expected_version": item["version"], "fields": json.dumps({"rating": 4})}
        for overrides in (
            {"fields": "["}, {"fields": "[]"}, {"fields": "null"}, {"fields": "{}"},
            {"fields": " " * 24001}, {"expected_version": ""}, {"expected_version": "not-a-version"},
            {"expected_version": "g" * 64}, {"profile": ""}, {"title": ""},
            {"profile": "x" * 257}, {"title": "x" * 513},
        ):
            with self.subTest(fields=list(overrides)):
                self.assert_api(self.post("/media/games/change", **{**base, **overrides}), 422)
        long_profile = "x" * 257
        self.assert_api(self.get("/media/games/data", profile=long_profile), 422)
        self.assert_api(self.detail(profile=long_profile), 422)
        self.assert_api(self.post("/media/games/refresh", profile=long_profile), 422)
        self.assert_api(self.post("/media/games/pick", profile=long_profile, keys="[]"), 422)
        self.assert_api(self.get("/media/unknown/data"), 404)
        self.assert_no_writes()

    def test_missing_editable_column_fails_before_any_sheet_write(self):
        for section, header, field, value in (
            ("games", "Rating", "rating", 4),
            ("shows", "Current Episode", "current_episode", 4),
        ):
            with self.subTest(section=section):
                sheet = self.sheets[section]
                column = sheet.values[0].index(header)
                for row in sheet.values:
                    row.pop(column)
                before = deepcopy(sheet.values)
                self.assert_api(self.change(section, **{field: value}), 422)
                self.assertEqual(sheet.values, before)
                self.assertEqual(self.recorded(section)["activity"], [])
        self.worksheet.assert_not_called()
        self.assert_no_writes()

    def test_duplicate_casefolded_identity_fails_closed(self):
        before = self.item()
        sheet = self.sheets["games"]
        sheet.values.append(sheet.values[1][:])
        sheet.set("Profile", "EXAMPLE PROFILE", row=2)
        sheet.set("Title", "EXAMPLE GAME", row=2)
        self.assert_api(self.get("/media/games/data"), 409)
        self.assert_api(self.detail(), 409)
        self.assert_api(self.change(item=before, rating=4), 409)
        self.assert_no_writes()
        self.assertEqual(self.recorded()["activity"], [])

    def test_detail_returns_recorded_changes_and_original_timestamps(self):
        self.assert_api(self.change(rating=4))
        detail = self.assert_api(self.detail())
        self.assertEqual(detail["item"], self.item())
        self.assertIsNone(detail["history_warning"])
        self.assertEqual(detail["pending_count"], 0)
        self.assertEqual(len(detail["activity"]), 1)
        self.assertEqual(detail["activity"][0]["changes"], {"rating": {"before": None, "after": 4}})
        self.assertEqual(detail["activity"][0]["occurred_at"], "2026-09-19T12:00:00+00:00")
        self.assertEqual(detail["item"]["date_started"], "2024-01-02")

    def test_fresh_refresh_reconciles_applied_write_with_lost_response_once(self):
        sheet = self.sheets["games"]
        original = deepcopy(sheet.values)
        sheet.fail_after = "all"
        response = self.change(rating=7, notes="Example recovered notes")
        self.assert_api(response, 503)
        self.assertNotIn("Synthetic worksheet", response.text)
        self.assertEqual(self.recorded(), {"activity": [], "runs": [], "pending_count": 1})
        self.assertEqual(self.sql("SELECT state FROM media_operations"), [("uncertain",)])
        self.assertEqual(self.workspace._undo, {})
        self.reads.side_effect = lambda section, *, force=False: deepcopy(self.sheets[section].values if force else original)
        self.assertIsNone(self.item()["rating"])
        self.reads.reset_mock()
        refreshed = self.assert_api(self.post("/media/games/refresh"))
        self.assertTrue(self.reads.call_args.kwargs["force"])
        self.assertEqual(refreshed["items"][0]["rating"], 7)
        self.assertEqual(refreshed["items"][0]["notes"], "Example recovered notes")
        self.assertIsNone(refreshed["history_warning"])
        recorded = self.recorded()
        self.assertEqual(recorded["pending_count"], 0)
        self.assertEqual(len(recorded["activity"]), 1)
        self.assertEqual(recorded["activity"][0]["kind"], "update")
        self.assertEqual(recorded["activity"][0]["changes"]["rating"], {"before": None, "after": 7})
        self.assertEqual(self.sql("SELECT state FROM media_operations"), [("confirmed",)])
        self.assert_api(self.post("/media/games/refresh"))
        self.assertEqual(self.recorded(), recorded)
        self.assertEqual(len(sheet.batches), 1)

    def test_partial_ambiguous_write_releases_reservation_without_fake_event(self):
        sheet = self.sheets["games"]
        sheet.fail_after = 1
        self.assert_api(self.change(status="completed", rating=8), 503)
        self.assertEqual(self.recorded(), {"activity": [], "runs": [], "pending_count": 1})
        refreshed = self.assert_api(self.post("/media/games/refresh"))
        self.assertTrue(refreshed["history_warning"])
        self.assertEqual(refreshed["items"][0]["status"], "completed")
        self.assertIsNone(refreshed["items"][0]["rating"])
        self.assertEqual(self.sql("SELECT state FROM media_operations"), [("unconfirmed",)])
        self.assertEqual(self.recorded(), {"activity": [], "runs": [], "pending_count": 1})
        detail = self.assert_api(self.detail())
        self.assertTrue(detail["history_warning"])
        self.assertEqual(detail["activity"], [])
        self.assertEqual(detail["runs"], [])
        sheet.fail_after = None
        self.assert_api(self.change(notes="Example subsequent change"))
        self.assertEqual(len(sheet.batches), 2)
        detail = self.assert_api(self.detail())
        self.assertTrue(detail["history_warning"])
        self.assertEqual(len(detail["activity"]), 1)
        self.assertEqual(detail["activity"][0]["changes"], {
            "notes": {"before": "Example notes", "after": "Example subsequent change"},
        })

    def test_silent_provider_no_write_is_not_confirmed(self):
        sheet = self.sheets["games"]
        before = deepcopy(sheet.values)
        sheet.ignore_writes = True
        self.assert_api(self.change(status="completed"), 503)
        self.assertEqual(sheet.values, before)
        self.assertEqual(self.recorded(), {"activity": [], "runs": [], "pending_count": 1})
        self.assertTrue(self.assert_api(self.post("/media/games/refresh"))["history_warning"])
        self.assertEqual(self.sql("SELECT state FROM media_operations"), [("unconfirmed",)])
        self.assertEqual(self.recorded()["activity"], [])
        sheet.ignore_writes = False
        self.assert_api(self.change(rating=6))
        self.assertEqual(len(self.recorded()["activity"]), 1)

    def test_failed_history_prepare_prevents_sheet_write_and_false_completion(self):
        self.recorded()
        self.sql("""CREATE TRIGGER synthetic_prepare_failure BEFORE INSERT ON media_operations
                    BEGIN SELECT RAISE(ABORT, 'Synthetic local storage failure'); END""")
        response = self.change(status="completed")
        self.assert_api(response, 503)
        self.assertNotIn("Synthetic local storage", response.text)
        self.assertNotIn(str(self.root), response.text)
        self.assert_no_writes()
        self.assertEqual(self.item()["status"], "backlog")
        self.assertEqual(self.recorded(), {"activity": [], "runs": [], "pending_count": 0})

    def test_failed_history_confirmation_rolls_back_and_remains_recoverable(self):
        self.recorded()
        self.sql("""CREATE TRIGGER synthetic_finish_failure BEFORE INSERT ON media_events
                    BEGIN SELECT RAISE(ABORT, 'Synthetic local storage failure'); END""")
        response = self.change(status="completed")
        saved = self.assert_api(response)
        self.assertEqual(saved["item"]["status"], "completed")
        self.assertTrue(saved["history_warning"])
        self.assertIsNone(saved["undo_token"])
        self.assertNotIn("Synthetic local storage", response.text)
        self.assertEqual(self.recorded(), {"activity": [], "runs": [], "pending_count": 1})
        self.assertEqual(self.sql("SELECT state FROM media_operations"), [("pending",)])
        detail = self.assert_api(self.detail())
        self.assertTrue(detail["history_warning"])
        self.assertEqual(detail["activity"], [])
        self.assertEqual(detail["runs"], [])
        self.assertTrue(self.assert_api(self.post("/media/games/refresh"))["history_warning"])
        self.assert_api(self.change(rating=9), 409)
        self.assertEqual(len(self.sheets["games"].batches), 1)
        self.sql("DROP TRIGGER synthetic_finish_failure")
        self.assertIsNone(self.assert_api(self.post("/media/games/refresh"))["history_warning"])
        recorded = self.recorded()
        self.assertEqual(recorded["pending_count"], 0)
        self.assertEqual(len(recorded["activity"]), 1)
        self.assertEqual(len(recorded["runs"]), 1)
        self.assertEqual(recorded["runs"][0]["status"], "completed")
        self.assert_api(self.post("/media/games/refresh"))
        self.assertEqual(self.recorded(), recorded)

    def test_unavailable_history_detail_warns_without_leaking_paths(self):
        with patch.dict(os.environ, {"LUIGI_WEB_MEDIA_DB": str(self.root)}):
            response = self.detail()
            detail = self.assert_api(response)
            self.assertEqual(detail["item"]["title"], "Example game")
            self.assertEqual(detail["activity"], [])
            self.assertEqual(detail["runs"], [])
            self.assertTrue(detail["history_warning"])
            self.assertNotIn(str(self.root), response.text)
            self.assertTrue(self.assert_api(self.post("/media/games/refresh"))["history_warning"])
        self.assert_no_writes()

    def test_noop_does_not_create_history_warning(self):
        for section in ("games", "shows"):
            with self.subTest(section=section):
                before = self.item(section)
                saved = self.assert_api(self.change(section, priority=3))
                self.assertEqual(saved["item"], before)
                self.assertIsNone(saved["undo_token"])
                self.assertIsNone(saved["history_warning"])
                self.assert_no_writes()
                detail = self.assert_api(self.detail(section))
                self.assertEqual(detail["activity"], [])
                self.assertEqual(detail["runs"], [])
                self.assertIsNone(detail["history_warning"])
                self.assertEqual(detail["pending_count"], 0)
                self.assertIsNone(self.assert_api(self.post(f"/media/{section}/refresh"))["history_warning"])

    def test_cached_steam_get_and_legacy_get_never_refresh_or_mutate(self):
        snapshot = self.refresh_steam()
        before = deepcopy(self.sheets["games"].values)
        self.fetch_steam.reset_mock()
        with patch.object(self.steam, "refresh", wraps=self.steam.refresh) as refresh:
            response = self.get("/media/games/steam", profile="Example profile", title="Example game")
            self.assertEqual(self.assert_api(response), snapshot)
            legacy = self.get("/gnw/games/steam-stats", profile="Example profile", title="Example game", app_id="999")
            self.assertEqual(legacy.status_code, 200, legacy.text)
            self.assertIn("Example game", legacy.text)
            refresh.assert_not_called()
        self.fetch_steam.assert_not_called()
        self.legacy_steam.assert_not_called()
        self.assertEqual(self.sheets["games"].values, before)
        self.assert_no_writes()
        self.assertFalse(self.database.exists())

    def test_steam_cache_miss_get_is_read_only(self):
        modern = self.assert_api(self.get("/media/games/steam", profile="Example profile", title="Example game"))
        self.assertIsNone(modern["snapshot"])
        self.assertIsNone(modern["snapshot_id"])
        self.assertTrue(modern["stale"])
        response = self.get("/gnw/games/steam-stats", profile="Example profile", title="Example game", app_id="123")
        self.assertEqual(response.status_code, 200, response.text)
        self.fetch_steam.assert_not_called()
        self.legacy_steam.assert_not_called()
        self.assert_no_writes()

    def test_explicit_steam_refresh_saves_known_hours_including_zero_only_on_save(self):
        for hours in (0, 14.5):
            with self.subTest(hours=hours):
                before = self.item()
                batches = len(self.sheets["games"].batches)
                snapshot = self.refresh_steam(hours)
                self.assertEqual(snapshot["snapshot"]["hours_played"], hours)
                self.assertEqual(len(self.sheets["games"].batches), batches)
                self.assertEqual(self.item(), before)
                saved = self.assert_api(self.post("/media/games/steam/save", profile=before["profile"],
                    title=before["title"], expected_version=before["version"],
                    snapshot_id=snapshot["snapshot_id"], hours_played="900"))
                self.assertEqual(float(saved["item"]["hours_played"]), hours)
                self.assertEqual(saved["item"]["status"], before["status"])
                self.assertEqual(saved["item"]["date_started"], before["date_started"])
                self.assertEqual(saved["item"]["date_completed"], before["date_completed"])
                self.assertEqual(len(self.sheets["games"].batches), batches + 1)
                self.assertTrue(saved["undo_token"])
                self.assertIsNone(saved["history_warning"])
                self.assertEqual(self.recorded()["activity"][0]["kind"], "steam_save")
        self.assertEqual(len(self.recorded()["activity"]), 2)
        self.assertEqual(self.fetch_steam.call_count, 2)
        self.legacy_steam.assert_not_called()

    def test_unknown_steam_hours_cannot_save_as_zero(self):
        before = self.item()
        snapshot = self.refresh_steam(None)
        self.assertTrue(snapshot["snapshot"]["playtime_unavailable"])
        self.assertIsNone(snapshot["snapshot"]["hours_played"])
        self.assert_api(self.post("/media/games/steam/save", profile=before["profile"], title=before["title"],
            expected_version=before["version"], snapshot_id=snapshot["snapshot_id"]), 503)
        self.assertEqual(self.item(), before)
        self.assert_no_writes()
        self.assertEqual(self.recorded()["activity"], [])

    def test_steam_snapshot_cannot_be_forged_expired_or_used_for_another_profile(self):
        with patch.object(self.steam, "monotonic", return_value=10):
            snapshot = self.refresh_steam()
            item = self.item()
            base = {"profile": item["profile"], "title": item["title"],
                    "expected_version": item["version"], "snapshot_id": snapshot["snapshot_id"]}
            self.assert_api(self.post("/media/games/steam/save", **{**base, "snapshot_id": "forged"}), 503)
            self.sheets["games"].append(**{
                "Profile": "Other example profile", "Title": "Example game", "Status": "backlog",
                "Priority": "3", "Source": "steam", "External ID": "123",
            })
            other = self.get("/media/games/data", profile="Other example profile").json()["items"][0]
            self.assert_api(self.post("/media/games/steam/save", **{
                **base, "profile": other["profile"], "expected_version": other["version"],
            }), 503)
        with patch.object(self.steam, "monotonic", return_value=611):
            self.assert_api(self.post("/media/games/steam/save", **base), 503)
        self.assert_no_writes()
        self.assertEqual(self.recorded()["activity"], [])

    def test_steam_save_requires_current_row_version(self):
        item = self.item()
        snapshot = self.refresh_steam()
        self.sheets["games"].set("Notes", "Example external change")
        self.assert_api(self.post("/media/games/steam/save", profile=item["profile"], title=item["title"],
            expected_version=item["version"], snapshot_id=snapshot["snapshot_id"]), 409)
        self.assert_no_writes()
        self.assertEqual(self.recorded()["activity"], [])

    def test_steam_refresh_error_and_nonsteam_item_fail_without_sheet_writes(self):
        self.fetch_steam.side_effect = RuntimeError("Synthetic provider detail")
        response = self.post("/media/games/steam/refresh", profile="Example profile", title="Example game")
        self.assert_api(response, 503)
        self.assertNotIn("Synthetic provider detail", response.text)
        self.sheets["games"].set("Source", "manual")
        self.assert_api(self.get("/media/games/steam", profile="Example profile", title="Example game"), 422)
        self.assert_api(self.post("/media/games/steam/refresh", profile="Example profile", title="Example game"), 422)
        self.assert_no_writes()

    def test_picker_limits_candidates_to_filtered_keys_profile_and_recent_scope(self):
        sheet = self.sheets["games"]
        sheet.append(**{"Profile": "Example profile", "Title": "Other example game", "Priority": "3", "Status": "backlog"})
        sheet.append(**{"Profile": "Other example profile", "Title": "Example game", "Priority": "3", "Status": "backlog"})
        items = self.assert_api(self.get("/media/games/data"))["items"]
        selected = next(item for item in items if item["title"] == "Other example game")
        foreign = next(item for item in items if item["profile"] == "Other example profile")
        show = self.item("shows")
        keys = json.dumps([selected["key"], foreign["key"], show["key"], "0" * 64])
        fields = {"profile": "Example profile", "keys": keys, "exclude_recent": "1"}
        picked = self.assert_api(self.post("/media/games/pick", **fields))
        self.assertEqual(picked["item"]["key"], selected["key"])
        self.assertIsNone(self.assert_api(self.post("/media/games/pick", **fields))["item"])
        allowed = self.assert_api(self.post("/media/games/pick", **{**fields, "exclude_recent": "0"}))
        self.assertEqual(allowed["item"]["key"], selected["key"])
        foreign_only = self.assert_api(self.post("/media/games/pick", profile="Example profile", keys=json.dumps([foreign["key"]])))
        self.assertIsNone(foreign_only["item"])
        self.assertIsNone(self.assert_api(self.post("/media/games/pick", keys="[]"))["item"])
        self.assertEqual(set(self.workspace._picks), {selected["key"]})
        self.assert_no_writes()
        self.assertFalse(self.database.exists())

    def test_picker_rejects_malformed_keys_without_mutating_recent_state(self):
        for raw in ("[", "{}", "[1]", '["forged"]', '["' + "g" * 64 + '"]',
                    json.dumps(["0" * 64] * 10001), " " * 700001):
            with self.subTest(length=len(raw)):
                self.assert_api(self.post("/media/games/pick", keys=raw), 422)
        self.assertEqual(self.workspace._picks, {})
        self.assert_no_writes()

    def test_legacy_bad_rating_strings_return_422_without_clearing_saved_rating(self):
        for section in ("games", "shows"):
            sheet = self.sheets[section]
            sheet.set("Rating", "7")
            original = deepcopy(sheet.values)
            item = self.item(section)
            for rating in ("not-a-rating", "8.5", "NaN", "Infinity"):
                with self.subTest(section=section, rating=rating):
                    response = self.post(f"/gnw/{section}/update", profile=item["profile"],
                                         title=item["title"], rating=rating)
                    self.assertEqual(self.assert_api(response, 422), {
                        "detail": "Invalid media request or unavailable field",
                    })
                    self.assertNotIn(rating, response.text)
                    self.assertEqual(sheet.values, original)
                    self.assertEqual(self.item(section)["rating"], 7)
                    self.assertEqual(self.recorded(section), {
                        "activity": [], "runs": [], "pending_count": 0,
                    })
        self.assert_no_writes()

    def test_legacy_bad_priority_and_status_return_safe_422_without_writes(self):
        for section in ("games", "shows"):
            item = self.item(section)
            original = deepcopy(self.sheets[section].values)
            invalid_status = "watching" if section == "games" else "playing"
            cases = [("update", "priority", value) for value in ("", "0", "6", "2.5", "not-a-priority")]
            cases += [(action, "status", value) for action in ("status", "update")
                      for value in ("", "not-a-status", invalid_status)]
            for action, field, value in cases:
                with self.subTest(section=section, action=action, field=field, value=value):
                    response = self.post(f"/gnw/{section}/{action}", profile=item["profile"],
                                         title=item["title"], **{field: value})
                    self.assertEqual(self.assert_api(response, 422), {
                        "detail": "Invalid media request or unavailable field",
                    })
                    self.assertNotIn("HX-Trigger", response.headers)
                    self.assertNotIn("HX-Refresh", response.headers)
                    self.assertEqual(self.sheets[section].values, original)
                    self.assertEqual(self.item(section), item)
                    self.assertEqual(self.recorded(section), {
                        "activity": [], "runs": [], "pending_count": 0,
                    })
        self.assert_no_writes()

    def test_legacy_valid_status_and_update_have_confirmed_local_history(self):
        for section in ("games", "shows"):
            cases = (
                ("status", {"status": "completed"}, {"status": "completed", "date_completed": "2026-09-19"}),
                ("update", {"rating": "8", "priority": "4", "notes": "Example legacy save"},
                 {"rating": 8, "priority": 4, "notes": "Example legacy save"}),
            )
            for event_count, (action, fields, expected) in enumerate(cases, start=1):
                with self.subTest(section=section, action=action):
                    before = self.item(section)
                    response = self.post(f"/gnw/{section}/{action}", profile=before["profile"],
                                         title=before["title"], **fields)
                    self.assertEqual(response.status_code, 204, response.text)
                    self.assertEqual(response.content, b"")
                    self.assertEqual(response.headers["HX-Refresh"], "true")
                    self.assertTrue(json.loads(response.headers["HX-Trigger"])["flashSuccess"]["message"])
                    saved = self.item(section)
                    for field, value in expected.items():
                        self.assertEqual(saved[field], value)
                    self.assertNotEqual(saved["version"], before["version"])
                    self.assertEqual(saved["date_started"], before["date_started"])
                    recorded = self.recorded(section)
                    self.assertEqual(recorded["pending_count"], 0)
                    self.assertEqual(len(recorded["activity"]), event_count)
                    event = recorded["activity"][0]
                    self.assertEqual(event["kind"], "update")
                    self.assertEqual(event["occurred_at"], "2026-09-19T12:00:00+00:00")
                    self.assertEqual(event["changes"], {
                        field: {"before": before[field], "after": value} for field, value in expected.items()
                    })
                    self.assertEqual(self.sql(
                        "SELECT media_operations.state FROM media_operations JOIN media_events "
                        "ON media_events.operation_id = media_operations.operation_id WHERE media_events.id = ?",
                        (event["id"],),
                    ), [("confirmed",)])
                    self.assertEqual(len(self.sheets[section].batches), event_count)
        self.assertEqual(self.database.parent, self.root)
        self.assertTrue(self.database.is_file())

    def test_legacy_edit_form_stale_expected_version_returns_409_without_overwrite(self):
        for section in ("games", "shows"):
            for action in ("update", "status"):
                with self.subTest(section=section, action=action):
                    before = self.item(section)
                    form = self.get(f"/gnw/{section}/edit", profile=before["profile"], title=before["title"])
                    self.assertEqual(form.status_code, 200, form.text)
                    self.assertIn(f'hx-post="/gnw/{section}/update"', form.text)
                    version = re.search(r'<input\b[^>]*name="expected_version"[^>]*value="([a-f0-9]{64})"', form.text)
                    self.assertIsNotNone(version)
                    self.assertEqual(version[1], before["version"])
                    self.sheets[section].set("Notes", f"Example external {action} edit")
                    self.sheets[section].set("Rating", "9")
                    external = deepcopy(self.sheets[section].values)
                    response = self.post(f"/gnw/{section}/{action}", profile=before["profile"],
                                         title=before["title"], expected_version=version[1],
                                         status="completed", rating="2", notes="Example stale edit")
                    self.assertEqual(self.assert_api(response, 409), {
                        "detail": "Media changed or a save is unresolved. Refresh before another change.",
                    })
                    self.assertNotIn("HX-Trigger", response.headers)
                    self.assertNotIn("HX-Refresh", response.headers)
                    self.assertEqual(self.sheets[section].values, external)
                    self.assertEqual(self.item(section)["rating"], 9)
                    self.assertEqual(self.item(section)["notes"], f"Example external {action} edit")
        self.assert_no_writes()
        self.assertFalse(self.database.exists())

    def test_legacy_explicit_status_changes_retain_original_completion_dates(self):
        started = "2024-01-02T09:10:11+00:00"
        completed = "2024-02-03T12:13:14+00:00"
        for section in ("games", "shows"):
            sheet = self.sheets[section]
            sheet.set("Date Started", started)
            sheet.set("Date Completed", completed)
            active = self.service.ACTIVE_STATUS[section]
            for action in ("status", "update"):
                for status in ("completed", active):
                    with self.subTest(section=section, action=action, status=status):
                        before = self.item(section)
                        response = self.post(f"/gnw/{section}/{action}", profile=before["profile"],
                                             title=before["title"], expected_version=before["version"], status=status)
                        self.assertEqual(response.status_code, 204, response.text)
                        saved = self.item(section)
                        self.assertEqual(saved["status"], status)
                        self.assertEqual(saved["date_started"], started)
                        self.assertEqual(saved["date_completed"], completed)
                        self.assertEqual(sheet.values[1][sheet.values[0].index("Date Started")], started)
                        self.assertEqual(sheet.values[1][sheet.values[0].index("Date Completed")], completed)
                        recorded = self.recorded(section)
                        self.assertEqual(recorded["pending_count"], 0)
                        self.assertEqual(recorded["activity"][0]["changes"], {
                            "status": {"before": before["status"], "after": status},
                        })
                        baseline = [run for run in recorded["runs"] if run["origin"] == "legacy_snapshot"]
                        self.assertEqual(len(baseline), 1)
                        self.assertEqual(baseline[0]["started_at"], started)
                        self.assertEqual(baseline[0]["completed_at"], completed)

    def test_data_get_invalid_section_does_not_access_engine_or_media_storage(self):
        from luigi_web.modules.tasks import repository

        with patch.object(repository, "get_engine", side_effect=AssertionError("Live database forbidden")) as engine:
            with patch.object(self.service, "library_state", side_effect=AssertionError("Media storage forbidden")) as storage:
                response = self.get("/media/invalid-section/data")
                self.assertEqual(self.assert_api(response, 404), {"detail": "Unknown media section"})
                engine.assert_not_called()
                storage.assert_not_called()
        self.reads.assert_not_called()
        self.worksheet.assert_not_called()
        self.fetch_steam.assert_not_called()
        self.assert_no_writes()
        self.assertFalse(self.database.exists())

    def test_insights_renders_actual_confirmed_30_day_counts_excluding_undo(self):
        for section in ("games", "shows"):
            with self.subTest(section=section):
                sheet = self.sheets[section]
                sheet.set("Status", "completed")
                sheet.set("Date Completed", "2024-02-03T12:13:14+00:00")
                old_progress = {"hours_played": 6.5} if section == "games" else {"current_episode": 4}
                progress = {"hours_played": 10.5} if section == "games" else {"current_episode": 6}
                undone_progress = {"hours_played": 99.5} if section == "games" else {"current_episode": 99}
                with patch.object(self.history.clock, "local_now", return_value=datetime(2026, 8, 20, 11, 59, 59, tzinfo=timezone.utc)):
                    self.assert_api(self.change(section, rating=6, **old_progress))
                    self.assert_api(self.change(section, "runs"))
                    self.assert_api(self.change(section, status="completed"))
                with patch.object(self.history.clock, "local_now", return_value=datetime(2026, 8, 20, 12, tzinfo=timezone.utc)):
                    self.assert_api(self.change(section, "runs"))
                self.assert_api(self.change(section, **progress))
                undone = self.assert_api(self.change(section, status="completed", rating=10, **undone_progress))
                self.assert_api(self.post(f"/media/{section}/undo", token=undone["undo_token"]))
                item = self.item(section)
                response = self.post(f"/gnw/{section}/status", profile=item["profile"], title=item["title"],
                                     expected_version=item["version"], status="completed")
                self.assertEqual(response.status_code, 204, response.text)
                sheet.ignore_writes = True
                try:
                    self.assert_api(self.change(section, rating=9), 503)
                finally:
                    sheet.ignore_writes = False
                recorded = self.recorded(section)
                self.assertEqual(len(recorded["activity"]), 8)
                self.assertEqual(sum(event["kind"] == "undo" for event in recorded["activity"]), 1)
                self.assertEqual(recorded["pending_count"], 1)
                expected = {
                    "completed_runs": 3, "recorded_completed_runs": 2, "legacy_completed_runs": 1,
                    "confirmed_changes": 6, "pending_count": 1,
                    "last_30_days": {"completed_runs": 1, "confirmed_changes": 3,
                                     "hours_added": 4.0 if section == "games" else 0.0,
                                     "episodes_added": 6 if section == "shows" else 0},
                }
                with patch.object(self.host.templates, "TemplateResponse", wraps=self.host.templates.TemplateResponse) as render:
                    response = self.get("/media/insights", section=section, profile="Example profile")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    template, context = render.call_args.args
                    self.assertEqual(template, "media_insights.html")
                    self.assertEqual(context["recorded_insights"], expected)
                    self.assertEqual(context["insights"]["total"], 1)
                    self.assertIsNone(context["history_error"])
                    self.assertIsNone(context["disabled_reason"])
                activity = re.search(r'<section\b[^>]*aria-labelledby="recorded-activity-title"[^>]*>(.*?)</section>',
                                     response.text, re.DOTALL)
                self.assertIsNotNone(activity)
                metrics = {"Completed runs": 1, "Confirmed changes": 3}
                metrics.update({"Hours added": 4.0} if section == "games" else {"Episodes added": 6})
                for label, value in metrics.items():
                    self.assertIn(f"<div><span>{label}</span><strong>{value}</strong></div>", activity[1])
                totals = {"Web-recorded completed runs": 2, "Legacy completed baseline (not timed activity)": 1,
                          "Total completed runs, including baseline": 3, "Confirmed web changes": 6,
                          "Unconfirmed operations": 1}
                for label, value in totals.items():
                    self.assertIn(f"<tr><td>{label}</td><td>{value}</td></tr>", activity[1])
        self.assertEqual(self.database.parent, self.root)
        self.assertTrue(self.database.is_file())

    def test_insights_history_read_failure_renders_safe_warning_and_keeps_sheet_summary(self):
        private_error = f"Synthetic private history failure at {self.root / 'private-history.sqlite3'}"
        for section in ("games", "shows"):
            with self.subTest(section=section):
                with patch.object(self.history, "insights", side_effect=RuntimeError(private_error)) as history_read:
                    with patch.object(self.host.templates, "TemplateResponse", wraps=self.host.templates.TemplateResponse) as render:
                        response = self.get("/media/insights", section=section, profile="Example profile")
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.headers["cache-control"], "no-store")
                        template, context = render.call_args.args
                        self.assertEqual(template, "media_insights.html")
                        self.assertIsNone(context["recorded_insights"])
                        self.assertEqual(context["history_error"], "Recorded web activity is unavailable")
                        self.assertIsNone(context["disabled_reason"])
                        self.assertEqual(context["insights"]["total"], 1)
                        self.assertNotIn(private_error, repr(context))
                    history_read.assert_called_once_with(section, "Example profile")
                self.assertIn('<p class="empty" role="status">Recorded activity is temporarily unavailable.</p>', response.text)
                self.assertIn('<div><span>Total</span><strong>1</strong></div>', response.text)
                self.assertNotIn('aria-label="Recorded activity totals"', response.text)
                self.assertNotIn('aria-label="Recorded web activity in the last 30 days"', response.text)
                self.assertNotIn("Synthetic private history failure", response.text)
                self.assertNotIn(str(self.root), response.text)
                self.assertNotIn("private-history.sqlite3", response.text)
                self.assertNotIn("RuntimeError", response.text)
        self.assert_no_writes()
        self.assertFalse(self.database.exists())


if __name__ == "__main__":
    unittest.main()