"""Live follow-up rules create new rows and leave completed history intact."""
from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from sqlalchemy import create_engine, text

from luigi_web.modules.tasks import repository


class TaskRuleGenerationTests(unittest.TestCase):
    def test_each_follow_up_has_new_identity_and_no_completion_state(self):
        rule = {
            "follow_up_task": "Example repeated task", "priority": 3,
            "catagory": "Example category", "task_group": "Example group",
            "subgroup": "Example subgroup", "due_offset_days": 2,
        }
        with patch.object(repository.clock, "local_today", return_value=date(2030, 4, 10)):
            first = repository._follow_up_task_payload(rule)
            second = repository._follow_up_task_payload(rule)
        self.assertNotEqual(first["uuid"], second["uuid"])
        for created in (first, second):
            self.assertEqual(created["task"], rule["follow_up_task"])
            self.assertEqual(created["priority"], 3)
            self.assertEqual(created["catagory"], "Example category")
            self.assertEqual(created["sub_group"], "Example subgroup")
            self.assertEqual(created["status"], "Not Started")
            self.assertEqual(created["completed"], 0)
            self.assertIsNone(created["completed_time"])
            self.assertIsNone(created["start_time"])
            self.assertEqual(created["due_date"], "2030-04-12")

    def test_same_name_rule_inserts_new_rows_without_resetting_old_completion(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        columns = ", ".join(f"{column} TEXT" for column in repository._TASK_COLUMNS)
        with engine.begin() as connection, patch.object(repository, "_cols_for", return_value=repository._TASK_COLUMNS):
            connection.execute(text(f"CREATE TABLE tasks ({columns})"))
            connection.execute(text("""
                CREATE TABLE follow_up_tasks (
                    trigger_task TEXT, follow_up_task TEXT, catagory TEXT,
                    task_group TEXT, subgroup TEXT, relevant_link TEXT,
                    priority INTEGER, estimated_time REAL, due_offset_days INTEGER
                )
            """))
            connection.execute(text("""
                INSERT INTO tasks (uuid, task, completed, completed_time, status)
                VALUES (:uuid, :task, 1, :completed_time, 'Completed')
            """), {"uuid": "example-completed", "task": "Example repeated task", "completed_time": "2030-04-09T12:00:00"})
            connection.execute(text("""
                INSERT INTO follow_up_tasks (trigger_task, follow_up_task, priority)
                VALUES (:task, :task, 3)
            """), {"task": "Example repeated task"})
            first = repository._spawn_follow_ups(connection, "Example repeated task")
            second = repository._spawn_follow_ups(connection, "Example repeated task")
            rows = connection.execute(text("SELECT uuid, task, completed, completed_time, status FROM tasks")).mappings().all()
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(set(first + second)), 2)
        original = next(row for row in rows if row["uuid"] == "example-completed")
        self.assertEqual(original["status"], "Completed")
        self.assertEqual(str(original["completed"]), "1")
        self.assertEqual(original["completed_time"], "2030-04-09T12:00:00")
        for row in rows:
            if row["uuid"] in first + second:
                self.assertEqual(row["status"], "Not Started")
                self.assertEqual(str(row["completed"]), "0")
                self.assertIsNone(row["completed_time"])


if __name__ == "__main__":
    unittest.main()