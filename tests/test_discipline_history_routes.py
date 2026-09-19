"""Authenticated history routes backed by synthetic SQLite records."""
from datetime import date
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from luigi_web.modules.discipline import history, routes


class DisciplineHistoryRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "LUIGI_WEB_DATA_DIR": temporary, "LUIGI_WEB_MODULES": "discipline",
            "LUIGI_WEB_UI_TOKEN": "synthetic-history-token", "LUIGI_WEB_TIMEZONE": "America/New_York",
        }))
        from luigi_web import application as host

        self.host = host
        self.enterContext(patch.object(host, "_require_v2"))
        self.enterContext(patch.object(history.clock, "local_today", return_value=date(2030, 1, 2)))
        self.enterContext(patch.object(history.db, "_try_refresh_discipline_streak"))
        self.enterContext(patch.dict(history._undo, clear=True))
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        self.enterContext(patch.object(history.db, "get_engine", return_value=self.engine))
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE discipline_list (uuid TEXT PRIMARY KEY, task TEXT, catagory TEXT,
                frequency_per_week INTEGER, active INTEGER, current_streak INTEGER)
            """))
            connection.execute(text("INSERT INTO discipline_list VALUES ('example-id', 'Example habit', 'Example category', 3, 1, 0)"))
            connection.execute(text("""
                CREATE TABLE discipline_completions (task TEXT, catagory TEXT, completed_date TEXT, logged_at TEXT)
            """))
            connection.execute(text("""
                INSERT INTO discipline_completions VALUES ('Example habit', 'Example category', :day, :stamp)
            """), [
                {"day": "2029-12-31", "stamp": "2030-01-01T02:00:00"},
                {"day": "2030-01-01", "stamp": "2030-01-01T15:00:00Z"},
            ])
        app = FastAPI()
        app.middleware("http")(host.csrf_middleware)
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer synthetic-history-token"}
        self.url = "/discipline/example-id/history"

    def state(self, year=2030):
        response = self.client.get(self.url + f"/data?year={year}", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    def change(self, day="2030-01-02", action="mark", expected=200, **overrides):
        state = self.state()
        record = next((row for row in state["completions"] if row["date"] == day), None)
        response = self.client.post(self.url, headers=self.headers, data={
            "day": day, "action": action, "year": "2030",
            "expected_version": record["version"] if record else state["emptyVersion"], **overrides,
        })
        self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response

    def undo(self, token, expected=200):
        response = self.client.post(self.url + "/undo", headers=self.headers, data={"token": token, "year": "2030"})
        self.assertEqual(response.status_code, expected, response.text)
        return response

    def test_year_records_local_logged_at_and_cross_year_current_week(self):
        state = self.state(2029)
        self.assertEqual(state["weekly"]["count"], 2)
        self.assertEqual(state["weekly"]["week_start"], "2029-12-31")
        self.assertEqual(state["completions"][0]["loggedAt"], "2029-12-31T21:00:00-05:00")
        self.assertNotIn("completedAt", state["completions"][0])
        self.assertEqual(state["year"], 2029)
        self.assertEqual(len(state["completions"]), 1)
        self.assertEqual(state["name"], "Example habit")

    def test_page_renders_live_template_without_example_sandbox(self):
        response = self.client.get(self.url, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn('id="dh-live"', response.text)
        self.assertIn("/module-assets/discipline/history.js", response.text)
        self.assertNotIn("history-example.js", response.text)

    def test_mark_updates_weekly_and_undo_restores(self):
        result = self.change().json()
        self.assertEqual(result["state"]["weekly"]["count"], 3)
        self.assertTrue(result["state"]["weekly"]["target_met"])
        self.assertEqual(result["undo_ttl_ms"], 12000)
        self.assertIsNotNone(result["undo_token"])
        restored = self.undo(result["undo_token"]).json()["state"]
        self.assertEqual(restored["weekly"]["count"], 2)
        self.undo(result["undo_token"], expected=409)

    def test_remove_undo_keeps_exact_original_logged_timestamp(self):
        before = self.state()
        token = self.change("2030-01-01", "unmark").json()["undo_token"]
        self.assertEqual(self.state()["completions"], [])
        self.assertEqual(self.undo(token).json()["state"], before)

    def test_duplicate_mark_has_no_new_timestamp_or_undo(self):
        before = self.state()
        result = self.change("2030-01-01").json()
        self.assertIsNone(result["undo_token"])
        self.assertEqual(result["state"], before)

    def test_stale_versions_and_invalid_inputs_never_save(self):
        before = self.state()
        self.change("2030-01-01", expected=409, expected_version=before["emptyVersion"])
        for day in ("2030-01-03", "2030-02-30", "20300102", "2029-12-31"):
            self.change(day, expected=422)
        self.change(action="toggle", expected=400)
        self.change(expected=400, expected_version="bad")
        self.assertEqual(self.state(), before)

    def test_paused_today_is_readable_but_only_past_mutations_allowed(self):
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE discipline_list SET active = 0"))
        self.assertFalse(self.state()["active"])
        self.change(expected=409)
        self.change("2030-01-01", "unmark")

    def test_undo_expiry_and_different_uuid_cannot_write(self):
        token = self.change().json()["undo_token"]
        response = self.client.post("/discipline/other-example/history/undo", headers=self.headers, data={"token": token})
        self.assertEqual(response.status_code, 409)
        with patch.object(history.time, "monotonic", return_value=history._undo[token]["expires"] + 1):
            self.undo(token, expected=409)
        self.assertEqual(self.state()["weekly"]["count"], 3)

    def test_undo_conflicts_with_newer_logged_value(self):
        token = self.change().json()["undo_token"]
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE discipline_completions SET logged_at = '2030-01-02T11:00:00Z' WHERE completed_date = '2030-01-02'"))
        self.undo(token, expected=409)
        self.assertEqual(self.state()["weekly"]["count"], 3)

    def test_auth_and_cookie_csrf(self):
        for path in (self.url, self.url + "/data"):
            self.assertEqual(self.client.get(path).status_code, 401)
        for path in (self.url, self.url + "/undo"):
            self.assertEqual(self.client.post(path).status_code, 401)
        self.client.cookies.set("luigi_session", "synthetic-history-token")
        self.client.cookies.set("luigi_csrf", "synthetic-csrf")
        for path in (self.url, self.url + "/undo"):
            self.assertEqual(self.client.post(path).status_code, 403)
        response = self.client.post(self.url, headers={"X-CSRF-Token": "synthetic-csrf"}, data={
            "day": "2030-01-02", "action": "mark", "year": "2030", "expected_version": history.db.discipline_history_version([]),
        })
        self.assertEqual(response.status_code, 200)

    def test_generic_storage_errors_no_success_state(self):
        with patch.object(history.db, "list_discipline_history", side_effect=RuntimeError("private detail")):
            response = self.client.get(self.url + "/data", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Discipline history is unavailable"})
        state = self.state()
        with patch.object(history.db, "change_discipline_history", side_effect=RuntimeError("private detail")):
            response = self.client.post(self.url, headers=self.headers, data={
                "day": "2030-01-02", "year": "2030", "action": "mark", "expected_version": state["emptyVersion"],
            })
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private detail", response.text)
        self.assertNotIn("state", response.json())

    def test_missing_invalid_year_and_missing_timestamp(self):
        self.assertEqual(self.client.get("/discipline/missing/history", headers=self.headers).status_code, 404)
        for year in ("1899", "2032", "invalid", "2030.5"):
            self.assertEqual(self.client.get(self.url + f"/data?year={year}", headers=self.headers).status_code, 422)
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE discipline_completions SET logged_at = NULL"))
        self.assertIsNone(self.state()["completions"][0]["loggedAt"])