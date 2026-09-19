"""Synthetic, isolated tests of the live Home contract."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, Table, Text, URL, create_engine

from luigi_web.modules.planning import home_service
from luigi_web.modules.tasks import operations, repository as db


def task_row(row_uuid: str, **fields):
    return {
        "uuid": row_uuid, "task": f"Example {row_uuid}", "priority": 2,
        "status": "Not Started", "completed": 0, "archived": 0,
        "due_date": None, "catagory": "Example category", **fields,
    }


class HomeFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {
            "LUIGI_WEB_OPERATIONS_DB": str(Path(temporary.name) / "operations.db"),
            "LUIGI_WEB_MODULES": "tasks,discipline,planning",
            "LUIGI_WEB_DATA_DIR": temporary.name,
            "LUIGI_WEB_UI_TOKEN": "synthetic-home-token",
        }))
        self.enterContext(patch.object(db, "_WEB_META_PATH", str(self.root / "metadata.json")))
        self.enterContext(patch.object(db, "get_engine", side_effect=AssertionError("Live database access forbidden")))
        self.today = date(2026, 9, 18)
        self.enterContext(patch.object(home_service.clock, "local_today", return_value=self.today))
        self.tasks = self.enterContext(patch.object(db, "list_tasks", return_value=[]))
        self.recurring = self.enterContext(patch.object(db, "list_recurring", return_value=[]))
        self.habits = self.enterContext(patch.object(db, "list_disciplines", return_value=[]))
        self.completions = self.enterContext(patch.object(db, "list_completion_tasks_for_day", return_value=set()))


class HomeStateTests(HomeFixture):
    def test_all_rows_unique_references_and_explicit_today_only(self) -> None:
        self.tasks.return_value = [task_row(str(index), due_date="2026-09-17") for index in range(30)]
        self.tasks.return_value.extend([
            task_row("29"), task_row("archived", archived=1), task_row(""),
            task_row("completed", status="Completed", completed=1),
        ])
        self.recurring.return_value = [task_row("29", due_date=self.today)]
        for row_uuid in ("29", "archived", "missing", "completed"):
            operations.set_today_selection(row_uuid, "task", True, day=self.today)
        state = home_service.load_home_state()
        tasks = {row["id"]: row for row in state["tasks"]}
        self.assertEqual(len(tasks), 32)
        self.assertEqual(len(state["tasks"]), 32)
        self.assertTrue(tasks["task:29"]["today"])
        self.assertFalse(tasks["recurring:29"]["today"])
        self.assertTrue(tasks["task:completed"]["done"])
        self.assertTrue(tasks["task:completed"]["today"])
        self.assertFalse(tasks["task:0"]["today"])
        self.assertEqual(tasks["task:0"]["priority"], 2)
        self.assertEqual(tasks["task:0"]["notes"], "")
        self.assertEqual(tasks["recurring:29"]["endpoint"], "/recurring")
        self.assertEqual(set(state), {"today", "tasks", "habits", "shortcuts", "selection_available", "selection_error"})
        self.assertEqual(set(tasks["task:0"]), {
            "id", "uuid", "source", "title", "notes", "due", "today", "priority",
            "done", "status", "category", "blockers", "endpoint",
        })

    def test_empty_today_and_date_rollover(self) -> None:
        self.tasks.return_value = [task_row("example", due_date=self.today)]
        self.assertFalse(home_service.load_home_state()["tasks"][0]["today"])
        operations.set_today_selection("example", "task", True, day=self.today)
        state = home_service.load_home_state(today=date(2026, 9, 19))
        self.assertEqual(state["today"], "2026-09-19")
        self.assertFalse(state["tasks"][0]["today"])

    def test_dates_normalized_without_time_or_invalid_values(self) -> None:
        for value, expected in (
            (None, ""), ("", ""), ("invalid", ""), ("2026-02-30", ""),
            (self.today, "2026-09-18"),
            (datetime(2026, 9, 18, 23, tzinfo=timezone.utc), "2026-09-18"),
            ("2026-09-18T23:00:00+00:00", "2026-09-18"),
        ):
            with self.subTest(value=value):
                self.assertEqual(home_service.due_iso(value), expected)

    def test_habits_use_normalized_current_day_completions(self) -> None:
        self.habits.return_value = [
            {"uuid": "habit", "task": "Example  Habit", "active": 1,
             "frequency_per_week": 3, "current_streak": 4},
            {"uuid": "inactive", "task": "Example inactive", "active": 0},
        ]
        self.completions.return_value = {"  example habit  "}
        self.assertEqual(home_service.load_home_state()["habits"], [{
            "id": "habit", "title": "Example  Habit", "done": True,
            "frequency_per_week": 3, "streak": 4,
        }])
        self.habits.assert_called_once_with(False)
        self.completions.assert_called_once_with("2026-09-18")

    def test_dependency_labels_resolve_current_source_and_completion(self) -> None:
        self.tasks.return_value = [task_row("dependent"), task_row("blocker", completed=1)]
        self.recurring.return_value = [task_row("blocker", task="Example recurring blocker")]
        for source in ("task", "recurring"):
            operations.add_dependency(
                dependent_uuid="dependent", dependent_source="task", dependent_label="Example dependent",
                blocker_uuid="blocker", blocker_source=source, blocker_label="Example old label",
            )
        state = home_service.load_home_state()
        self.assertEqual(state["tasks"][0]["blockers"], ["Example recurring blocker"])

    def test_selection_failure_is_explicit_and_does_not_hide_tasks(self) -> None:
        self.tasks.return_value = [task_row("example")]
        with patch.object(operations, "list_today_selections", side_effect=RuntimeError("synthetic internal detail")), \
             patch.object(operations, "list_dependencies", side_effect=RuntimeError("synthetic internal detail")):
            state = home_service.load_home_state()
        self.assertFalse(state["selection_available"])
        self.assertEqual(state["selection_error"], home_service.SELECTION_ERROR)
        self.assertEqual(len(state["tasks"]), 1)
        self.assertNotIn("synthetic internal detail", str(state))

    def test_selection_failure_preserves_available_blockers(self) -> None:
        self.tasks.return_value = [task_row("dependent"), task_row("blocker")]
        operations.add_dependency(
            dependent_uuid="dependent", dependent_source="task", dependent_label="Example dependent",
            blocker_uuid="blocker", blocker_source="task", blocker_label="Example blocker",
        )
        with patch.object(operations, "list_today_selections", side_effect=RuntimeError("synthetic detail")):
            state = home_service.load_home_state()
        self.assertFalse(state["selection_available"])
        self.assertEqual(state["tasks"][0]["blockers"], ["Example blocker"])


class HomeRouteTests(HomeFixture):
    def setUp(self) -> None:
        super().setUp()
        from luigi_web import application as host
        from luigi_web.modules.planning import routes

        self.host = host
        self.routes = routes
        self.enterContext(patch.dict(host._STARTUP_SCHEMA, {"version": 2, "error": None}))
        self.require_schema = self.enterContext(patch.object(host, "_require_v2", wraps=host._require_v2))
        self.reactivate = self.enterContext(patch.object(host, "_reactivate_recurring"))
        self.enterContext(patch.object(host, "_UNDO_QUEUE", {}))
        self.stash = self.enterContext(patch.object(host, "_stash_undo", wraps=host._stash_undo))
        self.records = {
            (source, "example"): task_row("example", recurring=int(source == "recurring"),
                                          recurring_interval=3 if source == "recurring" else None,
                                          recurring_days=None, recurring_month_ordinal=None,
                                          recurring_month_weekday=None)
            for source in ("task", "recurring")
        }
        self.get_task = self.enterContext(patch.object(db, "get_task", side_effect=lambda row_uuid: deepcopy(self.records.get(("task", row_uuid)))))
        self.get_recurring = self.enterContext(patch.object(db, "get_recurring", side_effect=lambda row_uuid: deepcopy(self.records.get(("recurring", row_uuid)))))
        self.update_task = self.enterContext(patch.object(db, "update_task", side_effect=lambda row_uuid, data: self.records[("task", row_uuid)].update(data)))
        self.update_recurring = self.enterContext(patch.object(db, "update_recurring", side_effect=lambda row_uuid, data: self.records[("recurring", row_uuid)].update(data)))
        self.enabled = set()
        app = FastAPI()
        app.state.modules = SimpleNamespace(
            is_enabled=lambda module: module in self.enabled,
            navigation=lambda: [], enabled=self.enabled, landing_path="/home",
        )
        app.middleware("http")(host.csrf_middleware)
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer synthetic-home-token"}

    def post(self, path: str, **fields):
        return self.client.post(path, data={"source": "task", "uuid": "example", **fields}, headers=self.headers)

    def test_data_and_page_share_state_without_legacy_queries_or_providers(self) -> None:
        self.tasks.return_value = [task_row("example", due_date=self.today)]
        self.enabled.update({"media", "cards", "characters", "finance", "assistant"})
        with patch.object(db, "list_open_tasks", side_effect=AssertionError("Legacy dashboard query")), \
             patch.object(self.host.gnw, "list_items", side_effect=AssertionError("Provider access")), \
             patch.object(self.host.templates, "TemplateResponse", return_value=HTMLResponse("synthetic home")) as render:
            data = self.client.get("/home/data", headers=self.headers)
            page = self.client.get("/home", headers=self.headers)
        self.assertEqual(data.status_code, 200)
        self.assertEqual(data.headers["cache-control"], "no-store")
        self.assertEqual(page.status_code, 200)
        context = render.call_args.args[1]
        self.assertEqual(context["home_state"], data.json())
        self.assertEqual(set(context), {"request", "active_nav", "page_title", "home_state", "today_iso", "home_date"})
        self.assertEqual(context["home_date"], "Friday, September 18")
        self.assertEqual(render.call_args.kwargs["headers"]["Cache-Control"], "no-store")
        self.assertEqual([shortcut["href"] for shortcut in data.json()["shortcuts"]], ["/games", "/cards", "/characters"])
        self.assertEqual(self.reactivate.call_count, 2)
        self.assertIs(self.host.home_page, self.routes.home_page)

    def test_disabled_shortcuts_are_absent(self) -> None:
        self.assertEqual(self.client.get("/home/data", headers=self.headers).json()["shortcuts"], [])
        self.enabled.add("characters")
        shortcuts = self.client.get("/home/data", headers=self.headers).json()["shortcuts"]
        self.assertEqual(shortcuts, [{"label": "Characters", "href": "/characters", "icon": "shield"}])

    def test_live_template_renders_synthetic_state(self) -> None:
        self.tasks.return_value = [task_row("example", task="Example <task>")]
        operations.set_today_selection("example", "task", True, day=self.today)
        response = self.client.get("/home", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("Example &lt;task&gt;", response.text)
        self.assertIn('data-home-data="/home/data"', response.text)

    def test_all_live_routes_require_authentication(self) -> None:
        for path in ("/home", "/home/data"):
            self.assertEqual(self.client.get(path).status_code, 401)
        for path in ("/home/today", "/home/reschedule"):
            self.assertEqual(self.client.post(path).status_code, 401)
        self.require_schema.assert_not_called()
        self.tasks.assert_not_called()
        self.update_task.assert_not_called()

    def test_cookie_csrf_for_both_mutations_and_bearer_bypass(self) -> None:
        self.client.cookies.set("luigi_session", "synthetic-home-token")
        self.client.get("/home/data", headers=self.headers)
        for path, fields in (
            ("/home/today", {"selected": "true", "day": "2026-09-18"}),
            ("/home/reschedule", {"due_date": "2026-09-20"}),
        ):
            payload = {"source": "task", "uuid": "example", **fields}
            for headers in ({}, {"X-CSRF-Token": "wrong"}):
                self.assertEqual(self.client.post(path, data=payload, headers=headers).status_code, 403)
            headers = {"X-CSRF-Token": self.client.cookies["luigi_csrf"]}
            self.assertEqual(self.client.post(path, data=payload, headers=headers).status_code, 200)
            self.assertEqual(self.client.post(path, data=payload, headers=self.headers).status_code, 200)

    def test_today_selection_is_verified_and_never_changes_shared_fields(self) -> None:
        before = deepcopy(self.records)
        for source in ("task", "recurring"):
            response = self.post("/home/today", source=source, selected="true", day="2026-09-18")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"selected": True, "day": "2026-09-18"})
            self.assertEqual(response.headers["cache-control"], "no-store")
        response = self.post("/home/today", selected="false", day="2026-09-18")
        self.assertEqual(response.json(), {"selected": False, "day": "2026-09-18"})
        self.assertEqual(operations.list_today_selections(self.today), {("recurring", "example")})
        self.assertEqual(self.records, before)
        self.update_task.assert_not_called()
        self.update_recurring.assert_not_called()
        self.stash.assert_not_called()

    def test_bad_dates_sources_and_stale_days_do_not_reach_database(self) -> None:
        cases = [
            ("/home/today", {"selected": "true", "day": "2026-09-17"}, 409),
            ("/home/today", {"selected": "true", "day": "2026-09-19"}, 409),
            ("/home/today", {"selected": "true", "day": "2026-09-18", "source": "finance"}, 422),
            ("/home/today", {"selected": "invalid", "day": "2026-09-18"}, 422),
            ("/home/reschedule", {"source": "cards", "due_date": "2026-09-18"}, 422),
        ]
        for invalid in ("invalid", "2026-02-30", "20260918", "2026-W38-5", "2026-09-18T12:00:00", " 2026-09-18"):
            cases.extend([
                ("/home/today", {"selected": "true", "day": invalid}, 422),
                ("/home/reschedule", {"due_date": invalid}, 422),
            ])
        for path, payload, expected in cases:
            with self.subTest(path=path, payload=payload):
                self.assertEqual(self.post(path, **payload).status_code, expected)
        self.get_task.assert_not_called()
        self.get_recurring.assert_not_called()
        self.update_task.assert_not_called()
        self.update_recurring.assert_not_called()

    def test_missing_and_archived_rows_cannot_be_mutated(self) -> None:
        self.records[("task", "archived")] = task_row("archived", archived=1)
        for row_uuid in ("missing", "archived"):
            for path, payload in (("/home/today", {"day": "2026-09-18", "selected": "true"}),
                                  ("/home/reschedule", {"due_date": "2026-09-20"})):
                self.assertEqual(self.post(path, uuid=row_uuid, **payload).status_code, 404)
        self.assertEqual(operations.list_today_selections(self.today), set())
        self.update_task.assert_not_called()

    def test_selection_write_exception_and_false_verification_never_succeed(self) -> None:
        for options in ({"side_effect": RuntimeError("synthetic internal detail")}, {"return_value": False}, {"return_value": True}):
            with self.subTest(options=options), patch.object(operations, "set_today_selection", **options):
                response = self.post("/home/today", selected="true", day="2026-09-18")
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("synthetic internal detail", response.text)
                self.assertNotIn("HX-Trigger", response.headers)

    def test_database_and_schema_failures_are_generic(self) -> None:
        self.tasks.side_effect = RuntimeError("synthetic internal detail")
        response = self.client.get("/home/data", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("synthetic internal detail", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.host._STARTUP_SCHEMA["error"] = "synthetic internal schema detail"
        for path in ("/home", "/home/data"):
            response = self.client.get(path, headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("synthetic internal schema detail", response.text)
        self.assertGreaterEqual(self.require_schema.call_count, 3)

    def test_dependency_failure_is_503_when_selections_are_available(self) -> None:
        with patch.object(operations, "list_dependencies", side_effect=RuntimeError("synthetic detail")):
            response = self.client.get("/home/data", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("synthetic detail", response.text)

    def test_selection_storage_failure_still_returns_data(self) -> None:
        self.tasks.return_value = [task_row("example")]
        with patch.object(operations, "list_today_selections", side_effect=RuntimeError("synthetic detail")):
            response = self.client.get("/home/data", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["selection_available"])
        self.assertEqual(response.json()["selection_error"], home_service.SELECTION_ERROR)
        self.assertEqual(len(response.json()["tasks"]), 1)

    def test_reschedule_absolute_date_clear_and_noop(self) -> None:
        operations.set_today_selection("example", "task", True, day=self.today)
        response = self.post("/home/reschedule", due_date="2026-09-16")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"due": "2026-09-16"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        event = json.loads(response.headers["HX-Trigger"])["showUndo"]
        self.assertEqual(event["ttl_ms"], self.host._UNDO_TTL_SECONDS * 1000)
        self.assertEqual(self.host._UNDO_QUEUE[event["op_id"]]["snapshot"]["due_date"], None)
        response = self.post("/home/reschedule", due_date="2026-09-16")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("HX-Trigger", response.headers)
        response = self.post("/home/reschedule", due_date="")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"due": ""})
        self.assertIsNone(self.records[("task", "example")]["due_date"])
        self.assertEqual(operations.list_today_selections(self.today), {("task", "example")})

    def test_failed_reschedule_has_no_undo_or_success(self) -> None:
        for options, status in (({"side_effect": RuntimeError("synthetic internal detail")}, 503),
                                ({"side_effect": None, "return_value": None}, 409)):
            with self.subTest(status=status), patch.object(db, "update_task", **options):
                response = self.post("/home/reschedule", due_date="2026-09-20")
                self.assertEqual(response.status_code, status)
                self.assertNotIn("synthetic internal detail", response.text)
                self.assertNotIn("HX-Trigger", response.headers)
        self.stash.assert_not_called()

    def test_invalid_stored_date_cannot_masquerade_as_verified_clear(self) -> None:
        self.records[("task", "example")]["due_date"] = "invalid"
        self.update_task.side_effect = None
        response = self.post("/home/reschedule", due_date="")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("HX-Trigger", response.headers)
        self.stash.assert_not_called()

    def test_reschedule_rechecks_missing_archived_and_changed_rows(self) -> None:
        before = deepcopy(self.records[("task", "example")])
        for after, expected in (
            (None, 409),
            ({**before, "due_date": "2026-09-20", "archived": 1}, 409),
            ({**before, "due_date": "2026-09-20", "completed": 1}, 409),
            (RuntimeError("synthetic detail"), 503),
        ):
            with self.subTest(after=after), patch.object(db, "get_task", side_effect=[before, after]):
                response = self.post("/home/reschedule", due_date="2026-09-20")
            self.assertEqual(response.status_code, expected)
            self.assertNotIn("HX-Trigger", response.headers)
            self.assertNotIn("synthetic detail", response.text)
        self.stash.assert_not_called()

    def test_inconsistent_nonrecurring_schedule_rejected_before_update(self) -> None:
        self.records[("task", "example")]["recurring_interval"] = 4
        response = self.post("/home/reschedule", due_date="2026-09-20")
        self.assertEqual(response.status_code, 409)
        self.update_task.assert_not_called()
        self.stash.assert_not_called()

    def test_task_read_errors_for_both_mutations_are_generic(self) -> None:
        self.get_task.side_effect = RuntimeError("synthetic detail")
        for path, payload in (("/home/today", {"day": "2026-09-18", "selected": "true"}),
                              ("/home/reschedule", {"due_date": "2026-09-20"})):
            response = self.post(path, **payload)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("synthetic detail", response.text)
        self.update_task.assert_not_called()
        self.stash.assert_not_called()


class HomeRepositoryTests(HomeFixture):
    def setUp(self) -> None:
        super().setUp()
        from luigi_web import application as host
        from luigi_web.modules.planning import home_routes
        from luigi_web.modules.tasks.routes import router as task_router

        self.host = host
        self.engine = create_engine(URL.create("sqlite", database=str(self.root / "tasks.db")))
        self.addCleanup(self.engine.dispose)
        self.enterContext(patch.object(db, "get_engine", return_value=self.engine))
        self.enterContext(patch.object(db, "_TABLES_MISSING_COLUMNS", {}))
        self.enterContext(patch.dict(host._STARTUP_SCHEMA, {"version": 2, "error": None}))
        self.enterContext(patch.object(host, "_UNDO_QUEUE", {}))
        metadata = MetaData()
        integers = {"priority", "completed", "recurring", "recurring_interval", "archived",
                    "recurring_month_ordinal", "recurring_month_weekday"}
        self.tables = {
            table: Table(table, metadata, *(
                Column(column, Integer if column in integers else Text, primary_key=column == "uuid")
                for column in db._TASK_COLUMNS
            ))
            for table in ("tasks", "recurring_tasks")
        }
        metadata.create_all(self.engine)
        app = FastAPI()
        app.middleware("http")(host.csrf_middleware)
        app.include_router(home_routes.router)
        app.include_router(task_router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer synthetic-home-token"}

    def test_real_updates_and_undo_preserve_all_other_fields_and_today(self) -> None:
        schedules = (
            {"recurring": 0},
            {"recurring": 1, "recurring_interval": 3},
            {"recurring": 1, "recurring_days": "0,2,4"},
            {"recurring": 1, "recurring_month_ordinal": 2, "recurring_month_weekday": 1},
        )
        for source, table, getter in (("task", "tasks", db.get_task),
                                      ("recurring", "recurring_tasks", db.get_recurring)):
            for index, schedule in enumerate(schedules):
                with self.subTest(source=source, schedule=schedule):
                    row_uuid = f"example-{source}-{index}"
                    snapshot = dict.fromkeys(db._TASK_COLUMNS)
                    snapshot.update(task_row(
                        row_uuid, status="Completed", completed=1, completed_time="2026-09-17T12:00:00",
                        due_date="2026-09-17", task_creation="2026-09-01", start_time="2026-09-16T10:00:00",
                        project="Example project", priority=7, relevant_link="https://example.invalid/",
                        task_group="Example group", sub_group="Example subgroup", estimated_time="2.5", logged_hours="1.5",
                        **schedule,
                    ))
                    with self.engine.begin() as connection:
                        connection.execute(self.tables[table].insert(), snapshot)
                    operations.set_today_selection(row_uuid, source, True, day=self.today)
                    operations.add_dependency(
                        dependent_uuid=row_uuid, dependent_source=source, dependent_label="Example dependent",
                        blocker_uuid="example-blocker", blocker_source="task", blocker_label="Example blocker",
                    )
                    dependencies = operations.list_dependencies()
                    before = getter(row_uuid)
                    for due in ("2026-10-01", "2026-09-15", ""):
                        response = self.client.post("/home/reschedule", data={
                            "source": source, "uuid": row_uuid, "due_date": due,
                        }, headers=self.headers)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.json(), {"due": due})
                        after = getter(row_uuid)
                        self.assertEqual(after, {**before, "due_date": due or None})
                        event = json.loads(response.headers["HX-Trigger"])["showUndo"]
                        undo_response = self.client.post(f"/undo/{event['op_id']}", headers=self.headers)
                        self.assertEqual(undo_response.status_code, 200, undo_response.text)
                        self.assertEqual(getter(row_uuid), before)
                        self.assertIn((source, row_uuid), operations.list_today_selections(self.today))
                        self.assertEqual(operations.list_dependencies(), dependencies)

    def test_blocked_task_reschedule_never_requests_a_status_transition(self) -> None:
        row = dict.fromkeys(db._TASK_COLUMNS)
        row.update(task_row("example-blocked", status="Blocked", recurring=1, recurring_interval=2))
        with self.engine.begin() as connection:
            connection.execute(self.tables["tasks"].insert(), row)
        with patch.object(db, "_set_task_like_status", side_effect=AssertionError("Status transition forbidden")), \
             patch.object(db, "_assert_task_unblocked", side_effect=AssertionError("Dependency transition forbidden")):
            response = self.client.post("/home/reschedule", data={
                "source": "task", "uuid": "example-blocked", "due_date": "2026-09-20",
            }, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(db.get_task("example-blocked"), {**row, "due_date": "2026-09-20"})


if __name__ == "__main__":
    unittest.main()