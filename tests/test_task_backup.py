"""Offline contract tests for preview-first shared task restoration."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text
from fastapi.testclient import TestClient

from luigi_web import application, auth, db, task_backup


class TaskBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.metadata_path = Path(self.temp_dir.name) / "task-web-metadata.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        task_backup._PREVIEWS.clear()

    @staticmethod
    def _engine(*, reject_follow_up: bool = False):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        task_columns = ", ".join(f"{column} TEXT" for column in db._TASK_COLUMNS)
        priority = "TEXT CHECK (CAST(priority AS INTEGER) <= 5)" if reject_follow_up else "TEXT"
        with engine.begin() as conn:
            conn.execute(text(f"CREATE TABLE tasks ({task_columns})"))
            conn.execute(text(f"CREATE TABLE recurring_tasks ({task_columns})"))
            conn.execute(text("""
                CREATE TABLE discipline_list (
                    uuid TEXT PRIMARY KEY, task TEXT, catagory TEXT,
                    frequency_per_week INTEGER, active INTEGER,
                    current_streak INTEGER
                )
            """))
            conn.execute(text("""
                CREATE TABLE discipline_completions (
                    task TEXT, catagory TEXT, completed_date TEXT, logged_at TEXT,
                    UNIQUE(task, completed_date)
                )
            """))
            conn.execute(text(f"""
                CREATE TABLE follow_up_tasks (
                    uuid TEXT PRIMARY KEY, trigger_task TEXT, follow_up_task TEXT,
                    catagory TEXT, task_group TEXT, subgroup TEXT,
                    relevant_link TEXT, priority {priority}, estimated_time REAL,
                    due_offset_days INTEGER, created TEXT
                )
            """))
        return engine

    @staticmethod
    def _payload(*, follow_up_priority: int = 3):
        return {
            "format": task_backup.FORMAT,
            "schema_version": 2,
            "tables": {
                "tasks": [{
                    "uuid": "11111111-1111-4111-8111-111111111111",
                    "task": "Restored task", "priority": 4,
                    "status": "Not Started", "task_creation": "2026-08-21",
                    "completed": 0, "recurring": 0,
                }],
                "recurring_tasks": [],
                "discipline_list": [{
                    "uuid": "22222222-2222-4222-8222-222222222222",
                    "task": "Example discipline", "catagory": "Health",
                    "frequency_per_week": 3, "active": 1, "current_streak": 2,
                }],
                "discipline_completions": [{
                    "task": "Example discipline", "catagory": "Health",
                    "completed_date": "2026-08-20",
                    "logged_at": "2026-08-20T18:00:00-04:00",
                }],
                "follow_up_tasks": [{
                    "uuid": "33333333-3333-4333-8333-333333333333",
                    "trigger_task": "Restored task", "follow_up_task": "Next task",
                    "priority": follow_up_priority, "due_offset_days": 1,
                    "created": "2026-08-21T10:00:00-04:00",
                }],
            },
            "web_metadata": {
                "tasks": {
                    "11111111-1111-4111-8111-111111111111": {
                        "project": "Example Project", "archived": 0,
                    }
                },
                "recurring_tasks": {},
            },
        }

    def test_prepare_commit_and_idempotent_second_restore(self) -> None:
        engine = self._engine()
        raw = json.dumps(self._payload()).encode()
        with (
            patch.object(db, "get_engine", return_value=engine),
            patch.object(db, "_WEB_META_PATH", str(self.metadata_path)),
        ):
            token, plan = task_backup.prepare(raw)
            self.assertEqual(plan["tables"]["tasks"], {"insert": 1, "update": 0})
            result = task_backup.commit(token)
            self.assertEqual(result["tasks"]["insert"], 1)
            with self.assertRaisesRegex(task_backup.RestoreError, "already used"):
                task_backup.commit(token)

            second_token, second_plan = task_backup.prepare(raw)
            self.assertEqual(second_plan["tables"]["tasks"], {"insert": 0, "update": 1})
            second = task_backup.commit(second_token)
            self.assertEqual(second["tasks"]["update"], 1)

        with engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one(), 1)
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM discipline_completions")).scalar_one(), 1)
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(
            metadata["tasks"]["11111111-1111-4111-8111-111111111111"]["project"],
            "Example Project",
        )
        engine.dispose()

    def test_duplicate_uuid_is_rejected_before_database_access(self) -> None:
        payload = self._payload()
        payload["tables"]["tasks"].append(dict(payload["tables"]["tasks"][0]))
        with self.assertRaisesRegex(task_backup.RestoreError, "duplicate key"):
            task_backup.parse_backup(json.dumps(payload).encode())

    def test_late_failure_rolls_back_all_shared_rows(self) -> None:
        engine = self._engine(reject_follow_up=True)
        payload = task_backup.parse_backup(
            json.dumps(self._payload(follow_up_priority=9)).encode()
        )
        with (
            patch.object(db, "get_engine", return_value=engine),
            patch.object(db, "_WEB_META_PATH", str(self.metadata_path)),
        ):
            with self.assertRaises(Exception):
                task_backup.restore_backup(payload)
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one(), 0)
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM discipline_list")).scalar_one(), 0)
        self.assertFalse(self.metadata_path.exists())
        engine.dispose()

    def test_restore_routes_preview_then_commit(self) -> None:
        client = TestClient(application.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")
        client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        plan = {
            "tables": {"tasks": {"insert": 1, "update": 0}},
            "metadata_rows": 1, "total_rows": 1,
        }
        result = {"tasks": {"insert": 1, "update": 0}}
        with (
            patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "main-secret"}),
            patch.object(task_backup, "prepare", return_value=("preview-token", plan)),
        ):
            preview = client.post(
                "/admin/restore/preview",
                files={"backup": ("backup.json", b"{}", "application/json")},
                headers={"X-CSRF-Token": "csrf-value"},
            )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.headers["Cache-Control"], "no-store")
        self.assertIn("Preview only", preview.text)
        self.assertIn("preview-token", preview.text)

        with (
            patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "main-secret"}),
            patch.object(task_backup, "commit", return_value=result) as commit,
        ):
            committed = client.post(
                "/admin/restore/commit",
                data={"token": "preview-token"},
                headers={"X-CSRF-Token": "csrf-value"},
            )
        self.assertIn("Restore committed and verified", committed.text)
        commit.assert_called_once_with("preview-token")

    def test_restore_upload_requires_csrf(self) -> None:
        client = TestClient(application.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")
        with patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "main-secret"}):
            response = client.post(
                "/admin/restore/preview",
                files={"backup": ("backup.json", b"{}", "application/json")},
            )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()