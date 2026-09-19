"""Live history against disposable preview state, never the shared engine."""
from __future__ import annotations

import functools
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import date, timedelta
from unittest.mock import patch

from test_discipline_preview import HabitCards
from test_home_preview_integration import guard_preview_io


ROOT = Path(__file__).resolve().parents[1]
WORKER = __name__ == "__main__" and "--history-preview-worker" in sys.argv


def isolated_preview(test):
    @functools.wraps(test)
    def run(self):
        if WORKER:
            return test(self)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("LUIGI_WEB_")}
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--history-preview-worker",
             ".".join(self.id().split(".")[-2:])],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    return run


class HistoryPreviewCase(unittest.TestCase):
    preview_options = {}

    def setUp(self):
        if not WORKER:
            return
        sys.path.insert(0, str(ROOT))
        from fastapi.testclient import TestClient
        from scripts.preview_workspace import preview_context

        self.addCleanup(lambda: self.assertFalse(self.directory.exists(), "Temporary preview was not removed"))
        app = self.enterContext(preview_context(**self.preview_options))
        from luigi_web import application

        self.host = application
        self.db = application.db
        self.directory = Path(os.environ["LUIGI_WEB_DATA_DIR"])
        self.today = application.clock.local_today()
        self.client = TestClient(app, base_url="http://127.0.0.1:58309", follow_redirects=False)
        self.addCleanup(self.client.close)
        self.assertEqual(self.client.get("/discipline/progress").status_code, 200)
        self.headers = {"X-CSRF-Token": self.client.cookies["luigi_csrf"]}

    def tearDown(self):
        if WORKER and hasattr(self, "db"):
            self.db.get_engine.assert_not_called()

    def state(self, row_uuid="preview-habit", year=None):
        response = self.client.get(f"/discipline/{row_uuid}/history/data", params={"year": year or self.today.year})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    def post(self, path, expected=200, **fields):
        response = self.client.post(path, headers=self.headers, data=fields)
        self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response

    def change(self, day, action, row_uuid="preview-habit", expected=200, version=None):
        state = self.state(row_uuid, day.year)
        completion = next((row for row in state["completions"] if row["date"] == day.isoformat()), None)
        return self.post(
            f"/discipline/{row_uuid}/history", expected=expected,
            day=day.isoformat(), action=action, year=day.year,
            expected_version=version if version is not None else completion["version"] if completion else state["emptyVersion"],
        )

    def progress(self, row_uuid="preview-habit"):
        response = self.client.get("/discipline/progress")
        self.assertEqual(response.status_code, 200)
        return next(row for row in response.json()["disciplines"] if row["uuid"] == row_uuid)

    def assert_shared_day(self, day, marked):
        state = self.state(year=day.year)
        task = state["name"]
        self.assertEqual(day.isoformat() in {row["date"] for row in state["completions"]}, marked)
        self.assertEqual(self.db.completion_exists(task, day.isoformat()), marked)
        self.assertEqual(day.isoformat() in self.db.list_completions_for_year(day.year)[task], marked)
        self.assertEqual(task in self.db.list_completion_tasks_for_day(day.isoformat()), marked)
        self.assertEqual(state["weekly"], self.progress()["weekly"])
        if day == self.today:
            self.assertEqual(self.progress()["today_done"], marked)
            home = self.client.get("/home/data")
            self.assertEqual(home.status_code, 200)
            habit = next(row for row in home.json()["habits"] if row["id"] == "preview-habit")
            self.assertEqual(habit["done"], marked)


