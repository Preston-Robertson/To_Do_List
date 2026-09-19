"""Cold-process checks of disposable, synthetic Discipline preview state."""
from __future__ import annotations

import functools
from html.parser import HTMLParser
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from datetime import date, timedelta
from unittest.mock import patch

from test_home_preview_integration import guard_preview_io


ROOT = Path(__file__).resolve().parents[1]
WORKER = __name__ == "__main__" and "--discipline-preview-worker" in sys.argv


def isolated_preview(test):
    @functools.wraps(test)
    def run(self):
        if WORKER:
            return test(self)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("LUIGI_WEB_")}
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--discipline-preview-worker", ".".join(self.id().split(".")[-2:])],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    return run


class HabitCards(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.cards = {}
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "article" and "data-discipline-card" in attributes:
            self.current = {"attributes": attributes, "nodes": [], "text": []}
            self.cards[attributes["data-discipline-uuid"]] = self.current
        if self.current is not None:
            self.current["nodes"].append((tag, attributes))

    def handle_endtag(self, tag):
        if tag == "article":
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"].append(data)


class PreviewCase(unittest.TestCase):
    preview_options = {}

    def setUp(self):
        if not WORKER:
            return
        self.open_preview(**self.preview_options)

    def open_preview(self, **options):
        sys.path.insert(0, str(ROOT))
        from fastapi.testclient import TestClient
        from scripts.preview_workspace import preview_context

        self.addCleanup(lambda: self.assertFalse(self.directory.exists(), "Temporary preview was not removed"))
        app = self.enterContext(preview_context(**options))
        from luigi_web import application

        self.host = application
        self.db = application.db
        self.directory = Path(os.environ["LUIGI_WEB_DATA_DIR"])
        self.today = application.clock.local_today()
        self.client = TestClient(app, base_url="http://127.0.0.1:58308", follow_redirects=False)
        self.addCleanup(self.client.close)
        self.assertEqual(self.client.get("/discipline/progress").status_code, 200)
        self.headers = {"X-CSRF-Token": self.client.cookies["luigi_csrf"]}

    def tearDown(self):
        if WORKER and hasattr(self, "db"):
            self.db.get_engine.assert_not_called()

    def progress(self):
        response = self.client.get("/discipline/progress")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        state = response.json()
        self.assertEqual(state["today"], self.today.isoformat())
        return {row["uuid"]: row for row in state["disciplines"]}

    def post(self, path, expected=200, **fields):
        response = self.client.post(path, headers=self.headers, data=fields)
        self.assertEqual(response.status_code, expected)
        return response

    def history(self):
        return self.db.list_discipline_completions_between(date(self.today.year - 1, 1, 1), self.today)


class DisciplinePreviewTests(PreviewCase):
    @isolated_preview
    def test_completion_ranges_and_active_readback_are_disposable(self):
        before = self.db.get_discipline("preview-habit")
        start, end = self.today - timedelta(days=3), self.today - timedelta(days=1)
        history = self.db.list_discipline_completions_between(start, end)
        self.assertEqual(history[before["task"]], {
            (self.today - timedelta(days=offset)).isoformat() for offset in (1, 2, 3)
        })
        day = end.isoformat()
        self.assertEqual(self.db.list_discipline_completions_between(end, end)[before["task"]], {day})
        history[before["task"]].clear()
        self.assertTrue(self.db.list_discipline_completions_between(start, end)[before["task"]])
        history = self.db.list_discipline_completions_between(start, end)
        for active in (False, False, True, True):
            self.assertTrue(self.db.set_discipline_active("preview-habit", active))
            self.assertEqual(self.db.get_discipline("preview-habit"), {**before, "active": int(active)})
            self.assertEqual(bool(self.db.list_disciplines(False)), active)
            self.assertEqual(self.db.list_discipline_completions_between(start, end), history)
        self.assertFalse(self.db.set_discipline_active("preview-habit-missing", False))
        self.assertIsNone(self.db.get_discipline("preview-habit-missing"))

    @isolated_preview
    def test_uuid_heatmap_corrections_preserve_default_security(self):
        before = self.db.get_discipline("preview-habit")
        day = (self.today - timedelta(days=10)).isoformat()
        for action, marked in (("mark", True), ("unmark", False)):
            response = self.client.post("/discipline/toggle", headers=self.headers, data={
                "discipline_uuid": "preview-habit", "task": "Ignored example label", "day": day, "action": action,
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.db.completion_exists(before["task"], day), marked)
        self.assertEqual(self.client.post("/discipline/toggle", headers=self.headers).status_code, 403)
        response = self.client.post("/discipline/toggle", headers=self.headers, data={
            "task": before["task"], "day": day,
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "Synthetic discipline reference required"})
        self.assertEqual(len(self.db.list_disciplines()), 1)


class DisciplineDemoTests(PreviewCase):
    preview_options = {"discipline_demo": True}

    @isolated_preview
    def test_demo_renders_annual_heatmaps_weekly_targets_and_daily_only_streak(self):
        response = self.client.get("/discipline")
        self.assertEqual(response.status_code, 200)
        cards = HabitCards(response.text).cards
        self.assertEqual(set(cards), {"preview-habit", "preview-habit-reading", "preview-habit-daily", "preview-habit-paused"})
        self.assertEqual(len(self.db.list_disciplines()), 4)
        self.assertEqual(len(self.db.list_disciplines(False)), 3)
        for row in self.db.list_disciplines():
            card = cards[row["uuid"]]
            text = " ".join(card["text"])
            self.assertIn(f"/ {row['frequency_per_week']} this week", text)
            self.assertEqual("Daily streak:" in text, row["frequency_per_week"] == 7)
            self.assertEqual(card["attributes"]["data-weekly-available"], "true")
            cells = [attrs for tag, attrs in card["nodes"] if attrs.get("role") == "gridcell"]
            self.assertEqual(len(cells), (date(self.today.year + 1, 1, 1) - date(self.today.year, 1, 1)).days)
            self.assertTrue(any("data-discipline-pin" in attrs for tag, attrs in card["nodes"]))
        self.assertIn("Daily streak: 6 days", " ".join(cards["preview-habit-daily"]["text"]))
        self.assertIn("hidden", cards["preview-habit-paused"]["attributes"])
        for control in ("data-discipline-search", "data-discipline-category-filter", "data-discipline-status",
                        "data-discipline-target-filter", "data-discipline-organize", "data-discipline-reset-layout"):
            self.assertIn(control, response.text)
        history = self.history()
        self.assertGreaterEqual(len({day[:7] for days in history.values() for day in days}), 3)
        self.assertTrue(all(day <= self.today.isoformat() for days in history.values() for day in days))

    @isolated_preview
    def test_progress_mark_unmark_is_current_week_idempotent_and_canonical(self):
        before = self.progress()
        for row_uuid in ("preview-habit", "preview-habit-reading", "preview-habit-paused"):
            self.assertIsNone(before[row_uuid]["daily_streak"])
        monday = self.today - timedelta(days=self.today.weekday())
        for row_uuid, state in before.items():
            row = self.db.get_discipline(row_uuid)
            days = self.history()[row["task"]]
            self.assertEqual(state["weekly"]["count"], sum(monday.isoformat() <= day <= self.today.isoformat() for day in days))
        for _ in range(2):
            result = self.post("/discipline/preview-habit/today", action="mark", day="1999-01-01",
                               task="Ignored example title", catagory="Ignored example category").json()
            self.assertEqual(result["task"], "Practice a skill")
            self.assertEqual(result["day"], self.today.isoformat())
            self.assertTrue(result["marked"])
            state = self.progress()["preview-habit"]
            self.assertEqual(state["weekly"]["count"], before["preview-habit"]["weekly"]["count"] + 1)
            self.assertTrue(state["today_done"])
        for _ in range(2):
            self.assertFalse(self.post("/discipline/preview-habit/today", action="unmark").json()["marked"])
            self.assertEqual(self.progress(), before)

    @isolated_preview
    def test_daily_streak_uses_all_completion_history_after_corrections(self):
        self.assertEqual(self.progress()["preview-habit-daily"]["daily_streak"], 6)
        self.assertEqual(self.post("/discipline/preview-habit-daily/today", action="mark").json()["streak"], 7)
        day = (self.today - timedelta(days=1)).isoformat()
        for action, expected in (("unmark", 1), ("mark", 7)):
            self.post("/discipline/toggle", discipline_uuid="preview-habit-daily", day=day, action=action)
            self.assertEqual(self.progress()["preview-habit-daily"]["daily_streak"], expected)
            self.assertEqual(self.db.get_discipline("preview-habit-daily")["current_streak"], expected)
        self.post("/discipline/preview-habit-daily/today", action="unmark")
        self.assertEqual(self.progress()["preview-habit-daily"]["daily_streak"], 6)

    @isolated_preview
    def test_pause_resume_changes_only_active_state_and_home_visibility(self):
        before = self.db.get_discipline("preview-habit")
        history = self.history()
        progress = self.progress()["preview-habit"]
        for action, active in (("deactivate", False), ("deactivate", False), ("resume", True), ("resume", True)):
            response = self.post(f"/discipline/preview-habit/{action}", expected=204)
            self.assertEqual(response.headers["HX-Refresh"], "true")
            self.assertEqual(self.db.get_discipline("preview-habit"), {**before, "active": int(active)})
            self.assertEqual(self.history(), history)
            self.assertEqual(self.progress()["preview-habit"], {**progress, "active": active})
            self.assertEqual("preview-habit" in {row["uuid"] for row in self.db.list_disciplines(False)}, active)
            home = self.client.get("/home/data")
            self.assertEqual(home.status_code, 200)
            self.assertEqual("preview-habit" in {row["id"] for row in home.json()["habits"]}, active)

    @isolated_preview
    def test_paused_today_is_rejected_but_past_heatmap_correction_can_be_undone(self):
        history = self.history()
        response = self.post("/discipline/preview-habit-paused/today", expected=409, action="mark")
        self.assertEqual(response.json(), {"detail": "inactive disciplines cannot be marked"})
        response = self.post("/discipline/toggle", expected=409, discipline_uuid="preview-habit-paused",
                             day=self.today.isoformat(), action="mark")
        self.assertEqual(response.json(), {"detail": "Paused disciplines only allow past-date corrections"})
        day = (self.today - timedelta(days=10)).isoformat()
        title = self.db.get_discipline("preview-habit-paused")["task"]
        for action, marked in (("mark", True), ("mark", True), ("unmark", False), ("unmark", False)):
            response = self.post("/discipline/toggle", discipline_uuid="preview-habit-paused", day=day, action=action)
            self.assertEqual('class="heatmap-cell is-marked"' in response.text, marked)
            self.assertEqual(self.db.completion_exists(title, day), marked)
        self.assertEqual(self.history(), history)
        self.assertFalse(self.progress()["preview-habit-paused"]["active"])
        self.post("/discipline/preview-habit-paused/delete", expected=403)
        self.assertEqual(self.history(), history)

    @isolated_preview
    def test_unknown_references_and_dates_cannot_extend_synthetic_storage(self):
        history = self.history()
        today = self.today.isoformat()
        for row_uuid in ("foo", "preview-habit-missing"):
            response = self.post("/discipline/toggle", expected=404, discipline_uuid=row_uuid,
                                 task="Practice a skill", day=today, action="mark")
            self.assertEqual(response.json(), {"detail": "discipline not found"})
        for title in ("foo", "Practice a skill"):
            response = self.post("/discipline/toggle", expected=403, task=title, day=today, action="mark")
            self.assertEqual(response.json(), {"detail": "Synthetic discipline reference required"})
        for action in ("deactivate", "resume", "today"):
            response = self.post(f"/discipline/preview-habit-missing/{action}", expected=404)
            self.assertEqual(response.json(), {"detail": "discipline not found"})
        for day in ((self.today + timedelta(days=1)).isoformat(), f"{self.today.year - 2}-12-31"):
            self.post("/discipline/toggle", expected=422, discipline_uuid="preview-habit", day=day, action="mark")
        response = self.post("/discipline/toggle", expected=400, discipline_uuid="preview-habit", day="invalid")
        self.assertEqual(response.json(), {"detail": "day must be an ISO date"})
        for day in (today, "invalid", (self.today + timedelta(days=1)).isoformat()):
            self.assertFalse(self.db.mark_completion("foo", "Example category", day))
            self.assertFalse(self.db.unmark_completion("foo", day))
        self.assertEqual(self.history(), history)

    @isolated_preview
    def test_csrf_session_and_exact_action_allowlist_remain_enforced(self):
        history = self.history()
        for path in ("/discipline/preview-habit/deactivate", "/discipline/preview-habit/resume",
                     "/discipline/preview-habit/today", "/discipline/toggle"):
            for headers in ({}, {"X-CSRF-Token": "incorrect"}):
                response = self.client.post(path, headers=headers, data={
                    "discipline_uuid": "preview-habit", "day": self.today.isoformat(), "action": "mark",
                })
                self.assertEqual(response.status_code, 403)
        for path in ("/discipline", "/discipline/preview-habit", "/discipline/preview-habit/delete",
                     "/discipline/unknown-id/today", "/discipline/preview-habit/extra/today",
                     f"/discipline/preview-habit-{'a' * 49}/today", "/admin/update", "/gnw", "/feedback", "/chat/send"):
            self.post(path, expected=403)
        response = self.client.post("/discipline/preview-habit/today", headers={**self.headers, "Origin": "https://example.invalid"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "Same-origin requests only"})
        self.assertEqual(self.client.get("/discipline", headers={"Host": "example.invalid"}).status_code, 403)
        self.assertEqual(self.history(), history)
        self.assertTrue(self.db.get_discipline("preview-habit")["active"])
        self.assertIn("luigi_preview_session_58308", self.client.cookies)
        self.assertNotIn("luigi_preview_session_80", self.client.cookies)
        self.client.cookies.clear()
        response = self.client.post("/discipline/preview-habit/today", headers=self.headers)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Preview session required"})
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.history(), history)
        self.db.get_engine.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "Shared storage disabled in preview"):
            self.db.get_engine()
        self.db.get_engine.reset_mock()

    @isolated_preview
    def test_progress_and_pause_failures_are_generic_and_never_successful(self):
        history = self.history()
        with patch.object(self.db, "set_discipline_active", return_value=False):
            response = self.post("/discipline/preview-habit/deactivate", expected=503)
        self.assertEqual(response.json(), {"detail": "Discipline state could not be saved"})
        self.assertNotIn("HX-Trigger", response.headers)
        with patch.object(self.db, "list_discipline_completions_between", side_effect=RuntimeError("Synthetic failure")):
            response = self.client.get("/discipline/progress")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Discipline progress is unavailable"})
        self.assertNotIn("Synthetic failure", response.text)
        self.assertEqual(self.history(), history)
        self.assertTrue(self.db.get_discipline("preview-habit")["active"])

    @isolated_preview
    def test_history_example_uses_only_its_own_synthetic_months(self):
        history = self.history()
        with ExitStack() as stack:
            guards = [stack.enter_context(patch.object(self.db, name, side_effect=AssertionError("Record queries forbidden")))
                      for name in ("list_disciplines", "get_discipline", "list_completions_for_year",
                                   "list_discipline_completions_between", "list_completion_tasks_for_day",
                                   "computed_discipline_streak", "list_tasks")]
            response = self.client.get("/discipline/history-preview")
            self.assertEqual(response.status_code, 200)
            for guard in guards:
                guard.assert_not_called()
        self.assertEqual(response.headers["cache-control"], "no-store")
        for fixture in ("Example reading practice", "2030-03-18", "2030-04-24"):
            self.assertIn(fixture, response.text)
        self.assertNotIn("data-discipline-card", response.text)
        self.assertEqual(self.history(), history)


