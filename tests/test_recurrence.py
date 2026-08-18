"""Dependency-free tests for recurring calendar calculations."""
from __future__ import annotations

import unittest
from datetime import date

import recurrence


class MonthlyRecurrenceTests(unittest.TestCase):
    def test_first_monday_is_strictly_after_completion(self) -> None:
        self.assertEqual(
            recurrence.next_monthly_occurrence(date(2026, 8, 3), 1, 0),
            date(2026, 9, 7),
        )

    def test_last_friday_handles_month_length(self) -> None:
        self.assertEqual(
            recurrence.next_monthly_occurrence(date(2026, 2, 1), -1, 4),
            date(2026, 2, 27),
        )

    def test_invalid_monthly_schedule_is_rejected(self) -> None:
        self.assertIsNone(recurrence.parse_monthly_schedule(5, 0))
        self.assertIsNone(recurrence.parse_monthly_schedule(1, 7))

    def test_calendar_projects_selected_weekdays(self) -> None:
        row = {
            "recurring": 1,
            "due_date": "2026-08-03",
            "recurring_days": "0,4",
        }
        self.assertEqual(
            recurrence.calendar_occurrence_dates(
                row, date(2026, 8, 1), date(2026, 8, 10)
            ),
            [date(2026, 8, 3), date(2026, 8, 7), date(2026, 8, 10)],
        )

    def test_calendar_projects_monthly_positions(self) -> None:
        row = {
            "recurring": 1,
            "task_creation": "2026-07-01",
            "recurring_month_ordinal": 1,
            "recurring_month_weekday": 0,
        }
        self.assertEqual(
            recurrence.calendar_occurrence_dates(
                row, date(2026, 8, 1), date(2026, 10, 10)
            ),
            [date(2026, 8, 3), date(2026, 9, 7), date(2026, 10, 5)],
        )


if __name__ == "__main__":
    unittest.main()