class DisciplineHistoryPreviewTests(HistoryPreviewCase):
    @isolated_preview
    def test_mark_reload_remove_and_undo_preserve_original_timestamp(self):
        day = self.today - timedelta(days=10)
        with patch.object(self.db, "now_iso", return_value=f"{self.today}T09:15:00"):
            response = self.change(day, "mark").json()
        self.assertEqual(response["undo_ttl_ms"], 12000)
        marked = self.state(year=day.year)
        self.assertEqual(response["state"], marked)
        completion = next(row for row in marked["completions"] if row["date"] == day.isoformat())
        self.assertIsNotNone(completion["loggedAt"])
        removed = self.change(day, "unmark").json()
        self.assertNotIn(day.isoformat(), {row["date"] for row in self.state(year=day.year)["completions"]})
        with patch.object(self.db, "now_iso", return_value=f"{self.today}T10:30:00"):
            restored = self.post("/discipline/preview-habit/history/undo", token=removed["undo_token"], year=day.year).json()
        self.assertEqual(restored["state"], marked)
        self.assertEqual(self.state(year=day.year), marked)
        seeded = self.today - timedelta(days=1)
        task = marked["name"]
        original = self.db.list_discipline_history(task, seeded, seeded)
        removed = self.change(seeded, "unmark").json()
        self.post("/discipline/preview-habit/history/undo", token=removed["undo_token"], year=seeded.year)
        self.assertEqual(self.db.list_discipline_history(task, seeded, seeded), original)

    @isolated_preview
    def test_live_render_no_store_assets_and_weekly_match_main(self):
        self.assertEqual(len(self.db.list_disciplines()), 1)
        state = self.state()
        page = self.client.get("/discipline/preview-habit/history")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertIn('id="dh-live"', page.text)
        self.assertIn("/module-assets/discipline/history.js", page.text)
        self.assertNotIn("history-example.js", page.text)
        self.assertNotIn('type="time"', page.text)
        for view in ("month", "year", "log"):
            self.assertIn(f'id="dh-{view}-tab"', page.text)
        script = self.client.get("/module-assets/discipline/history.js")
        self.assertEqual(script.status_code, 200)
        self.assertNotIn("localStorage", script.text)
        self.assertNotIn("sessionStorage", script.text)
        main = self.client.get("/discipline")
        self.assertEqual(main.status_code, 200)
        card = HabitCards(main.text).cards["preview-habit"]
        self.assertIn(f"/discipline/preview-habit/history?year={self.today.year}",
                  {attrs.get("href") for _, attrs in card["nodes"]})
        self.assertEqual(len([attrs for _, attrs in card["nodes"] if attrs.get("role") == "gridcell"]),
                         (date(self.today.year + 1, 1, 1) - date(self.today.year, 1, 1)).days)
        self.assertIn(f"/ {state['weeklyTarget']} this week", " ".join(card["text"]))
        self.assertEqual(state["weekly"], self.progress()["weekly"])
        self.assertEqual(self.state(year=self.today.year - 1)["weekly"], state["weekly"])
        self.assertTrue(all("completedAt" not in row for row in state["completions"]))

    @isolated_preview
    def test_history_today_and_heatmap_share_completion_state(self):
        before = self.state()
        marked = self.change(self.today, "mark").json()
        self.assertEqual(marked["state"]["weekly"]["count"], before["weekly"]["count"] + 1)
        self.assert_shared_day(self.today, True)
        repeated = self.change(self.today, "mark").json()
        self.assertIsNone(repeated["undo_token"])
        self.assertEqual(repeated["state"], marked["state"])
        version = next(row["version"] for row in marked["state"]["completions"] if row["date"] == self.today.isoformat())
        self.post("/discipline/preview-habit/today", action="unmark")
        self.assert_shared_day(self.today, False)
        self.change(self.today, "unmark", version=version, expected=409)
        self.post("/discipline/preview-habit/today", action="mark")
        self.assert_shared_day(self.today, True)
        current = self.state()
        self.post("/discipline/preview-habit/today", action="mark")
        self.assertEqual(self.state(), current)
        self.post("/discipline/toggle", discipline_uuid="preview-habit", day=self.today.isoformat(), action="unmark")
        self.assert_shared_day(self.today, False)
        past = self.today - timedelta(days=10)
        self.post("/discipline/toggle", discipline_uuid="preview-habit", day=past.isoformat(), action="mark")
        self.assert_shared_day(past, True)
        self.change(past, "unmark")
        self.assert_shared_day(past, False)

    @isolated_preview
    def test_pause_blocks_today_and_undo_but_allows_past_corrections(self):
        today_change = self.change(self.today, "mark").json()
        self.post("/discipline/preview-habit/deactivate", expected=204)
        self.assertFalse(self.state()["active"])
        for action in ("mark", "unmark"):
            self.change(self.today, action, expected=409)
        self.post("/discipline/preview-habit/history/undo", expected=409,
                  token=today_change["undo_token"], year=self.today.year)
        past = self.today - timedelta(days=1)
        before = self.state(year=past.year)
        removed = self.change(past, "unmark").json()
        self.post("/discipline/preview-habit/history/undo", token=removed["undo_token"], year=past.year)
        self.assertEqual(self.state(year=past.year), before)
        past = self.today - timedelta(days=10)
        before = self.state(year=past.year)
        marked = self.change(past, "mark").json()
        self.post("/discipline/preview-habit/history/undo", token=marked["undo_token"], year=past.year)
        self.assertEqual(self.state(year=past.year), before)
        self.post("/discipline/preview-habit/resume", expected=204)
        self.post("/discipline/preview-habit/history/undo", token=today_change["undo_token"], year=self.today.year)
        self.assert_shared_day(self.today, False)

    @isolated_preview
    def test_bad_versions_dates_and_actions_never_write(self):
        before = self.state()
        for version, status in (("", 400), ("invalid", 400), ("0" * 64, 409)):
            response = self.change(self.today, "mark", version=version, expected=status)
            self.assertNotIn("state", response.json())
        seeded = self.today - timedelta(days=1)
        response = self.change(seeded, "unmark", version=before["emptyVersion"], expected=409)
        self.assertIn("Reload before saving", response.json()["detail"])
        valid = {"day": self.today.isoformat(), "year": self.today.year,
                 "action": "mark", "expected_version": before["emptyVersion"]}
        for overrides, status in (
            ({"action": "toggle"}, 400), ({"day": "invalid"}, 422),
            ({"day": (self.today + timedelta(days=1)).isoformat()}, 422),
            ({"day": f"{self.today.year}-02-30"}, 422),
            ({"year": self.today.year - 1}, 422), ({"year": "invalid"}, 422),
        ):
            self.post("/discipline/preview-habit/history", expected=status, **(valid | overrides))
        self.change(date(self.today.year - 2, 12, 31), "mark", expected=409)
        self.assertEqual(self.state(), before)

    @isolated_preview
    def test_bounded_adapters_copy_rows_and_block_unknown_storage(self):
        task = self.state()["name"]
        day = self.today - timedelta(days=1)
        original = self.db.list_discipline_history(task, day, day)
        self.assertEqual(len(original), 1)
        self.assertEqual(original[0]["logged_at"], f"{day}T12:00:00")
        self.assertEqual(self.db.list_discipline_history(f" {task.upper()} ", day, day), original)
        copied = self.db.list_discipline_history(task, day, day)
        copied[0]["logged_at"] = None
        copied.clear()
        self.assertEqual(self.db.list_discipline_history(task, day, day), original)
        for start, end in ((day, day - timedelta(days=1)), (day - timedelta(days=371), day)):
            with self.assertRaisesRegex(ValueError, "bounded date range"):
                self.db.list_discipline_history(task, start, end)
        self.assertEqual(self.db.list_discipline_history("Unknown example", day, day), [])
        version = self.db.discipline_history_version(original)
        for row_uuid in ("unknown", "preview-habit-missing"):
            with self.assertRaises(self.db.DisciplineHistoryConflict):
                self.db.change_discipline_history(row_uuid, day, version, False)
        with self.assertRaises(self.db.DisciplineHistoryConflict):
            self.db.change_discipline_history("preview-habit", day, version, True, restore={
                "task": "Unknown example", "day": day.isoformat(), "before": original,
            })
        self.assertEqual(self.db.list_discipline_history(task, day, day), original)
        self.db.get_engine.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "Shared storage disabled in preview"):
            self.db.get_engine()
        self.db.get_engine.reset_mock()

    @isolated_preview
    def test_unknown_paths_methods_origin_and_csrf_cannot_mutate(self):
        before = self.state()
        fields = {"day": self.today.isoformat(), "year": self.today.year,
                  "action": "mark", "expected_version": before["emptyVersion"], "token": "synthetic-invalid"}
        for suffix in ("history", "history/undo"):
            path = f"/discipline/preview-habit/{suffix}"
            for headers in ({}, {"X-CSRF-Token": "incorrect"},
                            {**self.headers, "Origin": "https://example.invalid"}):
                response = self.client.post(path, headers=headers, data=fields)
                self.assertEqual(response.status_code, 403)
            for method in ("PUT", "PATCH", "DELETE"):
                self.assertEqual(self.client.request(method, path, headers=self.headers, data=fields).status_code, 403)
        for path in (
            "/discipline/unknown/history", "/discipline/preview-habit-missing/history",
            "/discipline/preview-habit-missing/history/undo", "/discipline/preview-habit-paused/history",
            "/discipline/preview-habit/history/", "/discipline/preview-habit/history/data",
            "/discipline/preview-habit/history/undo/extra", "/discipline/preview-habit/extra/history",
            f"/discipline/preview-habit-{'a' * 49}/history", "/discipline/preview-habit/delete",
        ):
            self.assertEqual(self.client.post(path, headers=self.headers, data=fields).status_code, 403)
        for suffix in ("history", "history/data"):
            response = self.client.get(f"/discipline/preview-habit-missing/{suffix}")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/discipline/preview-habit/history", headers={"Host": "example.invalid"}).status_code, 403)
        self.assertEqual(self.state(), before)

    @isolated_preview
    def test_preview_session_required_for_both_history_mutations(self):
        state = self.state()
        self.client.cookies.clear()
        for suffix in ("history", "history/undo"):
            response = self.client.post(f"/discipline/preview-habit/{suffix}", headers=self.headers, data={
                "day": self.today.isoformat(), "year": self.today.year, "action": "mark",
                "expected_version": state["emptyVersion"], "token": "synthetic-invalid",
            })
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"detail": "Preview session required"})
            self.assertNotIn("set-cookie", response.headers)
        self.client.cookies.set("luigi_preview_session_58309", "incorrect")
        self.assertEqual(self.client.post("/discipline/preview-habit/history", headers=self.headers).status_code, 401)
        self.client.cookies.clear()
        self.assertEqual(self.state(), state)
        self.assertIn("luigi_preview_session_58309", self.client.cookies)
        self.assertNotIn("luigi_preview_session_80", self.client.cookies)

    @isolated_preview
    def test_undo_expiry_reuse_and_conflict_do_not_overwrite(self):
        from luigi_web.modules.discipline import history

        changed = self.change(self.today, "mark").json()
        token = changed["undo_token"]
        with patch.object(history.time, "monotonic", return_value=history._undo[token]["expires"] + 1):
            self.post("/discipline/preview-habit/history/undo", token=token, year=self.today.year, expected=409)
        self.assert_shared_day(self.today, True)
        removed = self.change(self.today, "unmark").json()
        self.post("/discipline/preview-habit/today", action="mark")
        before = self.state()
        self.post("/discipline/preview-habit/history/undo", token=removed["undo_token"], year=self.today.year, expected=409)
        self.assertEqual(self.state(), before)
        removed = self.change(self.today, "unmark").json()
        self.post("/discipline/preview-habit/history/undo", token=removed["undo_token"], year=self.today.year)
        self.post("/discipline/preview-habit/history/undo", token=removed["undo_token"], year=self.today.year, expected=409)

    @isolated_preview
    def test_adapter_errors_require_reload_without_success_state(self):
        before = self.state()
        with patch.object(self.db, "list_discipline_history", side_effect=RuntimeError("Synthetic storage failure")):
            response = self.client.get("/discipline/preview-habit/history/data")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), {"detail": "Discipline history is unavailable"})
        with patch.object(self.db, "change_discipline_history", side_effect=RuntimeError("Synthetic storage failure")):
            response = self.change(self.today, "mark", expected=503)
        self.assertIn("Reload before another change", response.json()["detail"])
        self.assertNotIn("Synthetic storage failure", response.text)
        self.assertNotIn("state", response.json())
        self.assertEqual(self.state(), before)


