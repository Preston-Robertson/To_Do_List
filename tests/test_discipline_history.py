"""Live Discipline history with synthetic, isolated storage."""
from datetime import date
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from luigi_web.modules.tasks import repository


class DisciplineHistoryRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        self.enterContext(patch.object(repository, "get_engine", return_value=self.engine))
        self.enterContext(patch.object(repository.clock, "local_today", return_value=date(2030, 4, 24)))
        self.enterContext(patch.object(repository, "_try_refresh_discipline_streak"))
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE discipline_list (uuid TEXT PRIMARY KEY, task TEXT, catagory TEXT, active INTEGER)
            """))
            connection.execute(text("INSERT INTO discipline_list VALUES ('example-id', 'Example habit', 'Example category', 1)"))
            connection.execute(text("""
                CREATE TABLE discipline_completions
                (task TEXT, catagory TEXT, completed_date TEXT, logged_at TEXT)
            """))
            connection.execute(text("""
                INSERT INTO discipline_completions VALUES (:task, NULL, :day, :stamp)
            """), [
                {"task": "Example habit", "day": "2030-01-01", "stamp": "2030-01-02T12:00:00"},
                {"task": " EXAMPLE HABIT ", "day": "2030-12-31T09:00:00", "stamp": None},
                {"task": "Example habit", "day": "2031-01-01", "stamp": "2031-01-01T12:00:00Z"},
                {"task": "Other example", "day": "2030-04-03", "stamp": "2030-04-03T12:00:00Z"},
            ])

    def test_year_includes_last_day_and_retains_original_logged_at(self):
        rows = repository.list_discipline_history("Example habit", date(2030, 1, 1), date(2030, 12, 31))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["completed_date"], "2030-12-31T09:00:00")
        self.assertIsNone(rows[0]["logged_at"])
        self.assertEqual(rows[1]["logged_at"], "2030-01-02T12:00:00")
        self.assertNotIn("completed_at", rows[1])

    def test_task_is_parameterized(self):
        self.assertEqual(repository.list_discipline_history("' OR 1=1 --", date(2030, 1, 1), date(2030, 12, 31)), [])

    def test_unbounded_range_rejected(self):
        with self.assertRaises(ValueError):
            repository.list_discipline_history("Example habit", date(2028, 1, 1), date(2030, 12, 31))

    def rows(self, day):
        return repository.list_discipline_history("Example habit", day, day)

    def change(self, day, marked, **kwargs):
        return repository.change_discipline_history(
            "example-id", day, repository.discipline_history_version(self.rows(day)), marked, **kwargs,
        )

    def test_mark_is_idempotent_and_undo_removes_only_new_day(self):
        day = date(2030, 4, 20)
        snapshot = self.change(day, True)
        self.assertTrue(snapshot["changed"])
        rows = self.rows(day)
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["logged_at"])
        self.assertFalse(self.change(day, True)["changed"])
        self.assertEqual(self.rows(day), rows)
        self.change(day, False, restore=snapshot)
        self.assertEqual(self.rows(day), [])
        self.assertEqual(len(self.rows(date(2030, 1, 1))), 1)

    def test_remove_undo_retains_original_timestamp_and_duplicate_rows(self):
        day = date(2030, 1, 1)
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO discipline_completions VALUES (' EXAMPLE HABIT ', NULL, '2030-01-01T09:00:00', NULL)"))
        original = self.rows(day)
        snapshot = self.change(day, False)
        self.assertEqual(self.rows(day), [])
        self.change(day, True, restore=snapshot)
        self.assertEqual(self.rows(day), original)

    def test_stale_write_and_stale_undo_rejected(self):
        day = date(2030, 4, 20)
        empty = repository.discipline_history_version([])
        snapshot = self.change(day, True)
        with self.assertRaises(repository.DisciplineHistoryConflict):
            repository.change_discipline_history("example-id", day, empty, False)
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE discipline_completions SET logged_at = '2030-04-22T12:00:00' WHERE completed_date = '2030-04-20'"))
        with self.assertRaises(repository.DisciplineHistoryConflict):
            repository.change_discipline_history("example-id", day, snapshot["after_version"], False, restore=snapshot)

    def test_paused_today_and_future_blocked_but_past_corrections_allowed(self):
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE discipline_list SET active = 0"))
        for day in (date(2030, 4, 24), date(2030, 4, 25)):
            with self.assertRaises(repository.DisciplineHistoryConflict):
                self.change(day, True)
        self.assertTrue(self.change(date(2030, 4, 23), True)["changed"])

    def test_renamed_or_ambiguous_discipline_cannot_restore_detached_history(self):
        snapshot = self.change(date(2030, 1, 1), False)
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE discipline_list SET task = 'Renamed example'"))
        with self.assertRaises(repository.DisciplineHistoryConflict):
            self.change(date(2030, 1, 1), True, restore=snapshot)
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE discipline_list SET task = 'Example habit'"))
            connection.execute(text("INSERT INTO discipline_list VALUES ('duplicate-id', 'example habit', NULL, 1)"))
        with self.assertRaises(repository.DisciplineHistoryConflict):
            self.change(date(2030, 1, 1), True)

    def test_rejected_write_rolls_back(self):
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TRIGGER reject_mark AFTER INSERT ON discipline_completions
                BEGIN DELETE FROM discipline_completions WHERE completed_date = NEW.completed_date; END
            """))
        with self.assertRaisesRegex(RuntimeError, "could not be verified"):
            self.change(date(2030, 4, 20), True)
        self.assertEqual(self.rows(date(2030, 4, 20)), [])