"""Contract tests for the optional LuigiBot-owned task event ledger."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from luigi_web import db, task_events


_TASK_EVENTS_DDL = """
CREATE TABLE task_events (
    event_uuid TEXT PRIMARY KEY,
    operation_uuid TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    source_task_uuid TEXT NOT NULL,
    source_table TEXT NOT NULL,
    task_snapshot TEXT NOT NULL,
    catagory_snapshot TEXT,
    occurred_at TEXT NOT NULL,
    effective_date TEXT,
    due_date_snapshot TEXT,
    actor_source TEXT NOT NULL,
    related_event_uuid TEXT,
    details_json TEXT NOT NULL
)
"""


class TaskEventsContractTests(unittest.TestCase):
    @staticmethod
    def _task_engine(*, task_events_ddl: str | None = None):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        task_columns = ", ".join(f"{column} TEXT" for column in db._TASK_COLUMNS)
        with engine.begin() as conn:
            conn.execute(text(f"CREATE TABLE tasks ({task_columns})"))
            conn.execute(text(f"CREATE TABLE recurring_tasks ({task_columns})"))
            conn.execute(text("""
                CREATE TABLE follow_up_tasks (
                    trigger_task TEXT, follow_up_task TEXT, catagory TEXT,
                    task_group TEXT, subgroup TEXT, relevant_link TEXT,
                    priority INTEGER, estimated_time REAL, due_offset_days INTEGER
                )
            """))
            conn.execute(text("""
                INSERT INTO tasks (
                    uuid, task, catagory, status, completed, completed_time,
                    recurring, priority, archived
                ) VALUES (
                    'task-1', 'Example task', 'Example category',
                    'Not Started', 0, NULL, 0, 1, 0
                )
            """))
            if task_events_ddl:
                conn.execute(text(task_events_ddl))
        return engine

    def test_missing_table_degrades_without_ddl(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as conn:
            status = task_events.capability(conn)
            event_uuid = task_events.append_event(
                conn,
                event_type=task_events.COMPLETED,
                source_task_uuid="task-1",
                source_table="tasks",
                task_snapshot="Example task",
                occurred_at="2026-08-18T20:00:00",
                effective_date="2026-08-18",
                actor_source="web",
            )
        self.assertFalse(status.available)
        self.assertIn("not installed", status.reason)
        self.assertIsNone(event_uuid)
        self.assertNotIn("task_events", engine.dialect.get_table_names(engine.connect()))
        engine.dispose()

    def test_effective_date_uses_server_local_cutoff(self) -> None:
        eastern = ZoneInfo("America/New_York")
        self.assertEqual(
            task_events.effective_date_for(
                datetime(2026, 8, 19, 1, 0, tzinfo=eastern), cutoff="04:00"
            ),
            "2026-08-18",
        )
        self.assertEqual(
            task_events.effective_date_for(
                datetime(2026, 8, 19, 5, 0, tzinfo=eastern), cutoff="04:00"
            ),
            "2026-08-19",
        )

    def test_effective_date_override_is_validated(self) -> None:
        self.assertEqual(
            task_events.effective_date_for(
                datetime(2026, 8, 19, 5, 0), override="2026-08-17"
            ),
            "2026-08-17",
        )
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            task_events.effective_date_for(
                datetime(2026, 8, 19, 5, 0), override="yesterday"
            )

    def test_completion_and_reversal_are_idempotent(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text(_TASK_EVENTS_DDL))
            self.assertTrue(task_events.capability(conn).available)
            first = task_events.append_event(
                conn,
                event_type=task_events.COMPLETED,
                source_task_uuid="task-1",
                source_table="tasks",
                task_snapshot="Example task",
                catagory_snapshot="Example category",
                occurred_at="2026-08-18T20:00:00",
                effective_date="2026-08-18",
                due_date_snapshot="2026-08-18",
                actor_source="web",
                operation_uuid="operation-complete",
            )
            retry = task_events.append_event(
                conn,
                event_type=task_events.COMPLETED,
                source_task_uuid="task-1",
                source_table="tasks",
                task_snapshot="Example task",
                occurred_at="2026-08-18T20:00:01",
                effective_date="2026-08-18",
                actor_source="web",
                operation_uuid="operation-complete",
            )
            reversal = task_events.append_reversal(
                conn,
                completion_event_uuid=first or "",
                source_task_uuid="task-1",
                source_table="tasks",
                task_snapshot="Example task",
                occurred_at="2026-08-18T20:01:00",
                actor_source="web",
                operation_uuid="operation-reverse",
            )
            rows = conn.execute(text(
                "SELECT event_type, related_event_uuid FROM task_events "
                "ORDER BY occurred_at"
            )).all()
        self.assertEqual(first, retry)
        self.assertIsNotNone(reversal)
        self.assertEqual(rows, [
            (task_events.COMPLETED, None),
            (task_events.COMPLETION_REVERSED, first),
        ])
        engine.dispose()

    def test_active_completion_query_excludes_reversed_events(self) -> None:
        engine = self._task_engine(task_events_ddl=_TASK_EVENTS_DDL)
        with engine.begin() as conn:
            active = task_events.append_event(
                conn,
                event_type=task_events.COMPLETED,
                source_task_uuid="task-1",
                source_table="tasks",
                task_snapshot="Example task",
                occurred_at="2026-08-18T20:00:00",
                effective_date="2026-08-18",
                actor_source="web",
                operation_uuid="active-completion",
            )
            reversed_event = task_events.append_event(
                conn,
                event_type=task_events.COMPLETED,
                source_task_uuid="task-1",
                source_table="tasks",
                task_snapshot="Earlier completion",
                occurred_at="2026-08-17T20:00:00",
                effective_date="2026-08-17",
                actor_source="web",
                operation_uuid="reversed-completion",
            )
            task_events.append_reversal(
                conn,
                completion_event_uuid=reversed_event or "",
                source_task_uuid="task-1",
                source_table="tasks",
                task_snapshot="Earlier completion",
                occurred_at="2026-08-17T21:00:00",
                actor_source="web",
                operation_uuid="reverse-earlier",
            )
            rows = task_events.list_active_completions(
                conn, start_date="2026-08-01", end_date="2026-08-31"
            )
        self.assertEqual([row["event_uuid"] for row in rows], [active])
        self.assertTrue(rows[0]["source_exists"])
        engine.dispose()

    def test_generic_event_query_filters_type_and_task(self) -> None:
        engine = self._task_engine(task_events_ddl=_TASK_EVENTS_DDL)
        with engine.begin() as conn:
            task_events.append_event(
                conn,
                event_type=task_events.COMPLETED,
                source_task_uuid="task-1",
                source_table="tasks",
                task_snapshot="Example task",
                occurred_at="2026-08-18T20:00:00-04:00",
                effective_date="2026-08-18",
                actor_source="web",
                operation_uuid="event-query-example",
            )
            task_events.append_event(
                conn,
                event_type=task_events.COMPLETED,
                source_task_uuid="task-2",
                source_table="tasks",
                task_snapshot="Different task",
                occurred_at="2026-08-18T19:00:00-04:00",
                effective_date="2026-08-18",
                actor_source="assistant",
                operation_uuid="event-query-different",
            )
            rows = task_events.list_events(
                conn,
                start_timestamp="2026-08-01T00:00:00-04:00",
                event_type=task_events.COMPLETED,
                query="example",
            )
        self.assertEqual([row["task_snapshot"] for row in rows], ["Example task"])
        engine.dispose()

    def test_reversal_resolves_original_effective_date(self) -> None:
        engine = self._task_engine(task_events_ddl=_TASK_EVENTS_DDL)
        with engine.begin() as conn:
            completed = task_events.append_event(
                conn,
                event_type=task_events.COMPLETED,
                source_task_uuid="task-1",
                source_table="tasks",
                task_snapshot="Example task",
                occurred_at="2026-08-19T01:00:00",
                effective_date="2026-08-18",
                actor_source="web",
                operation_uuid="complete-after-midnight",
            )
            reversed_event = task_events.append_reversal(
                conn,
                completion_event_uuid=completed or "",
                source_task_uuid="task-1",
                source_table="tasks",
                task_snapshot="Example task",
                occurred_at="2026-08-19T01:05:00",
                actor_source="web",
                operation_uuid="reopen-after-midnight",
            )
            original = task_events.completion_reversed_by(
                conn, reversed_event or ""
            )
        self.assertEqual(original["effective_date"], "2026-08-18")
        engine.dispose()

    def test_task_completion_degrades_when_ledger_is_absent(self) -> None:
        engine = self._task_engine()
        with patch.object(db, "get_engine", return_value=engine):
            transition = db.toggle_task_completed("task-1")
        with engine.connect() as conn:
            completed = conn.execute(text(
                "SELECT completed FROM tasks WHERE uuid = 'task-1'"
            )).scalar_one()
        self.assertEqual(int(completed), 1)
        self.assertFalse(transition.history_available)
        self.assertIsNone(transition.event_uuid)
        engine.dispose()

    def test_legacy_completion_fallback_converts_utc_to_local_calendar_day(self) -> None:
        engine = self._task_engine()
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE tasks SET completed = 1, status = 'Completed',
                    completed_time = '2026-08-19T01:00:00'
                WHERE uuid = 'task-1'
            """))
        with (
            patch.object(db, "get_engine", return_value=engine),
            patch.dict(os.environ, {"LUIGI_WEB_TIMEZONE": "America/New_York"}),
        ):
            status, rows = db.list_task_completion_events(
                date(2026, 8, 18), date(2026, 8, 18)
            )
        self.assertFalse(status.available)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["due_date"], "2026-08-18")
        self.assertTrue(rows[0]["_history_limited"])
        engine.dispose()

    def test_date_override_rolls_back_when_ledger_is_absent(self) -> None:
        engine = self._task_engine()
        with patch.object(db, "get_engine", return_value=engine):
            with self.assertRaisesRegex(ValueError, "requires the shared"):
                db.toggle_task_completed("task-1", effective_date="2026-08-17")
        with engine.connect() as conn:
            completed = conn.execute(text(
                "SELECT completed FROM tasks WHERE uuid = 'task-1'"
            )).scalar_one()
        self.assertEqual(int(completed), 0)
        engine.dispose()

    def test_task_completion_and_reopen_write_linked_events(self) -> None:
        engine = self._task_engine(task_events_ddl=_TASK_EVENTS_DDL)
        with patch.object(db, "get_engine", return_value=engine):
            completed = db.toggle_task_completed("task-1")
            reopened = db.toggle_task_completed("task-1")
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT event_uuid, event_type, actor_source, related_event_uuid
                FROM task_events ORDER BY occurred_at, event_type
            """)).all()
        self.assertTrue(completed.history_available)
        self.assertEqual(completed.event_type, task_events.COMPLETED)
        self.assertEqual(reopened.event_type, task_events.COMPLETION_REVERSED)
        self.assertEqual(rows[0][1:3], (task_events.COMPLETED, "web"))
        self.assertEqual(rows[1][1], task_events.COMPLETION_REVERSED)
        self.assertEqual(rows[1][3], rows[0][0])
        engine.dispose()

    def test_task_transition_rolls_back_when_installed_ledger_rejects_write(self) -> None:
        rejecting_ddl = _TASK_EVENTS_DDL.replace(
            "actor_source TEXT NOT NULL,",
            "actor_source TEXT NOT NULL CHECK (actor_source != 'web'),",
        )
        engine = self._task_engine(task_events_ddl=rejecting_ddl)
        with patch.object(db, "get_engine", return_value=engine):
            with self.assertRaises(IntegrityError):
                db.toggle_task_completed("task-1")
        with engine.connect() as conn:
            completed = conn.execute(text(
                "SELECT completed FROM tasks WHERE uuid = 'task-1'"
            )).scalar_one()
            event_count = conn.execute(text(
                "SELECT COUNT(*) FROM task_events"
            )).scalar_one()
        self.assertEqual(int(completed), 0)
        self.assertEqual(event_count, 0)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()