class DisciplinePreviewCliTests(unittest.TestCase):
    def check_cli(self, *flags):
        sys.path.insert(0, str(ROOT))
        from scripts import preview_workspace

        original = preview_workspace.preview_context
        directories = []

        @contextmanager
        def checked_context(*args, **kwargs):
            with original(*args, **kwargs) as app:
                from luigi_web import application

                directories.append(Path(os.environ["LUIGI_WEB_DATA_DIR"]))
                self.assertEqual(len(application.db.list_disciplines()), 4)
                self.assertEqual(len(application.db.list_recurring()), 3 if "--occurrence-demo" in flags else 1)
                try:
                    yield app
                finally:
                    application.db.get_engine.assert_not_called()

        output = StringIO()
        with patch.object(sys, "argv", ["preview_workspace.py", "--check", *flags]), \
             patch.object(preview_workspace, "preview_context", side_effect=checked_context) as context, \
             patch("uvicorn.Server", side_effect=AssertionError("Preview server startup forbidden")) as server, \
             redirect_stdout(output):
            preview_workspace.main()
        context.assert_called_once_with(occurrence_demo="--occurrence-demo" in flags, discipline_demo=True)
        server.assert_not_called()
        self.assertEqual(output.getvalue().strip(), "Validated 16 synthetic workspace endpoints without external services.")
        self.assertTrue(directories)
        self.assertTrue(all(not directory.exists() for directory in directories))

    @isolated_preview
    def test_discipline_demo_cli_check(self):
        self.check_cli("--discipline-demo")

    @isolated_preview
    def test_combined_demo_cli_check(self):
        self.check_cli("--occurrence-demo", "--discipline-demo")


if __name__ == "__main__":
    if WORKER:
        sys.addaudithook(guard_preview_io)
        unittest.main(argv=[sys.argv[0], sys.argv[-1]], verbosity=2)
    else:
        unittest.main()