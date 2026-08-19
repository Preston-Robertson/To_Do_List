"""Offline tests for the local-only feedback inbox."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from luigi_web import application, auth, feedback


class FeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "LUIGI_WEB_UI_TOKEN": "main-secret",
            "LUIGI_WEB_FEEDBACK_DB": os.path.join(self.temp_dir.name, "feedback.db"),
        })
        self.env.start()
        feedback.init_db()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp_dir.cleanup()

    def test_feedback_lifecycle_and_exports(self) -> None:
        row_uuid = feedback.create_item({
            "category": "Idea",
            "message": "Add a compact agenda.",
            "page_path": "/calendar",
        })
        self.assertTrue(feedback.update_item(row_uuid, {
            "status": "Planned", "tags": "calendar", "notes": "Roadmap",
        }))
        rows = feedback.list_items(status="Planned", query="agenda")
        self.assertEqual([row["uuid"] for row in rows], [row_uuid])
        self.assertEqual(feedback.export_payload()["items"][0]["page_path"], "/calendar")
        self.assertIn("Add a compact agenda.", feedback.export_markdown())
        self.assertTrue(feedback.delete_item(row_uuid))
        self.assertEqual(feedback.list_items(), [])

    def test_feedback_create_self_initializes_schema(self) -> None:
        os.remove(feedback.db_path())
        row_uuid = feedback.create_item({
            "category": "Bug", "message": "Startup-independent capture",
        })
        self.assertEqual(feedback.list_items()[0]["uuid"], row_uuid)

    def test_feedback_rejects_query_data_and_oversized_messages(self) -> None:
        with self.assertRaisesRegex(ValueError, "without query data"):
            feedback.create_item({
                "category": "Bug", "message": "Example",
                "page_path": "/tasks?token=secret",
            })
        with self.assertRaisesRegex(ValueError, "1-5000"):
            feedback.create_item({"category": "Bug", "message": "x" * 5001})

    def test_feedback_routes_require_csrf_and_exports_are_no_store(self) -> None:
        client = TestClient(application.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")
        rejected = client.post("/feedback", data={
            "category": "Bug", "message": "Example bug",
        })
        self.assertEqual(rejected.status_code, 403)

        client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        accepted = client.post("/feedback", data={
            "category": "Bug", "message": "Example bug", "page_path": "/tasks",
        }, headers={"X-CSRF-Token": "csrf-value"})
        self.assertEqual(accepted.status_code, 204)
        review = client.get("/feedback/export")
        self.assertIn("Inspect every item", review.text)
        exported = client.get("/feedback/export.json")
        self.assertEqual(exported.headers["Cache-Control"], "no-store")
        self.assertNotIn("LUIGI_WEB_UI_TOKEN", exported.text)


if __name__ == "__main__":
    unittest.main()