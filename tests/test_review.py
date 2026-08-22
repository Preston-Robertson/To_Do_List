"""Offline tests for guided review persistence and aggregation."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from fastapi.testclient import TestClient

from luigi_web import application, auth
from luigi_web import db, review


class ReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "LUIGI_WEB_REVIEW_DB": os.path.join(self.temp_dir.name, "review.db"),
        })
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp_dir.cleanup()

    def test_review_session_round_trip_filters_unknown_steps(self) -> None:
        session = review.save_session(
            "daily",
            completed_steps=["triage_overdue", "unknown", "triage_overdue"],
            notes="Focus on one task.",
            anchor=date(2026, 8, 21),
        )
        self.assertEqual(session["completed_steps"], ["triage_overdue"])
        self.assertEqual(session["notes"], "Focus on one task.")
        self.assertEqual(session["review_date"], "2026-08-21")

    def test_daily_review_reuses_existing_aggregations(self) -> None:
        with (
            patch.object(db, "list_overdue_tasks", return_value=[{"uuid": "1"}]) as overdue,
            patch.object(db, "list_upcoming_tasks", return_value=[{"uuid": "2"}]) as upcoming,
            patch.object(db, "list_disciplines_at_risk", return_value=[{"uuid": "3"}]),
            patch.object(db, "list_disciplines_pending_today", return_value=[{"uuid": "4"}]),
            patch.object(db, "list_recent_completions", return_value=[]),
        ):
            state = review.build("daily", date(2026, 8, 21))
        overdue.assert_called_once_with(limit=50)
        upcoming.assert_called_once_with(days=1, limit=50)
        self.assertEqual(state["overdue"][0]["uuid"], "1")
        self.assertEqual(state["pending_disciplines"][0]["uuid"], "4")
        self.assertIsNone(state["weekly"])

    def test_weekly_review_uses_complete_week_rollup(self) -> None:
        with (
            patch.object(db, "list_overdue_tasks", return_value=[]),
            patch.object(db, "list_upcoming_tasks", return_value=[]),
            patch.object(db, "list_disciplines_at_risk", return_value=[]),
            patch.object(db, "list_recent_completions", return_value=[]),
            patch.object(db, "weekly_review", return_value={"completed_total": 5}) as weekly,
        ):
            state = review.build("weekly", date(2026, 8, 21))
        weekly.assert_called_once_with(date(2026, 8, 21))
        self.assertEqual(state["weekly"]["completed_total"], 5)
        self.assertEqual(state["session"]["review_date"], "2026-08-20")

    def test_review_routes_render_and_save(self) -> None:
        client = TestClient(application.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")
        client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        empty = {
            "scope": "daily", "anchor": "2026-08-21",
            "session": {"review_date": "2026-08-21", "completed_steps": [], "notes": ""},
            "steps": review.DAILY_STEPS, "overdue": [], "upcoming": [],
            "at_risk": [], "recent_completions": [], "pending_disciplines": [],
            "weekly": None,
        }
        with (
            patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "main-secret"}),
            patch.object(review, "build", return_value=empty),
        ):
            page = client.get("/review?scope=daily")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Daily review", page.text)
        self.assertIn("Triage overdue work", page.text)

        with patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "main-secret"}):
            saved = client.post(
                "/review/daily",
                data={
                    "completed_steps": "choose_focus",
                    "notes": "One important task.",
                },
                headers={"X-CSRF-Token": "csrf-value"},
            )
        self.assertEqual(saved.status_code, 204)
        session = review.get_session("daily")
        self.assertEqual(session["completed_steps"], ["choose_focus"])
        self.assertEqual(session["notes"], "One important task.")


if __name__ == "__main__":
    unittest.main()