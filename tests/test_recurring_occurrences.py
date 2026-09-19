"""Recurring occurrences preserve completed history and definition fields."""
from __future__ import annotations

import copy
import unittest

from luigi_web.modules.tasks.occurrences import occurrence_payload
from luigi_web.modules.tasks.recurrence import calendar_occurrence_dates
from datetime import date


class OccurrencePayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = {
            "id": 1, "uuid": "example-parent", "task": "Example weekly task",
            "priority": 4, "status": "Completed", "completed": 1,
            "completed_time": "2030-04-10T12:00:00+00:00",
            "task_creation": "2030-04-01", "start_time": "2030-04-09T12:00:00",
            "due_date": "2030-04-10", "logged_hours": 2,
            "recurring": 1, "recurring_interval": 7, "recurring_days": None,
            "recurring_month_ordinal": None, "recurring_month_weekday": None,
            "project": "Example project", "catagory": "Example category",
            "task_group": "Example group", "sub_group": "Example subgroup",
            "estimated_time": 1, "relevant_link": "https://example.invalid/", "archived": 0,
        }

    def test_new_instance_keeps_definition_but_not_execution_state(self) -> None:
        before = copy.deepcopy(self.source)
        result = occurrence_payload(self.source, "2030-04-17", "2030-04-17T08:00:00+00:00")
        self.assertEqual(self.source, before)
        self.assertNotEqual(result["uuid"], self.source["uuid"])
        self.assertNotIn("id", result)
        for field in ("task", "priority", "project", "catagory", "task_group", "sub_group", "estimated_time", "recurring_interval", "relevant_link"):
            self.assertEqual(result[field], self.source[field])
        self.assertEqual(result["completed"], 0)
        self.assertIsNone(result["completed_time"])
        self.assertIsNone(result["start_time"])
        self.assertEqual(result["logged_hours"], 0)
        self.assertEqual(result["status"], "Not Started")
        self.assertEqual(result["due_date"], "2030-04-17")
        self.assertEqual(result["task_creation"], "2030-04-17T08:00:00+00:00")

    def test_retries_use_stable_identity_not_creation_time(self) -> None:
        first = occurrence_payload(self.source, "2030-04-17", "2030-04-17T08:00:00")
        second = occurrence_payload(self.source, "2030-04-17", "2030-04-18T08:00:00")
        self.assertEqual(first["uuid"], second["uuid"])

    def test_new_completion_has_different_identity(self) -> None:
        first = occurrence_payload(self.source, "2030-04-17", "2030-04-17T08:00:00")
        self.source["completed_time"] = "2030-04-11T12:00:00+00:00"
        second = occurrence_payload(self.source, "2030-04-18", "2030-04-18T08:00:00")
        self.assertNotEqual(first["uuid"], second["uuid"])

    def test_weekday_and_monthly_definitions_are_copied(self) -> None:
        for fields in ({"recurring_interval": None, "recurring_days": "0,4"},
                       {"recurring_interval": None, "recurring_month_ordinal": -1, "recurring_month_weekday": 4}):
            with self.subTest(fields=fields):
                source = {**self.source, **fields}
                result = occurrence_payload(source, "2030-04-17", "2030-04-17T08:00:00")
                for field, value in fields.items():
                    self.assertEqual(result[field], value)

    def test_inactive_or_incomplete_rows_cannot_generate(self) -> None:
        for changed in ({"recurring": 0}, {"archived": 1}, {"completed": 0}, {"completed_time": None}, {"uuid": ""}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                occurrence_payload({**self.source, **changed}, "2030-04-17", "2030-04-17T08:00:00")

    def test_consumed_parent_does_not_duplicate_child_projections(self) -> None:
        parent = {**self.source, "_recurrence_generated": True}
        child = occurrence_payload(self.source, "2030-04-17", "2030-04-17T08:00:00")
        self.assertEqual(calendar_occurrence_dates(parent, date(2030, 4, 1), date(2030, 4, 30)), [])
        self.assertEqual(calendar_occurrence_dates(child, date(2030, 4, 1), date(2030, 4, 30)), [date(2030, 4, 17), date(2030, 4, 24)])


if __name__ == "__main__":
    unittest.main()