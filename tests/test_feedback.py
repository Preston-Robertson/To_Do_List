"""Offline tests for the local-only feedback inbox."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from luigi_web import application, auth, feedback, maintainer


class FeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "LUIGI_WEB_UI_TOKEN": "main-secret",
            "LUIGI_WEB_FEEDBACK_DB": os.path.join(self.temp_dir.name, "feedback.db"),
            "LUIGI_WEB_MAINTAINER_DB": os.path.join(self.temp_dir.name, "maintainer.db"),
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

    def test_feedback_approval_creates_one_sanitized_immutable_job(self) -> None:
        row_uuid = feedback.create_item({
            "category": "Bug",
            "message": "Contact person@example.test; token=abc12345; see "
                       "https://example.test/tasks?account=123.",
            "page_path": "/tasks",
        })
        item = feedback.list_items()[0]
        job_uuid = maintainer.enqueue_feedback(
            item, "Fix it for owner@example.test without changing authentication.",
        )

        job = maintainer.job_for_feedback(row_uuid)
        self.assertEqual(job["uuid"], job_uuid)
        self.assertEqual(job["status"], "Queued")
        self.assertEqual(job["page_path"], "/tasks")
        self.assertNotIn("person@example.test", job["request_text"])
        self.assertNotIn("abc12345", job["request_text"])
        self.assertNotIn("account=123", job["request_text"])
        self.assertNotIn("owner@example.test", job["acceptance_criteria"])

        feedback.update_item(row_uuid, {
            "status": "Archived", "tags": "changed", "notes": "changed",
        })
        self.assertEqual(
            maintainer.job_for_feedback(row_uuid)["acceptance_criteria"],
            "Fix it for [redacted email] without changing authentication.",
        )
        with self.assertRaisesRegex(ValueError, "already queued"):
            maintainer.enqueue_feedback(item, "Try a duplicate job.")

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

    def test_feedback_approval_route_requires_review_and_is_single_use(self) -> None:
        row_uuid = feedback.create_item({
            "category": "UX", "message": "Improve the compact task controls.",
            "page_path": "/tasks",
        })
        client = TestClient(application.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")
        client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        headers = {"X-CSRF-Token": "csrf-value"}

        unreviewed = client.post(f"/feedback/{row_uuid}/approve", data={
            "acceptance_criteria": "Controls fit at 390px.",
        }, headers=headers)
        self.assertEqual(unreviewed.status_code, 422)

        approved = client.post(f"/feedback/{row_uuid}/approve", data={
            "acceptance_criteria": "Controls fit at 390px.",
            "privacy_confirmed": "on",
        }, headers=headers)
        self.assertEqual(approved.status_code, 204)
        self.assertEqual(feedback.get_item(row_uuid)["status"], "Planned")
        self.assertEqual(maintainer.job_for_feedback(row_uuid)["status"], "Queued")

        duplicate = client.post(f"/feedback/{row_uuid}/approve", data={
            "acceptance_criteria": "Queue it twice.", "privacy_confirmed": "on",
        }, headers=headers)
        self.assertEqual(duplicate.status_code, 409)

        job = maintainer.job_for_feedback(row_uuid)
        maintainer.claim_next_job()
        maintainer.finish_job(
            job["uuid"], status="Draft PR", summary="Synthetic fix ready.",
            head_commit="a" * 40, pr_url="https://example.test/pull/7",
        )
        page = client.get("/feedback")
        self.assertIn("Synthetic fix ready.", page.text)
        self.assertIn("https://example.test/pull/7", page.text)


if __name__ == "__main__":
    unittest.main()