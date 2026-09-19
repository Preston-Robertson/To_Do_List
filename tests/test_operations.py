"""Offline task dependency and reminder-domain tests."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from fastapi.testclient import TestClient

from luigi_web import application, auth, db, operations


class OperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "LUIGI_WEB_OPERATIONS_DB": os.path.join(self.temp_dir.name, "operations.db"),
        })
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp_dir.cleanup()

    def test_dependency_cycle_is_rejected_and_blocker_is_enforced(self) -> None:
        operations.add_dependency(
            dependent_uuid="a", dependent_source="task", dependent_label="A",
            blocker_uuid="b", blocker_source="task", blocker_label="B",
        )
        with self.assertRaisesRegex(ValueError, "cycle"):
            operations.add_dependency(
                dependent_uuid="b", dependent_source="task", dependent_label="B",
                blocker_uuid="a", blocker_source="task", blocker_label="A",
            )
        resolver = lambda source, row_uuid: {
            "uuid": row_uuid, "completed": 0, "status": "Not Started"
        }
        with self.assertRaisesRegex(ValueError, "Blocked by: B"):
            operations.assert_unblocked("a", "task", resolver)
        operations.assert_unblocked(
            "a", "task",
            lambda source, row_uuid: {"completed": 1, "status": "Completed"},
        )
        operations.assert_unblocked("a", "task", lambda source, row_uuid: None)
        self.assertEqual(operations.list_dependencies(), [])

    def test_reminders_generate_deduplicate_snooze_and_dismiss(self) -> None:
        rule_uuid = operations.create_reminder_rule({
            "task_uuid": "task-1", "task_source": "task",
            "task_label": "Example", "kind": "custom",
            "remind_on": "2026-08-21", "message": "Review Example",
        })
        rows = [{
            "uuid": "task-1", "source": "task", "task": "Example",
            "status": "Not Started", "completed": 0, "due_date": "2026-08-20",
        }]
        self.assertEqual(operations.evaluate_notifications(rows, today=date(2026, 8, 21)), 2)
        self.assertEqual(operations.evaluate_notifications(rows, today=date(2026, 8, 21)), 2)
        notifications = operations.list_notifications()
        custom = next(row for row in notifications if row["rule_uuid"] == rule_uuid)
        self.assertTrue(operations.snooze_notification(custom["uuid"], 1))
        self.assertEqual(len(operations.list_notifications()), 1)
        remaining = operations.list_notifications()[0]
        self.assertTrue(operations.dismiss_notification(remaining["uuid"]))
        self.assertEqual(operations.list_notifications(), [])

    def test_resolved_conditions_retire_stale_notifications(self) -> None:
        overdue = [{
            "uuid": "task-1", "source": "task", "task": "Example",
            "status": "Not Started", "completed": 0, "due_date": "2026-08-20",
        }]
        self.assertEqual(
            operations.evaluate_notifications(overdue, today=date(2026, 8, 21)), 1
        )
        resolved = [{**overdue[0], "completed": 1, "status": "Completed"}]
        self.assertEqual(
            operations.evaluate_notifications(resolved, today=date(2026, 8, 21)), 0
        )
        self.assertEqual(operations.list_notifications(), [])

    def test_task_rule_cleanup_can_be_restored_for_undo(self) -> None:
        edge_uuid = operations.add_dependency(
            dependent_uuid="a", dependent_source="task", dependent_label="A",
            blocker_uuid="b", blocker_source="task", blocker_label="B",
        )
        rule_uuid = operations.create_reminder_rule({
            "task_uuid": "b", "task_source": "task", "task_label": "B",
            "kind": "custom", "remind_on": "2026-08-21",
        })
        snapshot = operations.delete_task_records("b", "task")
        self.assertEqual(operations.list_dependencies(), [])
        self.assertEqual(operations.list_reminder_rules(), [])

        operations.restore_task_records(snapshot)
        self.assertEqual(operations.list_dependencies()[0]["uuid"], edge_uuid)
        self.assertEqual(operations.list_reminder_rules()[0]["uuid"], rule_uuid)

    def test_reconcile_prunes_external_deletes_and_refreshes_labels(self) -> None:
        operations.add_dependency(
            dependent_uuid="a", dependent_source="task", dependent_label="Old A",
            blocker_uuid="b", blocker_source="task", blocker_label="Old B",
        )
        operations.create_reminder_rule({
            "task_uuid": "b", "task_source": "task", "task_label": "Old B",
            "kind": "custom", "remind_on": "2026-08-21",
        })
        operations.reconcile_task_records([
            {"uuid": "a", "source": "task", "task": "Current A"},
            {"uuid": "b", "source": "task", "task": "Current B"},
        ])
        edge = operations.list_dependencies()[0]
        self.assertEqual(edge["dependent_label"], "Current A")
        self.assertEqual(edge["blocker_label"], "Current B")
        self.assertEqual(operations.list_reminder_rules()[0]["task_label"], "Current B")

        removed = operations.reconcile_task_records([
            {"uuid": "a", "source": "task", "task": "Current A"},
        ])
        self.assertEqual(removed, {"dependencies": 1, "reminder_rules": 1})
        self.assertEqual(operations.list_dependencies(), [])
        self.assertEqual(operations.list_reminder_rules(), [])

    def test_edit_status_cannot_bypass_dependency_check(self) -> None:
        from sqlalchemy import create_engine, text

        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            for table in ("tasks", "recurring_tasks"):
                columns = ", ".join(f"{column} TEXT" for column in db._TASK_COLUMNS)
                connection.execute(text(f"CREATE TABLE {table} ({columns})"))
                connection.execute(text(f"INSERT INTO {table} (uuid, task, completed, status, priority) VALUES ('example', 'Example blocked task', 0, 'Not Started', 1)"))
        with patch.object(db, "get_engine", return_value=engine), \
             patch.object(db, "_TABLES_MISSING_COLUMNS", {}), \
             patch.object(db, "_assert_task_unblocked", side_effect=ValueError("Blocked by: B")) as check:
            for table in ("tasks", "recurring_tasks"):
                with self.subTest(table=table), self.assertRaisesRegex(ValueError, "Blocked by: B"):
                    db._update_task_like(table, "example", {"status": "In Progress", "priority": 7})
                with engine.connect() as connection:
                    row = connection.execute(text(f"SELECT completed, status, priority FROM {table} WHERE uuid='example'")).one()
                    self.assertEqual((str(row.completed), row.status, str(row.priority)), ("0", "Not Started", "1"))
            self.assertEqual(check.call_count, 2)

    def test_database_completion_uses_dependency_enforcement(self) -> None:
        with patch.object(operations, "assert_unblocked", side_effect=ValueError("Blocked by: B")):
            with self.assertRaisesRegex(ValueError, "Blocked by: B"):
                db._assert_task_unblocked("tasks", "task-a")

    def test_task_rule_routes_resolve_server_labels(self) -> None:
        client = TestClient(application.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")
        client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        dependent = {"uuid": "a", "task": "Dependent", "completed": 0}
        blocker = {"uuid": "b", "task": "Blocker", "completed": 0}
        with (
            patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "main-secret"}),
            patch.object(db, "get_task", side_effect=lambda value: dependent if value == "a" else blocker),
        ):
            response = client.post(
                "/task-rules/dependencies",
                data={"dependent": "task:a", "blocker": "task:b"},
                headers={"X-CSRF-Token": "csrf-value"},
            )
        self.assertEqual(response.status_code, 204)
        edge = operations.list_dependencies()[0]
        self.assertEqual(edge["dependent_label"], "Dependent")
        self.assertEqual(edge["blocker_label"], "Blocker")

    def test_reminder_count_and_inbox_routes_evaluate(self) -> None:
        client = TestClient(application.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")
        task = {
            "uuid": "task-1", "task": "Due task", "status": "Not Started",
            "completed": 0, "due_date": "2026-08-20",
        }
        with (
            patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "main-secret"}),
            patch.object(db, "list_tasks", return_value=[task]),
            patch.object(db, "list_recurring", return_value=[]),
            patch("luigi_web.operations.clock.local_today", return_value=date(2026, 8, 21)),
        ):
            count = client.get("/reminders/count")
            inbox = client.get("/reminders")
        self.assertEqual(count.text, "1")
        self.assertIn("Due task is overdue", inbox.text)
        self.assertEqual(inbox.headers["Cache-Control"], "no-store")

    def test_blocked_status_route_surfaces_reason(self) -> None:
        client = TestClient(application.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")
        client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        with (
            patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "main-secret"}),
            patch.object(db, "set_task_status", side_effect=ValueError("Blocked by: Blocker")),
        ):
            response = client.post(
                "/tasks/task-a/status", data={"status": "In Progress"},
                headers={"X-CSRF-Token": "csrf-value"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Blocked by", response.text)

    def test_blocked_edit_route_surfaces_reason(self) -> None:
        client = TestClient(application.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")
        client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        with (
            patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "main-secret"}),
            patch.object(db, "update_task", side_effect=ValueError("Blocked by: Blocker")),
        ):
            response = client.post(
                "/tasks/task-a", data={"task": "Dependent", "status": "In Progress"},
                headers={"X-CSRF-Token": "csrf-value"},
            )
        self.assertEqual(response.status_code, 422)
        self.assertIn("Blocked by", response.text)


if __name__ == "__main__":
    unittest.main()