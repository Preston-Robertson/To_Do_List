"""Today selection is app-owned and independent of shared task scheduling."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from luigi_web.modules.tasks import operations


class TodaySelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.dict(os.environ, {"LUIGI_WEB_OPERATIONS_DB": str(Path(temporary.name) / "operations.db")}))
        self.day = date(2026, 9, 17)

    def test_selection_is_persisted_and_idempotent(self) -> None:
        self.assertTrue(operations.set_today_selection("example", "task", True, day=self.day))
        self.assertTrue(operations.set_today_selection("example", "task", True, day=self.day))
        self.assertEqual(operations.list_today_selections(self.day), {("task", "example")})
        self.assertFalse(operations.set_today_selection("example", "task", False, day=self.day))
        self.assertEqual(operations.list_today_selections(self.day), set())

    def test_today_is_date_scoped_and_source_qualified(self) -> None:
        operations.set_today_selection("example", "task", True, day=self.day)
        operations.set_today_selection("example", "recurring", True, day=self.day)
        self.assertEqual(operations.list_today_selections(date(2026, 9, 18)), set())
        operations.set_today_selection("example", "task", False, day=self.day)
        self.assertEqual(operations.list_today_selections(self.day), {("recurring", "example")})

    def test_default_day_uses_configured_clock(self) -> None:
        with patch.object(operations.clock, "local_today", return_value=self.day):
            operations.set_today_selection("example", "task", True)
            self.assertEqual(operations.list_today_selections(), {("task", "example")})

    def test_invalid_selection_is_rejected(self) -> None:
        for row_uuid, source, selected in (("", "task", True), ("example", "finance", True), ("example", "task", 1), ("x" * 201, "task", True)):
            with self.subTest(source=source), self.assertRaises(ValueError):
                operations.set_today_selection(row_uuid, source, selected, day=self.day)

    def test_deletion_and_undo_restore_only_own_selection(self) -> None:
        operations.set_today_selection("example", "task", True, day=self.day)
        operations.set_today_selection("example", "recurring", True, day=self.day)
        snapshot = operations.delete_task_records("example", "task")
        self.assertEqual(operations.list_today_selections(self.day), {("recurring", "example")})
        operations.restore_task_records(snapshot)
        self.assertEqual(operations.list_today_selections(self.day), {("task", "example"), ("recurring", "example")})

    def test_storage_has_references_only_not_task_fields(self) -> None:
        operations.set_today_selection("example", "task", True, day=self.day)
        with operations._connect() as connection:
            columns = [row["name"] for row in connection.execute("PRAGMA table_info(home_today_selections)")]
        self.assertEqual(columns, ["task_uuid", "task_source", "selected_date"])


if __name__ == "__main__":
    unittest.main()