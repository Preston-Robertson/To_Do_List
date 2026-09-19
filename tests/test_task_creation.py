"""Unified creation routes to the correct shared table without converting edits."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from luigi_web import application as host


class UnifiedTaskCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "synthetic-creation-token"}))
        self.enterContext(patch.object(host, "_require_v2"))
        self.row = {"uuid": "example", "task": "Example task", "status": "Not Started", "completed": 0}
        self.create_task = self.enterContext(patch.object(host.db, "create_task", return_value="example"))
        self.create_recurring = self.enterContext(patch.object(host.db, "create_recurring", return_value="example"))
        self.get_task = self.enterContext(patch.object(host.db, "get_task", return_value=self.row))
        self.get_recurring = self.enterContext(patch.object(host.db, "get_recurring", return_value={**self.row, "recurring": 1, "recurring_interval": 7}))
        self.client = TestClient(host.app)
        self.addCleanup(self.client.close)
        self.client.headers["Authorization"] = "Bearer synthetic-creation-token"

    def test_one_off_ignores_stale_schedule_fields(self) -> None:
        response = self.client.post("/tasks", data={"task": "Example task", "recurring": "0", "recurring_interval": "7"})
        self.assertEqual(response.status_code, 200)
        self.create_task.assert_called_once_with({"task": "Example task"})
        self.create_recurring.assert_not_called()
        self.assertIn('data-endpoint="/tasks"', response.text)

    def test_repeat_creates_recurring_row_only(self) -> None:
        response = self.client.post("/tasks", data={"task": "Example task", "recurring": "1", "recurring_interval": "7"})
        self.assertEqual(response.status_code, 200)
        self.create_task.assert_not_called()
        self.create_recurring.assert_called_once()
        self.assertIn('data-endpoint="/recurring"', response.text)

    def test_selected_weekdays_survive_form_parsing(self) -> None:
        response = self.client.post("/tasks", content="task=Example&recurring=0&recurring=1&recurring_schedule_type=weekdays&recurring_days=0&recurring_days=4", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.create_recurring.call_args.args[0]["recurring_days"], ["0", "4"])

    def test_invalid_schedule_never_writes(self) -> None:
        response = self.client.post("/tasks", data={"task": "Example", "recurring": "1", "recurring_schedule_type": "weekdays"})
        self.assertEqual(response.status_code, 422)
        self.create_task.assert_not_called()
        self.create_recurring.assert_not_called()

    def test_bad_repeat_and_empty_title_never_write(self) -> None:
        for data in ({"task": "Example", "recurring": "invalid"}, {"task": " "}):
            with self.subTest(data=data):
                self.assertEqual(self.client.post("/tasks", data=data).status_code, 422)
        self.create_task.assert_not_called()
        self.create_recurring.assert_not_called()

    def test_readback_failure_has_no_success_signal(self) -> None:
        self.get_recurring.return_value = None
        response = self.client.post("/tasks", data={"task": "Example", "recurring": "1", "recurring_interval": "7"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("hx-trigger", response.headers)

    def test_cookie_creation_requires_csrf(self) -> None:
        del self.client.headers["Authorization"]
        self.client.cookies.set("luigi_session", "synthetic-creation-token")
        self.assertEqual(self.client.post("/tasks", data={"task": "Example"}).status_code, 403)
        self.create_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()