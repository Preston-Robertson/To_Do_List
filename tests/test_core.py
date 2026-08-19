"""Offline regression checks for core Luigi Web behavior.

These tests intentionally avoid PostgreSQL, Google, and an LLM endpoint so they
can run on every checkout. Live integration checks still belong in deployment
health/diagnostic flows.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from starlette.requests import Request

from luigi_web import application as app
from luigi_web import auth, clock, db, gnw, llm


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

    def test_monthly_position_reactivates_on_next_matching_date(self) -> None:
        row = {
            "completed": 1,
            "recurring": 1,
            "completed_time": "2026-08-03T10:00:00",
            "recurring_month_ordinal": 1,
            "recurring_month_weekday": 0,
        }
        self.assertEqual(db.reactivation_date(row), "2026-09-07")

    def test_reactivation_uses_effective_completion_date(self) -> None:
        row = {
            "completed": 1,
            "recurring": 1,
            "completed_time": "2026-08-19T01:00:00",
            "_effective_completion_date": "2026-08-18",
            "recurring_interval": 1,
        }
        self.assertEqual(db.reactivation_date(row), "2026-08-19")

    def test_follow_up_payload_maps_rule_and_due_offset(self) -> None:
        payload = db._follow_up_task_payload({
            "follow_up_task": "Fold Laundry",
            "subgroup": "Household",
            "priority": 2,
            "due_offset_days": 1,
        })
        self.assertEqual(payload["task"], "Fold Laundry")
        self.assertEqual(payload["sub_group"], "Household")
        self.assertEqual(
            payload["due_date"], (date.today() + timedelta(days=1)).isoformat()
        )
        self.assertEqual(payload["recurring"], 0)

    def test_completion_trigger_inserts_follow_up_task(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        task_columns = ", ".join(f"{column} TEXT" for column in db._TASK_COLUMNS)
        with engine.begin() as conn:
            conn.execute(text(f"CREATE TABLE tasks ({task_columns})"))
            conn.execute(text("""
                CREATE TABLE follow_up_tasks (
                    trigger_task TEXT, follow_up_task TEXT, catagory TEXT,
                    task_group TEXT, subgroup TEXT, relevant_link TEXT,
                    priority INTEGER, estimated_time REAL, due_offset_days INTEGER
                )
            """))
            conn.execute(text("""
                INSERT INTO follow_up_tasks
                    (trigger_task, follow_up_task, priority, due_offset_days)
                VALUES ('Do Laundry', 'Fold Laundry', 2, 0)
            """))
            created = db._spawn_follow_ups(conn, "do laundry")
            spawned = conn.execute(text(
                "SELECT uuid, task, status, due_date FROM tasks"
            )).mappings().one()

        self.assertEqual(created, [spawned["uuid"]])
        self.assertEqual(spawned["task"], "Fold Laundry")
        self.assertEqual(spawned["status"], "Not Started")
        self.assertEqual(spawned["due_date"], date.today().isoformat())
        engine.dispose()

    def test_undo_removes_generated_follow_up_task(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        task_columns = ", ".join(f"{column} TEXT" for column in db._TASK_COLUMNS)
        snapshot = {column: None for column in db._TASK_COLUMNS}
        snapshot.update({
            "uuid": "source-task", "task": "Do Laundry",
            "status": "Not Started", "completed": 0,
        })
        generated = {column: None for column in db._TASK_COLUMNS}
        generated.update({
            "uuid": "generated-task", "task": "Fold Laundry",
            "status": "Not Started", "completed": 0,
        })
        with engine.begin() as conn:
            conn.execute(text(f"CREATE TABLE tasks ({task_columns})"))
            columns = ", ".join(generated)
            bindings = ", ".join(f":{column}" for column in generated)
            conn.execute(
                text(f"INSERT INTO tasks ({columns}) VALUES ({bindings})"),
                generated,
            )
        with patch.object(db, "get_engine", return_value=engine):
            db.restore_task_row("tasks", snapshot, ["generated-task"])
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT uuid, task FROM tasks")).all()
        self.assertEqual(rows, [("source-task", "Do Laundry")])
        engine.dispose()

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

    def test_discipline_health_check_always_rolls_back(self) -> None:
        required = {
            "discipline_list": [
                "uuid", "task", "catagory", "frequency_per_week", "active",
                "current_streak",
            ],
            "discipline_completions": [
                "task", "catagory", "completed_date", "logged_at",
            ],
        }

        class Result:
            def __init__(self, rows=None, first=None):
                self._rows = rows or []
                self._first = first

            def all(self):
                return self._rows

            def first(self):
                return self._first

        class Transaction:
            rolled_back = False

            def rollback(self):
                self.rolled_back = True

        transaction = Transaction()

        class Connection:
            def begin(self):
                return transaction

            def execute(self, statement, params=None):
                sql = str(statement)
                if "information_schema.columns" in sql:
                    rows = [(table, column) for table, columns in required.items() for column in columns]
                    return Result(rows=rows)
                if "SELECT 1 FROM discipline_completions" in sql:
                    return Result(first=(1,))
                return Result()

        class ConnectionContext:
            def __enter__(self):
                return Connection()

            def __exit__(self, exc_type, exc, traceback):
                return False

        class Engine:
            def connect(self):
                return ConnectionContext()

        with patch.object(db, "get_engine", return_value=Engine()):
            detail = db.discipline_storage_health()
        self.assertIn("permissions verified", detail)
        self.assertTrue(transaction.rolled_back)


class RecurringFormTests(unittest.TestCase):
    def test_enabled_recurrence_requires_a_schedule(self) -> None:
        app._validate_recurring_form({
            "recurring": "1", "recurring_schedule_type": "interval",
            "recurring_interval": "7",
        })
        app._validate_recurring_form({
            "recurring": "1", "recurring_schedule_type": "weekdays",
            "recurring_days": ["0", "4"],
        })
        app._validate_recurring_form({
            "recurring": "1", "recurring_schedule_type": "monthly",
            "recurring_month_ordinal": "1", "recurring_month_weekday": "0",
        })
        with self.assertRaisesRegex(Exception, "Enter a repeat interval"):
            app._validate_recurring_form({
                "recurring": "1", "recurring_schedule_type": "interval",
                "recurring_interval": "",
            })
        with self.assertRaisesRegex(Exception, "Choose at least one weekday"):
            app._validate_recurring_form({
                "recurring": "1", "recurring_schedule_type": "weekdays",
                "recurring_days": [],
            })


class DisciplineWorkflowTests(unittest.TestCase):
    def test_mark_requires_post_commit_visibility(self) -> None:
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
            patch.object(db, "_try_refresh_discipline_streak"),
            patch.object(db, "completion_exists", return_value=False),
        ):
            self.assertFalse(db.mark_completion("Gym", "Health", "2026-08-13"))

    def test_done_today_marks_canonical_task_by_uuid(self) -> None:
        import asyncio

        class FormRequest:
            async def form(self):
                return {"action": "mark"}

        discipline = {
            "uuid": "disc-1", "task": "MacroFactor Logging",
            "catagory": "Health", "active": 1,
        }
        with (
            patch.object(app, "_require_v2"),
            patch.object(db, "get_discipline", return_value=discipline),
            patch.object(db, "mark_completion", return_value=True) as mark,
            patch.object(db, "completion_exists", return_value=True),
            patch.object(db, "computed_discipline_streak", return_value=5),
            patch.object(clock, "local_today", return_value=date(2026, 8, 18)),
        ):
            response = asyncio.run(app.discipline_today("disc-1", FormRequest()))
        mark.assert_called_once_with(
            "MacroFactor Logging", "Health", "2026-08-18"
        )
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload["marked"])
        self.assertEqual(payload["streak"], 5)
        self.assertEqual(payload["discipline_uuid"], "disc-1")

        source, _, _ = app.templates.env.loader.get_source(
            app.templates.env, "home.html"
        )
        self.assertIn(
            'data-endpoint="/discipline/{{ d.uuid }}/today"', source
        )

    def test_discipline_frequency_is_one_to_seven(self) -> None:
        self.assertEqual(db._discipline_frequency("7"), 7)
        with self.assertRaisesRegex(ValueError, "between 1 and 7"):
            db._discipline_frequency("0")


class GameAndWatchTests(unittest.TestCase):
    def test_media_insights_handles_partial_metrics(self) -> None:
        items = [
            {
                "title": "Rated backlog", "status": "backlog", "rating": 9,
                "platform": "Steam", "hours_played": "12.5",
                "date_added": "2026-08-01", "last_played": "2026-08-10",
            },
            {
                "title": "Finished", "status": "completed", "rating": 7,
                "platform": "", "hours_played": "not recorded",
                "date_added": "partial-date", "last_played": "",
            },
            {
                "title": "Unrated", "status": "paused", "rating": None,
                "platform": None, "hours_played": "2",
                "date_added": "", "last_played": "",
            },
        ]
        insights = gnw.media_insights(
            "games", items, today=date(2026, 8, 18)
        )
        self.assertEqual(insights["total"], 3)
        self.assertEqual(insights["average_rating"], 8.0)
        self.assertEqual(insights["completion_percent"], 33)
        self.assertEqual(insights["total_hours"], 14.5)
        self.assertEqual(insights["average_backlog_age_days"], 17)
        self.assertEqual(insights["breakdown_labels"], ["Unknown", "Steam"])
        self.assertEqual(
            [item["title"] for item in insights["highly_rated_unfinished"]],
            ["Rated backlog"],
        )

    def test_media_insights_page_uses_local_charts_and_tables(self) -> None:
        request = Request({
            "type": "http", "method": "GET", "path": "/media/insights",
            "query_string": b"section=games", "headers": [],
        })
        items = [{
            "title": "Example Game", "profile": "Example Profile",
            "status": "playing", "rating": 9, "platform": "Steam",
            "hours_played": "5", "date_added": "2026-08-01",
            "last_played": "2026-08-17",
        }]
        with (
            patch.object(gnw, "disabled_reason", return_value=None),
            patch.object(gnw, "list_profiles", return_value=["Example Profile"]),
            patch.object(gnw, "list_items", return_value=items),
        ):
            response = app.media_insights_page(request, "games", "")
        body = response.body.decode()
        self.assertIn("Media Insights", body)
        self.assertIn("chart.umd.min.js", body)
        self.assertIn("media_insights.js", body)
        self.assertIn("Highly rated unfinished", body)
        self.assertIn("Example Game", body)
        self.assertIn("insights-data-table", body)

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
                "games", "Example Profile",
                {"title": "Portal", "source": "steam", "external_id": "400", "cover_url": "cover"},
                status="backlog", priority=4,
            )
        self.assertTrue(ok)
        self.assertEqual(title, "Portal")
        written, cell_range = worksheet.update.call_args.args
        self.assertEqual(cell_range, "A2:G2")
        self.assertEqual(written[0][headers.index("Profile")], "Example Profile")
        self.assertEqual(written[0][headers.index("Title")], "Portal")

    def test_game_card_exposes_inline_rating(self) -> None:
        template = app.templates.get_template("partials/media_card.html")
        html = template.render(
            section="games",
            statuses=gnw.GAME_STATUSES,
            status_labels=gnw.STATUS_LABELS,
            item={
                "title": "Portal", "profile": "Example Profile", "priority": 4,
                "rating": 9, "cover_url": "", "link": None, "platform": "Steam",
                "is_multiplayer": False, "price": "", "tags": [], "status": "completed",
                "source": "steam", "external_id": "400",
            },
        )
        self.assertIn("Your rating", html)
        self.assertIn('name="rating"', html)
        self.assertIn('type="number"', html)
        self.assertIn('min="0"', html)
        self.assertIn('max="10"', html)
        self.assertIn('value="9"', html)
        self.assertIn('hx-post="/gnw/games/update"', html)
        self.assertIn("media-status-trigger", html)
        self.assertIn("Move to", html)
        self.assertIn('"status": "completed"', html)

    def test_show_card_exposes_inline_rating(self) -> None:
        template = app.templates.get_template("partials/media_card.html")
        html = template.render(
            section="shows",
            statuses=gnw.SHOW_STATUSES,
            status_labels=gnw.STATUS_LABELS,
            item={
                "title": "The Expanse", "profile": "Example Profile", "priority": 4,
                "rating": 8, "cover_url": "", "link": None, "genre": "Sci-Fi",
                "current_season": 3, "current_episode": 6, "total_episodes": 62,
                "tags": [], "status": "watching", "source": "tvmaze",
                "external_id": "2817",
            },
        )
        self.assertIn("Your rating", html)
        self.assertIn('name="rating"', html)
        self.assertIn('type="number"', html)
        self.assertIn('value="8"', html)
        self.assertIn('hx-post="/gnw/shows/update"', html)
        self.assertIn("S3 · E6/62", html)

    def test_full_sheet_url_is_accepted(self) -> None:
        url = "https://docs.google.com/spreadsheets/d/abc_123-X/edit?gid=0"
        self.assertEqual(gnw._normalize_sheet_id(url), "abc_123-X")

    def test_reordered_trimmed_headers_are_supported(self) -> None:
        headers = [" Title ", "Priority", " Profile ", "Status"]
        item = gnw._row_to_item(
            "games", headers, ["Example", "5", "Example Profile", "playing"]
        )
        self.assertEqual(item["title"], "Example")
        self.assertEqual(item["profile"], "Example Profile")
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

    def test_retired_github_models_endpoint_migrates_to_copilot(self) -> None:
        with patch.dict(os.environ, {
            "LUIGI_WEB_LLM_PROVIDER": "openai",
            "LUIGI_WEB_LLM_BASE_URL": "https://models.github.ai/inference",
            "LUIGI_WEB_LLM_API_KEY": "github-token",
            "LUIGI_WEB_LLM_MODEL": "openai/gpt-4o-mini",
        }, clear=True):
            provider = llm.build_provider_from_env()
        self.assertIsInstance(provider, llm.CopilotSDKProvider)
        self.assertEqual(provider.github_token, "github-token")
        self.assertEqual(provider.model, "auto")

    def test_copilot_can_use_existing_cli_login_without_api_key(self) -> None:
        with patch.dict(os.environ, {
            "LUIGI_WEB_LLM_PROVIDER": "copilot",
            "LUIGI_WEB_LLM_API_KEY": "",
            "LUIGI_WEB_LLM_MODEL": "",
        }, clear=True):
            provider = llm.build_provider_from_env()
        self.assertIsInstance(provider, llm.CopilotSDKProvider)
        self.assertIsNone(provider.github_token)
        self.assertEqual(provider.model, "auto")

    def test_copilot_retries_rejected_token_with_service_login(self) -> None:
        clients: list[dict[str, object]] = []

        class FakeSession:
            async def send_and_wait(self, prompt, timeout):
                return SimpleNamespace(data=SimpleNamespace(content="Fallback ready."))

            async def disconnect(self):
                pass

        class FakeClient:
            def __init__(self, **options):
                self.options = options
                clients.append(options)

            async def start(self):
                if self.options["github_token"]:
                    raise RuntimeError("401 authentication failed")

            async def create_session(self, **options):
                return FakeSession()

            async def stop(self):
                pass

        provider = llm.CopilotSDKProvider(
            github_token="rejected-token", model="", timeout=5,
            base_directory=tempfile.gettempdir(),
        )
        with patch("copilot.CopilotClient", FakeClient):
            result = provider.run_chat(
                [{"role": "user", "content": "Hello"}], {}
            )

        self.assertEqual(result.reply, "Fallback ready.")
        self.assertEqual([client["github_token"] for client in clients], [
            "rejected-token", None,
        ])
        self.assertFalse(clients[0]["use_logged_in_user"])
        self.assertTrue(clients[1]["use_logged_in_user"])

    def test_copilot_auth_error_explains_both_failed_methods(self) -> None:
        class FailingClient:
            def __init__(self, **options):
                pass

            async def start(self):
                raise RuntimeError("401 authentication failed")

            async def stop(self):
                pass

        provider = llm.CopilotSDKProvider(
            github_token="rejected-token", model="", timeout=5,
            base_directory=tempfile.gettempdir(),
        )
        with patch("copilot.CopilotClient", FailingClient):
            with self.assertRaisesRegex(
                llm.LLMError, "service-login fallback also failed"
            ):
                provider.run_chat([{"role": "user", "content": "Hello"}], {})

    def test_copilot_sdk_exposes_only_custom_allow_list(self) -> None:
        captured: dict[str, object] = {}

        class FakeSession:
            def __init__(self, options):
                self.options = options

            async def send_and_wait(self, prompt, timeout):
                captured["prompt"] = prompt
                invocation = SimpleNamespace(arguments={"limit": 2})
                captured["tool_result"] = self.options["tools"][0].handler(invocation)
                captured["blocked_tool_result"] = self.options["tools"][0].handler(invocation)
                return SimpleNamespace(data=SimpleNamespace(content="Two tasks found."))

            async def disconnect(self):
                captured["disconnected"] = True

        class FakeClient:
            def __init__(self, **options):
                captured["client"] = options

            async def start(self):
                captured["started"] = True

            async def create_session(self, **options):
                captured["session"] = options
                return FakeSession(options)

            async def stop(self):
                captured["stopped"] = True

        calls: list[dict[str, object]] = []
        tool = llm.Tool(
            name="list_open_tasks",
            description="List open tasks",
            parameters={"type": "object", "properties": {"limit": {"type": "integer"}}},
            handler=lambda arguments: calls.append(arguments) or {"tasks": []},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = llm.CopilotSDKProvider(
                github_token="github-token",
                model="",
                timeout=5,
                base_directory=temp_dir,
            )
            with (
                patch("copilot.CopilotClient", FakeClient),
                patch.dict(os.environ, {"LUIGI_WEB_LLM_MAX_TOOL_ITERATIONS": "1"}),
            ):
                result = llm.run_chat_with_tools(
                    provider,
                    [
                        {"role": "system", "content": "Use task tools only."},
                        {"role": "user", "content": "What is open?"},
                    ],
                    {tool.name: tool},
                )

        session = captured["session"]
        self.assertEqual(captured["client"]["mode"], "empty")
        self.assertFalse(captured["client"]["use_logged_in_user"])
        child_env = captured["client"]["env"]
        self.assertIn("COPILOT_CLI_EXTRACT_DIR", child_env)
        self.assertNotIn("LUIGI_WEB_PG_PASSWORD", child_env)
        self.assertNotIn("LUIGI_WEB_FINANCE_TOKEN", child_env)
        self.assertEqual(session["available_tools"], ["custom:*"])
        self.assertEqual(session["mcp_servers"], {})
        self.assertFalse(session["enable_file_hooks"])
        self.assertFalse(session["enable_host_git_operations"])
        self.assertFalse(session["enable_skills"])
        self.assertEqual(calls, [{"limit": 2}])
        self.assertIn("tool-call cap reached", captured["blocked_tool_result"])
        self.assertEqual(result.reply, "Two tasks found.")
        self.assertEqual(result.tool_calls[0].name, "list_open_tasks")
        self.assertTrue(result.tool_calls[0].ok)
        self.assertFalse(result.tool_calls[1].ok)


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
            patch.object(db, "list_recurring", return_value=[]),
            patch.object(
                db,
                "list_task_completion_events",
                return_value=(
                    app.task_events.Capability(False, "not installed"), []
                ),
            ),
        ):
            response = app.calendar_page(request, "2026-08")
        self.assertEqual(response.context["month_label"], "August 2026")
        self.assertGreaterEqual(len(response.context["weeks"]), 5)
        self.assertTrue(all(len(week) == 7 for week in response.context["weeks"]))

    def test_calendar_adds_projected_recurring_occurrences(self) -> None:
        request = Request({"type": "http", "method": "GET", "path": "/calendar",
                           "query_string": b"", "headers": []})
        recurring = {
            "uuid": "rent", "task": "Pay Rent", "priority": 9,
            "status": "Not Started", "completed": 0, "recurring": 1,
            "task_creation": "2026-01-01", "due_date": None,
            "recurring_month_ordinal": 1, "recurring_month_weekday": 0,
        }
        with (
            patch.object(app, "_require_v2"),
            patch.object(app, "_reactivate_recurring"),
            patch.object(db, "list_calendar_rows", return_value=[]),
            patch.object(db, "list_recurring", return_value=[recurring]),
            patch.object(
                db,
                "list_task_completion_events",
                return_value=(
                    app.task_events.Capability(False, "not installed"), []
                ),
            ),
        ):
            response = app.calendar_page(request, "2026-08")
        august_third = next(
            day for week in response.context["weeks"] for day in week
            if day["iso"] == "2026-08-03"
        )
        self.assertEqual([row["task"] for row in august_third["tasks"]], ["Pay Rent"])
        self.assertTrue(august_third["tasks"][0]["_projected"])


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

    def test_compact_list_keeps_correct_endpoints_and_actions(self) -> None:
        row = {
            "uuid": "rec-1", "task": "Weekly Review", "priority": 3,
            "status": "Not Started", "completed": 0, "due_date": "2026-08-15",
            "completed_time": None, "recurring": 1, "recurring_interval": 7,
            "recurring_days": None, "project": "Operations", "catagory": "Work",
            "task_group": None, "sub_group": None, "_endpoint_root": "/recurring",
        }
        html = app.templates.get_template("partials/task_list.html").render(
            rows=[row], statuses=db.STATUS_DISPLAY_ORDER, endpoint_root="/tasks",
        )
        self.assertIn('data-view-panel="list"', html)
        self.assertIn('hx-post="/recurring/rec-1/status"', html)
        self.assertIn('data-action-menu', html)
        self.assertIn('>Delete</button>', html)

    def test_task_data_attributes_normalize_boolean_completion(self) -> None:
        row = {
            "uuid": "done-1", "task": "Completed row", "priority": 1,
            "status": "Completed", "completed": True, "due_date": None,
            "completed_time": "2026-08-18T10:00:00", "recurring": 0,
            "recurring_interval": None, "recurring_days": None,
            "project": None, "catagory": None, "task_group": None,
            "sub_group": None, "_endpoint_root": "/tasks",
        }
        card = app.templates.get_template("partials/task_card.html").render(
            t=row, endpoint_root="/tasks",
        )
        compact = app.templates.get_template("partials/task_list.html").render(
            rows=[row], statuses=db.STATUS_DISPLAY_ORDER, endpoint_root="/tasks",
        )
        self.assertIn('data-completed="1"', card)
        self.assertIn('data-completed="1"', compact)

    def test_bulk_selection_identity_is_shared_by_board_and_list(self) -> None:
        row = {
            "uuid": "rec-1", "task": "Weekly Review", "priority": 3,
            "status": "Not Started", "completed": 0, "due_date": None,
            "completed_time": None, "recurring": 1, "recurring_interval": 7,
            "recurring_days": None, "project": None, "catagory": "Work",
            "task_group": None, "sub_group": None, "_endpoint_root": "/recurring",
        }
        card = app.templates.get_template("partials/task_card.html").render(
            t=row, endpoint_root="/tasks",
        )
        compact = app.templates.get_template("partials/task_list.html").render(
            rows=[row], statuses=db.STATUS_DISPLAY_ORDER, endpoint_root="/tasks",
        )
        for html in (card, compact):
            self.assertIn('data-uuid="rec-1"', html)
            self.assertIn('data-task-source="recurring"', html)
            self.assertIn("data-task-select", html)

    def test_bulk_route_reports_mixed_success_and_failure(self) -> None:
        client = TestClient(app.app)
        client.cookies.set(auth.COOKIE_NAME, "expected")
        client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        transition = db.TaskTransition(completed=1)
        with (
            patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "expected"}),
            patch.object(db, "set_task_status", return_value=transition) as task_status,
            patch.object(
                db, "set_recurring_status", side_effect=PermissionError("denied")
            ) as recurring_status,
        ):
            response = client.post("/tasks/bulk", json={
                "action": "status", "value": "Completed",
                "items": [
                    {"uuid": "task-1", "source": "task"},
                    {"uuid": "rec-1", "source": "recurring"},
                ],
            }, headers={"X-CSRF-Token": "csrf-value"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["succeeded"], 1)
        self.assertEqual(response.json()["failed"], 1)
        self.assertEqual(response.json()["results"][1]["error"], "Operation failed")
        task_status.assert_called_once_with("task-1", "Completed")
        recurring_status.assert_called_once_with("rec-1", "Completed")

    def test_bulk_route_rejects_invalid_action_before_writes(self) -> None:
        client = TestClient(app.app)
        client.cookies.set(auth.COOKIE_NAME, "expected")
        client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        with patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "expected"}):
            response = client.post("/tasks/bulk", json={
                "action": "drop-table", "items": [
                    {"uuid": "task-1", "source": "task"}
                ],
            }, headers={"X-CSRF-Token": "csrf-value"})
        self.assertEqual(response.status_code, 422)


class CommandPaletteTests(unittest.TestCase):
    def test_blank_palette_contains_quick_actions(self) -> None:
        request = Request({"type": "http", "method": "GET", "path": "/command-palette",
                           "query_string": b"", "headers": []})
        response = app.command_palette_results(request, "")
        body = response.body.decode()
        self.assertIn("New task", body)
        self.assertIn("Add game", body)
        self.assertIn("Go to", body)

    def test_palette_groups_task_search_results(self) -> None:
        request = Request({"type": "http", "method": "GET", "path": "/command-palette",
                           "query_string": b"q=review", "headers": []})
        task = {"uuid": "1", "task": "Weekly Review", "status": "Not Started",
                "due_date": None, "catagory": "Work", "source": "task"}
        with (
            patch.object(db, "find_tasks_by_name", return_value=[task]),
            patch.object(db, "search_disciplines", return_value=[]),
            patch.object(gnw, "is_enabled", return_value=False),
        ):
            response = app.command_palette_results(request, "review")
        body = response.body.decode()
        self.assertIn("Weekly Review", body)
        self.assertIn('/tasks/1/edit', body)

    def test_quick_add_emits_success_before_refresh(self) -> None:
        import asyncio

        class FormRequest:
            async def form(self):
                return {"task": "Capture screenshots", "priority": "2"}

        with (
            patch.object(app, "_require_v2"),
            patch.object(db, "create_task", return_value="new-uuid"),
        ):
            response = asyncio.run(app.tasks_quick_create(FormRequest()))
        self.assertEqual(response.headers.get("hx-refresh"), "true")
        self.assertIn("flashSuccess", response.headers.get("hx-trigger", ""))


if __name__ == "__main__":
    unittest.main()
