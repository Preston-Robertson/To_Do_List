"""Offline regression checks for core Luigi Web behavior.

These tests intentionally avoid PostgreSQL, Google, and an LLM endpoint so they
can run on every checkout. Live integration checks still belong in deployment
health/diagnostic flows.
"""
from __future__ import annotations

import json
import os
import unittest
from datetime import date
from unittest.mock import MagicMock, Mock, patch

from starlette.requests import Request

import app
import auth
import db
import gnw
import llm


class AuthTests(unittest.TestCase):
    def test_bearer_header_fails_closed(self) -> None:
        with patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "expected"}):
            self.assertFalse(auth.is_authenticated(None, "Bearer ", None))
            self.assertFalse(auth.is_authenticated(None, "Bearer wrong", None))
            self.assertTrue(auth.is_authenticated(None, "Bearer expected", None))


class DatabaseHelperTests(unittest.TestCase):
    def test_database_url_preserves_reserved_password_characters(self) -> None:
        env = {
            "LUIGI_WEB_PG_HOST": "db.local",
            "LUIGI_WEB_PG_PORT": "5432",
            "LUIGI_WEB_PG_DB": "luigi_todo",
            "LUIGI_WEB_PG_USER": "luigi",
            "LUIGI_WEB_PG_PASSWORD": "p@ss:/word",
        }
        with patch.dict(os.environ, env):
            url = db._dsn()
        self.assertEqual(url.password, "p@ss:/word")
        self.assertEqual(url.host, "db.local")

    def test_recurring_weekdays_are_normalized(self) -> None:
        self.assertEqual(db.parse_recurring_days(["4", "0", "4", "9"]), "0,4")

    def test_weekdays_take_priority_over_interval(self) -> None:
        row = {
            "completed": 1,
            "recurring": 1,
            "completed_time": "2026-08-13T10:00:00",
            "recurring_days": "0,4",
            "recurring_interval": 30,
        }
        self.assertEqual(db.reactivation_date(row), "2026-08-14")

    def test_streak_refresh_failure_does_not_fail_completion(self) -> None:
        class Result:
            def first(self):
                return (1,)

        class Connection:
            def execute(self, statement, params=None):
                return Result()

        class Transaction:
            def __enter__(self):
                return Connection()

            def __exit__(self, exc_type, exc, traceback):
                return False

        class Engine:
            def begin(self):
                return Transaction()

        with (
            patch.object(db, "get_engine", return_value=Engine()),
            patch.object(
                db,
                "_refresh_discipline_streak",
                side_effect=PermissionError("read-only streak column"),
            ) as refresh,
        ):
            # Must return normally: this derived-value failure happens after
            # the separate completion transaction has already committed.
            db._try_refresh_discipline_streak("Gym")
        refresh.assert_called_once()


class RecurringFormTests(unittest.TestCase):
    def test_enabled_recurrence_requires_a_schedule(self) -> None:
        app._validate_recurring_form({"recurring": "1", "recurring_interval": "7"})
        app._validate_recurring_form({"recurring": "1", "recurring_days": ["0", "4"]})
        with self.assertRaisesRegex(Exception, "Choose at least one weekday"):
            app._validate_recurring_form({"recurring": "1", "recurring_interval": ""})


class GameAndWatchTests(unittest.TestCase):
    def test_game_board_has_paused_and_achievement_sections(self) -> None:
        self.assertIn("paused", gnw.GAME_STATUSES)
        self.assertIn("achievements", gnw.GAME_STATUSES)
        self.assertEqual(gnw.STATUS_LABELS["achievements"], "100% Achievements")

    def test_steam_store_metadata_is_normalized(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "10": {"success": True, "data": {
                "name": "Counter-Strike", "header_image": "cover.jpg",
                "developers": ["Valve"], "genres": [{"description": "Action"}],
                "categories": [{"id": 1}], "release_date": {"date": "Nov 1, 2000"},
                "price_overview": {"final_formatted": "$9.99"},
            }}
        }
        client = MagicMock()
        client.__enter__.return_value.get.return_value = response
        with patch.object(gnw.httpx, "Client", return_value=client):
            result = gnw._steam_lookup("10")
        assert result is not None
        self.assertEqual(result["title"], "Counter-Strike")
        self.assertTrue(result["is_multiplayer"])
        self.assertEqual(result["source"], "steam")

    def test_catalog_game_is_written_by_header_name(self) -> None:
        headers = ["Title", "Profile", "Status", "Priority", "Cover URL", "External ID", "Source"]
        worksheet = Mock()
        with (
            patch.object(gnw, "_all_values", return_value=[headers]),
            patch.object(gnw, "_ws", return_value=worksheet),
            patch.object(gnw, "_invalidate"),
        ):
            ok, title = gnw.add_catalog_item(
                "games", "Preston",
                {"title": "Portal", "source": "steam", "external_id": "400", "cover_url": "cover"},
                status="backlog", priority=4,
            )
        self.assertTrue(ok)
        self.assertEqual(title, "Portal")
        written, cell_range = worksheet.update.call_args.args
        self.assertEqual(cell_range, "A2:G2")
        self.assertEqual(written[0][headers.index("Profile")], "Preston")
        self.assertEqual(written[0][headers.index("Title")], "Portal")

    def test_full_sheet_url_is_accepted(self) -> None:
        url = "https://docs.google.com/spreadsheets/d/abc_123-X/edit?gid=0"
        self.assertEqual(gnw._normalize_sheet_id(url), "abc_123-X")

    def test_reordered_trimmed_headers_are_supported(self) -> None:
        headers = [" Title ", "Priority", " Profile ", "Status"]
        item = gnw._row_to_item(
            "games", headers, ["Example", "5", "Preston", "playing"]
        )
        self.assertEqual(item["title"], "Example")
        self.assertEqual(item["profile"], "Preston")
        self.assertEqual(item["priority"], 5)
        self.assertEqual(item["status"], "playing")


