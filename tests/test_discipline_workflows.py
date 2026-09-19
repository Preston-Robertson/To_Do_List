"""Synthetic current-week and pause/resume workflow regressions."""
from __future__ import annotations

from datetime import date
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

from luigi_web.modules.discipline import routes, service


def discipline_row(**overrides):
    return {
        "uuid": "example-discipline",
        "task": "Example habit",
        "catagory": "Example category",
        "frequency_per_week": 3,
        "active": 1,
        "current_streak": 99,
        **overrides,
    }


class DisciplineProgressServiceTests(unittest.TestCase):
    def setUp(self):
        self.weekly = self.enterContext(patch.object(
            service.db, "list_discipline_completions_between",
            return_value={"Example habit": {"2030-04-01", "2030-04-03"}},
        ))
        self.today_tasks = self.enterContext(patch.object(
            service.db, "list_completion_tasks_for_day", return_value=set(),
        ))
        self.streak = self.enterContext(patch.object(
            service.db, "computed_discipline_streak", return_value=8,
        ))

    def test_current_target_and_exact_public_fields(self):
        result = service.discipline_progress([discipline_row()], date(2030, 4, 5))
        self.assertEqual(result, {
            "today": "2030-04-05", "week_start": "2030-04-01", "week_end": "2030-04-07",
            "disciplines": [{
                "uuid": "example-discipline", "active": True, "today_done": False,
                "daily_streak": None,
                "weekly": {"count": 2, "target": 3, "remaining": 1, "target_met": False,
                           "week_start": "2030-04-01", "week_end": "2030-04-07"},
            }],
        })
        self.weekly.assert_called_once_with(date(2030, 4, 1), date(2030, 4, 7))
        self.today_tasks.assert_called_once_with("2030-04-05")
        self.streak.assert_not_called()

    def test_normalized_legacy_names_merge_unique_days(self):
        self.weekly.return_value = {
            "Example habit": {"2030-04-01"},
            "  EXAMPLE   habit ": {"2030-04-01", "2030-04-03"},
        }
        self.today_tasks.return_value = {"example\tHABIT  "}
        result = service.discipline_progress([discipline_row()], date(2030, 4, 3))["disciplines"][0]
        self.assertEqual(result["weekly"]["count"], 2)
        self.assertTrue(result["today_done"])

    def test_week_spans_december_and_january(self):
        self.weekly.return_value = {"Example habit": {"2029-12-31", "2030-01-01", "2030-01-02"}}
        result = service.discipline_progress([discipline_row()], date(2030, 1, 2))
        self.weekly.assert_called_once_with(date(2029, 12, 31), date(2030, 1, 6))
        self.assertEqual(result["week_start"], "2029-12-31")
        self.assertEqual(result["disciplines"][0]["weekly"]["count"], 3)
        self.assertTrue(result["disciplines"][0]["weekly"]["target_met"])

    def test_selected_history_year_does_not_control_progress(self):
        row = discipline_row(_year_days={"2020-01-01"}, frequency_per_week=2)
        result = service.discipline_progress([row], date(2030, 4, 5))["disciplines"][0]
        self.assertEqual(result["weekly"]["count"], 2)
        self.assertEqual(result["weekly"]["target"], 2)
        self.assertTrue(result["weekly"]["target_met"])
        self.assertEqual(row["_year_days"], {"2020-01-01"})
        self.assertNotIn("_weekly", row)

    def test_paused_progress_and_completed_today_are_retained(self):
        self.today_tasks.return_value = {"Example habit"}
        result = service.discipline_progress([discipline_row(active=0)], date(2030, 4, 3))["disciplines"][0]
        self.assertFalse(result["active"])
        self.assertTrue(result["today_done"])
        self.assertEqual(result["weekly"]["count"], 2)

    def test_only_daily_target_reads_all_time_streak(self):
        result = service.discipline_progress([
            discipline_row(frequency_per_week=7),
            discipline_row(uuid="weekly-example", task="Weekly example", frequency_per_week=4),
        ], date(2030, 4, 5))["disciplines"]
        self.assertEqual(result[0]["daily_streak"], 8)
        self.assertIsNone(result[1]["daily_streak"])
        self.streak.assert_called_once_with("Example habit")

    def test_invalid_future_and_outside_week_days_do_not_count(self):
        self.weekly.return_value = {"Example habit": {
            "invalid", "2030-02-30", "2030-03-31", "2030-04-01", "2030-04-06", "2030-04-08",
        }}
        result = service.discipline_progress([discipline_row()], date(2030, 4, 5))["disciplines"][0]
        self.assertEqual(result["weekly"]["count"], 1)
        self.assertEqual(result["weekly"]["remaining"], 2)

    def test_weekly_read_failure_does_not_report_zero(self):
        self.weekly.side_effect = RuntimeError("synthetic storage failure")
        with self.assertRaises(RuntimeError):
            service.discipline_progress([discipline_row()], date(2030, 4, 5))
        self.streak.assert_not_called()


class DisciplineRepositoryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        self.addCleanup(self.engine.dispose)
        self.enterContext(patch.object(service.db, "get_engine", return_value=self.engine))
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE discipline_list (
                    uuid TEXT PRIMARY KEY, task TEXT, catagory TEXT,
                    frequency_per_week INTEGER, active INTEGER, current_streak INTEGER
                )
            """))
            connection.execute(text("""
                CREATE TABLE discipline_completions (
                    task TEXT, catagory TEXT, completed_date TEXT, logged_at TEXT
                )
            """))
            connection.execute(text("""
                INSERT INTO discipline_list
                VALUES (:uuid, :task, :catagory, :frequency_per_week, :active, :current_streak)
            """), discipline_row())
            connection.execute(text("""
                INSERT INTO discipline_completions
                VALUES ('Example habit', 'Example category', :day, '2030-01-02T12:00:00')
            """), [{"day": day} for day in (
                "2029-12-30", "2029-12-31", "2030-01-01", "2030-01-01",
                "2030-01-06T12:00:00", "2030-01-07",
            )])

    def history(self):
        with self.engine.connect() as connection:
            return connection.execute(text("SELECT * FROM discipline_completions")).all()

    def test_bounded_cross_year_query_is_one_parameterized_read(self):
        statements = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append((statement, parameters))

        event.listen(self.engine, "before_cursor_execute", capture)
        result = service.db.list_discipline_completions_between(date(2029, 12, 31), date(2030, 1, 6))
        self.assertEqual(result, {"Example habit": {"2029-12-31", "2030-01-01", "2030-01-06"}})
        self.assertEqual(len(statements), 1)
        self.assertNotIn("2029-12-31", statements[0][0])
        self.assertEqual(statements[0][1], ("2029-12-31", "2030-01-07"))

    def test_pause_resume_are_idempotent_and_preserve_every_other_field(self):
        original = service.db.get_discipline("example-discipline")
        history = self.history()
        for active in (False, False, True, True):
            with self.subTest(active=active):
                self.assertTrue(service.db.set_discipline_active("example-discipline", active))
                self.assertEqual(service.db.get_discipline("example-discipline"), {**original, "active": int(active)})
                self.assertEqual(self.history(), history)

    def test_unknown_uuid_returns_false_without_changing_records(self):
        original = service.db.get_discipline("example-discipline")
        history = self.history()
        self.assertFalse(service.db.set_discipline_active("missing-example", False))
        self.assertEqual(service.db.get_discipline("example-discipline"), original)
        self.assertEqual(self.history(), history)

    def test_failed_readback_rolls_back_trigger_side_effects(self):
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TRIGGER reject_pause AFTER UPDATE OF active ON discipline_list
                WHEN NEW.active = 0
                BEGIN
                    UPDATE discipline_list SET active = 1, frequency_per_week = 1
                    WHERE uuid = NEW.uuid;
                END
            """))
        original = service.db.get_discipline("example-discipline")
        with self.assertRaisesRegex(RuntimeError, "could not be verified"):
            service.db.set_discipline_active("example-discipline", False)
        self.assertEqual(service.db.get_discipline("example-discipline"), original)

    def test_failed_commit_is_not_success(self):
        def reject_commit(connection):
            raise RuntimeError("synthetic commit failure")

        event.listen(self.engine, "commit", reject_commit)
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic commit failure"):
                service.db.set_discipline_active("example-discipline", False)
        finally:
            event.remove(self.engine, "commit", reject_commit)
        self.assertEqual(service.db.get_discipline("example-discipline")["active"], 1)

    def test_bounded_query_failure_propagates(self):
        with patch.object(self.engine, "connect", side_effect=RuntimeError("synthetic read failure")):
            with self.assertRaises(RuntimeError):
                service.db.list_discipline_completions_between(date(2029, 12, 31), date(2030, 1, 6))

    def test_daily_streak_reads_across_year_boundary_and_ignores_cached_streak(self):
        self.enterContext(patch.object(service.db.clock, "local_today", return_value=date(2030, 1, 2)))
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE discipline_list SET frequency_per_week = 7"))
        rows = service.db.list_disciplines(include_inactive=True)
        state = service.discipline_progress(rows, date(2030, 1, 2))["disciplines"][0]
        self.assertEqual(state["daily_streak"], 3)
        self.assertEqual(state["weekly"]["count"], 2)
        self.assertEqual(state["weekly"]["target"], 7)
        self.assertEqual(state["weekly"]["remaining"], 5)
        self.assertEqual(rows[0]["current_streak"], 99)

    def test_pause_history_correction_resume_and_today_round_trip(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "LUIGI_WEB_DATA_DIR": temporary,
            "LUIGI_WEB_MODULES": "tasks,discipline",
            "LUIGI_WEB_UI_TOKEN": "synthetic-discipline-token",
        }))
        from luigi_web import application as host

        self.enterContext(patch.object(host, "_require_v2"))
        self.enterContext(patch.object(host.clock, "local_today", return_value=date(2030, 1, 2)))
        self.enterContext(patch.object(host.templates, "TemplateResponse", return_value=HTMLResponse("synthetic cell")))
        application = FastAPI()
        application.middleware("http")(host.csrf_middleware)
        application.include_router(routes.router)
        client = TestClient(application)
        self.addCleanup(client.close)
        headers = {"Authorization": "Bearer synthetic-discipline-token"}
        original = service.db.get_discipline("example-discipline")
        history = self.history()

        response = client.post("/discipline/example-discipline/deactivate", headers=headers)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(service.db.get_discipline("example-discipline"), {**original, "active": 0})
        self.assertEqual(self.history(), history)
        response = client.post("/discipline/example-discipline/today", headers=headers, data={"action": "mark"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.history(), history)
        paused = client.get("/discipline/progress", headers=headers).json()["disciplines"][0]
        self.assertFalse(paused["active"])
        self.assertEqual(paused["weekly"]["count"], 2)

        response = client.post("/discipline/toggle", headers=headers, data={
            "discipline_uuid": "example-discipline", "day": "2030-01-01", "action": "unmark",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(service.db.completion_exists("Example habit", "2030-01-01"))
        corrected = client.get("/discipline/progress", headers=headers).json()["disciplines"][0]
        self.assertEqual(corrected["weekly"]["count"], 1)
        response = client.post("/discipline/toggle", headers=headers, data={
            "discipline_uuid": "example-discipline", "day": "2030-01-01", "action": "mark",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(service.db.completion_exists("Example habit", "2030-01-01"))
        restored_history = self.history()
        response = client.post("/discipline/example-discipline/resume", headers=headers)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.history(), restored_history)
        resumed = client.get("/discipline/progress", headers=headers).json()["disciplines"][0]
        self.assertTrue(resumed["active"])
        self.assertEqual(resumed["weekly"]["count"], 2)
        self.assertFalse(resumed["today_done"])
        self.assertEqual(resumed["weekly"]["target"], 3)

        response = client.post("/discipline/example-discipline/today", headers=headers, data={"action": "mark"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["marked"])
        completed = client.get("/discipline/progress", headers=headers).json()["disciplines"][0]
        self.assertTrue(completed["today_done"])
        self.assertEqual(completed["weekly"]["count"], 3)
        self.assertTrue(completed["weekly"]["target_met"])
        response = client.post("/discipline/example-discipline/today", headers=headers, data={"action": "unmark"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["marked"])
        cleared = client.get("/discipline/progress", headers=headers).json()["disciplines"][0]
        self.assertFalse(cleared["today_done"])
        self.assertEqual(cleared["weekly"]["count"], 2)
        self.assertEqual(self.history(), restored_history)


class DisciplineRouteWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "LUIGI_WEB_DATA_DIR": temporary,
            "LUIGI_WEB_MODULES": "tasks,discipline",
            "LUIGI_WEB_UI_TOKEN": "synthetic-discipline-token",
        }))
        self.enterContext(patch.object(service.db, "get_engine", side_effect=AssertionError("Live storage forbidden")))
        from luigi_web import application as host

        self.host = host
        self.schema = self.enterContext(patch.object(host, "_require_v2"))
        self.enterContext(patch.object(host.clock, "local_today", return_value=date(2030, 1, 2)))
        self.enterContext(patch.object(host, "_available_years", return_value=[2020, 2029, 2030]))
        self.rows = self.enterContext(patch.object(service.db, "list_disciplines", return_value=[discipline_row()]))
        self.get_row = self.enterContext(patch.object(service.db, "get_discipline", return_value=discipline_row()))
        self.year = self.enterContext(patch.object(service.db, "list_completions_for_year", return_value={"Example habit": {"2020-02-03"}}))
        self.weekly = self.enterContext(patch.object(service.db, "list_discipline_completions_between", return_value={"Example habit": {"2029-12-31", "2030-01-01"}}))
        self.today_tasks = self.enterContext(patch.object(service.db, "list_completion_tasks_for_day", return_value={"  EXAMPLE   habit "}))
        self.streak = self.enterContext(patch.object(service.db, "computed_discipline_streak", return_value=8))
        self.set_active = self.enterContext(patch.object(service.db, "set_discipline_active", return_value=True))
        self.mark = self.enterContext(patch.object(service.db, "mark_completion", return_value=True))
        self.unmark = self.enterContext(patch.object(service.db, "unmark_completion", return_value=True))
        self.exists = self.enterContext(patch.object(service.db, "completion_exists", return_value=True))
        self.update = self.enterContext(patch.object(service.db, "update_discipline"))
        self.template = self.enterContext(patch.object(host.templates, "TemplateResponse", return_value=HTMLResponse("synthetic rendered page")))
        application = FastAPI()
        application.middleware("http")(host.csrf_middleware)
        application.include_router(routes.router)
        self.client = TestClient(application)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer synthetic-discipline-token"}

    def test_progress_contract_is_authenticated_current_week_and_no_store(self):
        response = self.client.get("/discipline/progress", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json(), {
            "today": "2030-01-02", "week_start": "2029-12-31", "week_end": "2030-01-06",
            "disciplines": [{
                "uuid": "example-discipline", "active": True, "today_done": True, "daily_streak": None,
                "weekly": {"count": 2, "target": 3, "remaining": 1, "target_met": False,
                           "week_start": "2029-12-31", "week_end": "2030-01-06"},
            }],
        })
        self.rows.assert_called_once_with(include_inactive=True)
        self.schema.assert_called_once()
        self.assertNotIn("Example habit", response.text)
        self.year.assert_not_called()

    def test_progress_route_precedes_dynamic_routes(self):
        paths = [route.path for route in routes.router.routes]
        self.assertLess(paths.index("/discipline/progress"), paths.index("/discipline/{row_uuid}/edit"))
        self.assertLess(paths.index("/discipline/toggle"), paths.index("/discipline/{row_uuid}"))

    def test_unauthenticated_progress_and_mutations_never_touch_storage(self):
        self.assertEqual(self.client.get("/discipline/progress").status_code, 401)
        for action in ("deactivate", "resume"):
            self.assertEqual(self.client.post(f"/discipline/example-discipline/{action}").status_code, 401)
        self.rows.assert_not_called()
        self.get_row.assert_not_called()
        self.set_active.assert_not_called()

    def test_schema_gate_blocks_new_routes(self):
        self.schema.side_effect = HTTPException(503, "Shared task storage unavailable")
        self.assertEqual(self.client.get("/discipline/progress", headers=self.headers).status_code, 503)
        for action in ("deactivate", "resume"):
            self.assertEqual(self.client.post(f"/discipline/example-discipline/{action}", headers=self.headers).status_code, 503)
        self.rows.assert_not_called()
        self.get_row.assert_not_called()
        self.set_active.assert_not_called()

    def test_progress_failure_is_generic_not_a_zero_snapshot(self):
        self.weekly.side_effect = RuntimeError("synthetic internal detail")
        response = self.client.get("/discipline/progress", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json(), {"detail": "Discipline progress is unavailable"})
        self.assertNotIn("HX-Trigger", response.headers)

    def test_page_keeps_selected_year_heatmap_and_current_progress(self):
        self.rows.return_value = [discipline_row(), discipline_row(uuid="daily-example", frequency_per_week=7)]
        response = self.client.get("/discipline?year=2020", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.year.assert_called_once_with(2020)
        self.weekly.assert_called_once_with(date(2029, 12, 31), date(2030, 1, 6))
        context = self.template.call_args.args[1]
        weekly_row, daily_row = context["disciplines"]
        self.assertEqual(weekly_row["_year_days"], {"2020-02-03"})
        self.assertTrue(weekly_row["_today_done"])
        self.assertEqual(weekly_row["_weekly"]["count"], 2)
        self.assertEqual(weekly_row["_streak"], 0)
        self.assertEqual(daily_row["_streak"], 8)
        self.streak.assert_called_once_with("Example habit")
        self.today_tasks.assert_called_once_with("2030-01-02")
        self.assertEqual(context["grid"], routes._year_grid(2020))
        self.assertIsNone(context["progress_error"])

    def test_page_weekly_failure_preserves_heatmap_today_and_daily_streak(self):
        self.rows.return_value = [discipline_row(frequency_per_week=7)]
        self.weekly.side_effect = RuntimeError("synthetic internal detail")
        response = self.client.get("/discipline?year=2020", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        context = self.template.call_args.args[1]
        row = context["disciplines"][0]
        self.assertIsNone(row["_weekly"])
        self.assertEqual(context["progress_error"], "Weekly progress is unavailable")
        self.assertEqual(row["_year_days"], {"2020-02-03"})
        self.assertTrue(row["_today_done"])
        self.assertEqual(row["_streak"], 8)

    def test_page_history_failure_has_only_generic_detail(self):
        self.year.side_effect = RuntimeError("synthetic internal detail")
        response = self.client.get("/discipline", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Discipline history is unavailable"})
        self.template.assert_not_called()

    def test_pause_and_resume_verify_active_only_and_refresh(self):
        for action, active, message in (("deactivate", False, "Discipline paused"), ("resume", True, "Discipline resumed")):
            with self.subTest(action=action):
                self.set_active.reset_mock()
                self.get_row.reset_mock()
                response = self.client.post(f"/discipline/example-discipline/{action}", headers=self.headers,
                                            data={"task": "Ignored replacement", "frequency_per_week": "1"})
                self.assertEqual(response.status_code, 204)
                self.assertEqual(response.headers["HX-Refresh"], "true")
                self.assertEqual(json.loads(response.headers["HX-Trigger"]), {"flashSuccess": {"message": message}})
                self.get_row.assert_called_once_with("example-discipline")
                self.set_active.assert_called_once_with("example-discipline", active)
                self.update.assert_not_called()
                self.mark.assert_not_called()
                self.unmark.assert_not_called()

    def test_pause_resume_missing_uuid_is_404(self):
        self.get_row.return_value = None
        for action in ("deactivate", "resume"):
            response = self.client.post(f"/discipline/missing-example/{action}", headers=self.headers)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("HX-Refresh", response.headers)
        self.set_active.assert_not_called()

    def test_pause_resume_failures_never_emit_success(self):
        for action in ("deactivate", "resume"):
            for fail_at in ("lookup", "write", "verification"):
                with self.subTest(action=action, fail_at=fail_at):
                    self.get_row.side_effect = RuntimeError("synthetic internal detail") if fail_at == "lookup" else None
                    self.set_active.side_effect = RuntimeError("synthetic internal detail") if fail_at == "write" else None
                    self.set_active.return_value = fail_at != "verification"
                    response = self.client.post(f"/discipline/example-discipline/{action}", headers=self.headers)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json(), {"detail": "Discipline state could not be saved"})
                    self.assertNotIn("HX-Refresh", response.headers)
                    self.assertNotIn("HX-Trigger", response.headers)

    def test_cookie_mutations_require_csrf_bearer_remains_supported(self):
        from luigi_web import auth

        self.client.cookies.set(auth.COOKIE_NAME, "synthetic-discipline-token")
        self.client.cookies.set(auth.CSRF_COOKIE_NAME, "synthetic-csrf-token")
        for action in ("deactivate", "resume"):
            self.set_active.reset_mock()
            response = self.client.post(f"/discipline/example-discipline/{action}")
            self.assertEqual(response.status_code, 403)
            self.set_active.assert_not_called()
            response = self.client.post(f"/discipline/example-discipline/{action}", headers={"X-CSRF-Token": "synthetic-csrf-token"})
            self.assertEqual(response.status_code, 204)
            response = self.client.post(f"/discipline/example-discipline/{action}", headers=self.headers)
            self.assertEqual(response.status_code, 204)

    def test_paused_today_endpoint_blocks_both_actions(self):
        self.get_row.return_value = discipline_row(active=0)
        for action in ("mark", "unmark"):
            response = self.client.post("/discipline/example-discipline/today", headers=self.headers, data={"action": action})
            self.assertEqual(response.status_code, 409)
        self.mark.assert_not_called()
        self.unmark.assert_not_called()

    def test_active_future_or_invalid_toggle_does_not_write(self):
        self.get_row.return_value = discipline_row(active=1)
        for day, action, expected in (("2030-01-03", "mark", 422), ("20300102", "mark", 400), ("2030-01-02", "invalid", 400)):
            with self.subTest(day=day, action=action):
                response = self.client.post("/discipline/toggle", headers=self.headers, data={
                    "discipline_uuid": "example-discipline", "day": day, "action": action,
                })
                self.assertEqual(response.status_code, expected)
                self.assertNotIn("HX-Trigger-After-Swap", response.headers)
        self.mark.assert_not_called()
        self.unmark.assert_not_called()

    def test_confirmed_heatmap_update_emits_progress_event_after_swap(self):
        response = self.client.post("/discipline/toggle", headers=self.headers, data={
            "discipline_uuid": "example-discipline", "day": "2030-01-01", "action": "mark",
        })
        self.assertEqual(response.status_code, 200)
        headers = self.template.call_args.kwargs["headers"]
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(headers["HX-Trigger-After-Swap"]), {
            "luigi:discipline-updated": {"uuid": "example-discipline", "day": "2030-01-01", "marked": True},
        })

    def test_page_supplies_current_week_labels(self):
        response = self.client.get("/discipline?year=2020", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        context = self.template.call_args.args[1]
        self.assertEqual(context["week_start"], "2029-12-31")
        self.assertEqual(context["week_end"], "2030-01-06")
        self.assertEqual(self.template.call_args.kwargs["headers"]["Cache-Control"], "no-store")

    def test_paused_heatmap_today_or_future_blocked_with_uuid_or_legacy_name(self):
        self.get_row.return_value = discipline_row(active=0)
        self.rows.return_value = [discipline_row(active=0)]
        for row_uuid in ("example-discipline", ""):
            for day in ("2030-01-02", "2030-01-03"):
                response = self.client.post("/discipline/toggle", headers=self.headers, data={
                    "discipline_uuid": row_uuid, "task": " EXAMPLE   habit ", "day": day, "action": "mark",
                })
                self.assertEqual(response.status_code, 409)
        self.mark.assert_not_called()
        self.unmark.assert_not_called()
        self.update.assert_not_called()

    def test_paused_past_heatmap_correction_reaches_existing_write_workflow(self):
        self.get_row.return_value = discipline_row(active=0)
        for action in ("mark", "unmark"):
            response = self.client.post("/discipline/toggle", headers=self.headers, data={
                "discipline_uuid": "example-discipline", "task": "Stale example name",
                "catagory": "Stale example category", "day": "2030-01-01", "action": action,
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.template.call_args.args[0], "partials/discipline_cell.html")
            self.assertEqual(self.template.call_args.args[1]["marked"], action == "mark")
        self.mark.assert_called_once_with("Example habit", "Example category", "2030-01-01")
        self.unmark.assert_called_once_with("Example habit", "2030-01-01")
        self.update.assert_not_called()

    def test_delete_refreshes_and_only_promises_undo_with_tasks_enabled(self):
        snapshot = {"discipline": discipline_row(), "completions": []}
        with (
            patch.object(service.db, "delete_discipline", return_value=snapshot),
            patch.object(self.host, "_module_enabled") as enabled,
            patch.object(self.host, "_stash_undo", return_value="synthetic-undo") as stash,
        ):
            for tasks_enabled in (False, True):
                enabled.return_value = tasks_enabled
                stash.reset_mock()
                response = self.client.post("/discipline/example-discipline/delete", headers=self.headers)
                self.assertEqual(response.status_code, 204)
                self.assertEqual(response.headers["HX-Refresh"], "true")
                triggers = json.loads(response.headers["HX-Trigger"])
                if tasks_enabled:
                    self.assertEqual(triggers["showUndo"]["ttl_ms"], 12000)
                    self.assertEqual(triggers["showUndo"]["op_id"], "synthetic-undo")
                    stash.assert_called_once()
                else:
                    self.assertNotIn("showUndo", triggers)
                    self.assertEqual(triggers["flashSuccess"]["message"], "Discipline deleted")
                    stash.assert_not_called()

    def test_dynamic_update_still_works_after_static_toggle_route(self):
        response = self.client.post("/discipline/example-discipline", headers=self.headers, data={"frequency_per_week": "4"})
        self.assertEqual(response.status_code, 204)
        self.update.assert_called_once_with("example-discipline", {"frequency_per_week": "4"})


if __name__ == "__main__":
    unittest.main()