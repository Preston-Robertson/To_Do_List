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
from unittest.mock import patch

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


class RecurringFormTests(unittest.TestCase):
    def test_enabled_recurrence_requires_a_schedule(self) -> None:
        app._validate_recurring_form({"recurring": "1", "recurring_interval": "7"})
        app._validate_recurring_form({"recurring": "1", "recurring_days": ["0", "4"]})
        with self.assertRaisesRegex(Exception, "Choose at least one weekday"):
            app._validate_recurring_form({"recurring": "1", "recurring_interval": ""})


class GameAndWatchTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