class LlmTests(unittest.TestCase):
    def tearDown(self) -> None:
        llm.reset_history("unit-test")

    def test_history_is_bounded_at_complete_turn(self) -> None:
        messages = [{"role": "system", "content": "system"}]
        for turn in range(40):
            messages.extend(
                [
                    {"role": "user", "content": f"u{turn}"},
                    {"role": "assistant", "content": f"a{turn}"},
                ]
            )
        llm.append_history("unit-test", messages)
        history = llm.get_history("unit-test")
        self.assertLessEqual(len(history), 64)
        self.assertEqual(history[0]["role"], "system")
        self.assertEqual(history[1]["role"], "user")

    def test_live_tool_cap_stops_a_looping_provider(self) -> None:
        class LoopProvider:
            name = "fake"
            model = "loop"

            def __init__(self) -> None:
                self.calls = 0

            def chat_completion(self, messages, tools=None):
                self.calls += 1
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": str(self.calls),
                            "type": "function",
                            "function": {
                                "name": "missing",
                                "arguments": json.dumps({}),
                            },
                        }
                    ],
                }

        provider = LoopProvider()
        with patch.dict(os.environ, {"LUIGI_WEB_LLM_MAX_TOOL_ITERATIONS": "2"}):
            result = llm.run_chat_with_tools(
                provider,
                [{"role": "user", "content": "loop"}],
                {},
            )
        self.assertEqual(provider.calls, 2)
        self.assertIn("tool-call cap", result.reply)


class GanttTests(unittest.TestCase):
    def test_scheduled_and_unscheduled_tasks_are_shaped(self) -> None:
        rows = [
            {
                "uuid": "1",
                "task": "Scheduled",
                "status": "Not Started",
                "priority": 2,
                "catagory": "Work",
                "task_creation": "2026-08-01",
                "start_time": None,
                "due_date": "2026-08-20",
                "source": "task",
            },
            {
                "uuid": "2",
                "task": "Unscheduled",
                "status": "Not Started",
                "priority": 1,
                "catagory": "Work",
                "task_creation": date.today().isoformat(),
                "start_time": None,
                "due_date": None,
                "source": "task",
            },
        ]
        chart = app._build_gantt(rows)
        self.assertIsNotNone(chart)
        assert chart is not None
        self.assertEqual(chart["scheduled_count"], 1)
        self.assertEqual(len(chart["unscheduled"]), 1)

    def test_projects_select_all_projects_by_default(self) -> None:
        request = Request({"type": "http", "method": "GET", "path": "/projects",
                           "query_string": b"", "headers": []})
        projects = [{"project": "Kitchen", "n": 2}, {"project": "Launch", "n": 1}]
        with (
            patch.object(app, "_require_v2"),
            patch.object(db, "list_projects_with_open_tasks", return_value=projects),
            patch.object(db, "project_grouping_enabled", return_value=True),
            patch.object(db, "list_project_rows", return_value=[]) as list_rows,
        ):
            response = app.projects_page(request)
        list_rows.assert_called_once_with(["Kitchen", "Launch"], include_recurring=True)
        self.assertEqual(response.context["selected_projects"], {"Kitchen", "Launch"})

    def test_calendar_builds_month_grid(self) -> None:
        request = Request({"type": "http", "method": "GET", "path": "/calendar",
                           "query_string": b"", "headers": []})
        with (
            patch.object(app, "_require_v2"),
            patch.object(app, "_reactivate_recurring"),
            patch.object(db, "list_calendar_rows", return_value=[]),
        ):
            response = app.calendar_page(request, "2026-08")
        self.assertEqual(response.context["month_label"], "August 2026")
        self.assertGreaterEqual(len(response.context["weeks"]), 5)
        self.assertTrue(all(len(week) == 7 for week in response.context["weeks"]))


class ConsolidatedTasksTests(unittest.TestCase):
    def test_tasks_page_includes_one_off_and_recurring_rows(self) -> None:
        request = Request({"type": "http", "method": "GET", "path": "/tasks",
                           "query_string": b"", "headers": []})
        task = {"uuid": "task-1", "task": "One-off", "priority": 1,
                "status": "Not Started", "completed": 0, "due_date": None}
        recurring = {"uuid": "rec-1", "task": "Recurring", "priority": 2,
                     "status": "Not Started", "completed": 0, "due_date": None,
                     "recurring": 1}
        with (
            patch.object(app, "_require_v2"),
            patch.object(app, "_reactivate_recurring"),
            patch.object(db, "list_tasks", return_value=[task]),
            patch.object(db, "list_recurring", return_value=[recurring]),
        ):
            response = app.tasks_page(request)
        cards = response.context["columns"]["Not Started"]
        self.assertEqual({row["uuid"] for row in cards}, {"task-1", "rec-1"})
        endpoints = {row["uuid"]: row["_endpoint_root"] for row in cards}
        self.assertEqual(endpoints, {"task-1": "/tasks", "rec-1": "/recurring"})
        self.assertTrue(response.context["consolidated"])


if __name__ == "__main__":
    unittest.main()