class DisciplineHistoryPreviewCliTests(unittest.TestCase):
    def check_cli(self, *flags):
        sys.path.insert(0, str(ROOT))
        from fastapi.testclient import TestClient
        from scripts import preview_workspace

        original = preview_workspace.preview_context
        directories = []

        @contextmanager
        def checked_context(*args, **kwargs):
            with original(*args, **kwargs) as app:
                from luigi_web import application

                directories.append(Path(os.environ["LUIGI_WEB_DATA_DIR"]))
                self.assertEqual(len(application.db.list_disciplines()), 4 if "--discipline-demo" in flags else 1)
                self.assertEqual(len(application.db.list_recurring()), 3 if "--occurrence-demo" in flags else 1)
                client = TestClient(app)
                try:
                    progress = client.get("/discipline/progress").json()["disciplines"]
                    for row in progress:
                        path = f"/discipline/{row['uuid']}/history"
                        page = client.get(path)
                        self.assertEqual(page.status_code, 200)
                        self.assertEqual(page.headers["cache-control"], "no-store")
                        self.assertIn('id="dh-live"', page.text)
                        state = client.get(path + "/data").json()
                        self.assertEqual(state["weekly"], row["weekly"])
                        self.assertEqual(state["active"], row["active"])
                        day = application.clock.local_today() - timedelta(days=10)
                        before = client.get(path + f"/data?year={day.year}").json()
                        headers = {"X-CSRF-Token": client.cookies["luigi_csrf"]}
                        change = client.post(path, headers=headers, data={
                            "day": day.isoformat(), "year": day.year, "action": "mark",
                            "expected_version": before["emptyVersion"],
                        })
                        self.assertEqual(change.status_code, 200, change.text)
                        undo = client.post(path + "/undo", headers=headers, data={
                            "token": change.json()["undo_token"], "year": day.year,
                        })
                        self.assertEqual(undo.status_code, 200, undo.text)
                        self.assertEqual(undo.json()["state"], before)
                    with patch.object(application.db, "list_discipline_history", side_effect=AssertionError("Example must stay separate")) as guard:
                        example = client.get("/discipline/history-preview")
                    self.assertEqual(example.status_code, 200)
                    self.assertIn("2030-04-24", example.text)
                    self.assertNotIn('id="dh-live"', example.text)
                    guard.assert_not_called()
                    yield app
                finally:
                    client.close()
                    application.db.get_engine.assert_not_called()

        output = StringIO()
        with patch.object(sys, "argv", ["preview_workspace.py", "--check", *flags]), \
             patch.object(preview_workspace, "preview_context", side_effect=checked_context), \
             patch("uvicorn.Server", side_effect=AssertionError("Preview server startup forbidden")) as server, \
             redirect_stdout(output):
            preview_workspace.main()
        server.assert_not_called()
        self.assertEqual(output.getvalue().strip(), "Validated 16 synthetic workspace endpoints without external services.")
        self.assertTrue(directories)
        self.assertTrue(all(not directory.exists() for directory in directories))

    @isolated_preview
    def test_default_cli_history(self):
        self.check_cli()

    @isolated_preview
    def test_discipline_demo_cli_history(self):
        self.check_cli("--discipline-demo")

    @isolated_preview
    def test_combined_demo_cli_history(self):
        self.check_cli("--discipline-demo", "--occurrence-demo")


if __name__ == "__main__":
    if WORKER:
        sys.addaudithook(guard_preview_io)
        unittest.main(argv=[sys.argv[0], sys.argv[-1]], verbosity=2)
    else:
        unittest.main()