"""Exercise the disposable preview in fresh processes, without ASGI startup."""
from __future__ import annotations

import functools
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORKER = __name__ == "__main__" and "--preview-worker" in sys.argv


def guard_preview_io(event, args):
    if event == "socket.connect":
        frame = sys._getframe(1)
        while frame:
            if frame.f_code.co_name == "_fallback_socketpair" and frame.f_globals.get("__name__") == "socket":
                return
            frame = frame.f_back
        raise AssertionError("External connections forbidden in preview tests")
    if event == "sqlite3.connect" and str(args[0]) not in {"", ":memory:"}:
        if not Path(args[0]).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise AssertionError("Only temporary SQLite storage is allowed")
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        target = Path(os.fsdecode(args[0])).resolve()
        if target.is_relative_to(ROOT / "data") or target.name.startswith((".env", "LOCAL_")):
            raise AssertionError("Protected local files are forbidden in preview tests")


def isolated_preview(test):
    @functools.wraps(test)
    def run(self):
        if WORKER:
            return test(self)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("LUIGI_WEB_")}
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--preview-worker", self.id().split(".")[-1]],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    return run


class HomePreviewIntegrationTests(unittest.TestCase):
    def setUp(self):
        if not WORKER:
            return
        sys.path.insert(0, str(ROOT))
        from fastapi.testclient import TestClient
        from scripts.preview_workspace import preview_context

        self.addCleanup(lambda: self.assertFalse(self.directory.exists(), "Temporary preview was not removed"))
        app = self.enterContext(preview_context())
        from luigi_web import application

        self.host = application
        self.db = application.db
        self.directory = Path(os.environ["LUIGI_WEB_DATA_DIR"])
        self.client = TestClient(app, base_url="http://127.0.0.1:58306", follow_redirects=False)
        self.addCleanup(self.client.close)
        response = self.client.get("/home/data")
        self.assertEqual(response.status_code, 200, response.text)
        self.today = response.json()["today"]
        self.headers = {"X-CSRF-Token": self.client.cookies["luigi_csrf"]}

    def tearDown(self):
        if WORKER and hasattr(self, "db"):
            self.db.get_engine.assert_not_called()

    def state(self):
        response = self.client.get("/home/data")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["today"], self.today)
        return response.json()

    def task(self, row_uuid="preview-task-1"):
        return next(row for row in self.state()["tasks"] if row["uuid"] == row_uuid)

    def post(self, path, **data):
        response = self.client.post(path, data=data, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def undo(self, response):
        op_id = json.loads(response.headers["HX-Trigger"])["showUndo"]["op_id"]
        self.post(f"/undo/{op_id}")

    @isolated_preview
    def test_reschedule_complete_reopen_and_undo_keep_today(self):
        initial = self.task()
        self.assertTrue(initial["today"])
        response = self.post("/home/reschedule", source="task", uuid=initial["uuid"], due_date="2026-10-01")
        self.assertEqual(self.task(), {**initial, "due": "2026-10-01"})
        self.undo(response)
        self.assertEqual(self.task(), initial)
        response = self.post("/tasks/preview-task-1/complete")
        completed = self.task()
        self.assertTrue(completed["done"])
        self.assertTrue(completed["today"])
        self.assertEqual(completed["due"], initial["due"])
        reopened = self.post("/tasks/preview-task-1/complete")
        self.assertFalse(self.task()["done"])
        self.undo(reopened)
        self.assertEqual(self.task(), completed)
        self.undo(response)
        self.assertEqual(self.task(), initial)

    @isolated_preview
    def test_initial_state_and_real_pages_use_synthetic_rows(self):
        state = self.state()
        self.assertEqual(len(state["tasks"]), 6)
        self.assertEqual({row["uuid"] for row in state["tasks"] if row["today"]},
                         {"preview-task-1", "preview-task-2"})
        self.assertEqual(len(state["habits"]), 1)
        self.assertFalse(state["habits"][0]["done"])
        self.assertEqual(state["habits"][0]["streak"], 3)
        self.assertEqual([row["uuid"] for row in self.db.list_recurring()], ["preview-recurring-1"])
        self.assertEqual(self.db.get_recurring("preview-recurring-1")["recurring_interval"], 7)
        for row in self.db.list_tasks():
            self.assertTrue(set(self.db._TASK_COLUMNS).issubset(row))
            self.assertTrue(row["uuid"].startswith("preview-task-"))
        for path in ("/modules", "/home", "/home/layout", "/home/preview", "/home/data", "/chat/panel",
                     "/tasks", "/calendar", "/projects", "/discipline", "/cards/mtg/decks",
                     "/characters", "/finance/unlock", "/tasks/new", "/tasks/preview-task-1/edit"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('data-home-data="/home/data"', self.client.get("/home").text)

    @isolated_preview
    def test_today_selection_is_independent_of_due_and_completion(self):
        before = self.db.get_task("preview-task-3")
        self.assertFalse(self.task("preview-task-3")["today"])
        self.post("/home/today", source="task", uuid=before["uuid"], day=self.today, selected="true")
        self.assertTrue(self.task("preview-task-3")["today"])
        self.assertEqual(self.db.get_task(before["uuid"]), before)
        self.post("/home/today", source="task", uuid=before["uuid"], day=self.today, selected="false")
        response = self.post("/home/reschedule", source="task", uuid=before["uuid"], due_date=self.today)
        self.assertFalse(self.task("preview-task-3")["today"])
        self.undo(response)
        response = self.post("/home/reschedule", source="task", uuid=before["uuid"], due_date="")
        self.assertEqual(self.task("preview-task-3")["due"], "")
        self.undo(response)
        self.assertEqual(self.db.get_task(before["uuid"]), before)
        response = self.client.post("/home/today", data={"source": "task", "uuid": before["uuid"],
            "day": (date.fromisoformat(self.today) - timedelta(days=1)).isoformat(), "selected": "true"}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.task("preview-task-3")["today"])

    @isolated_preview
    def test_create_and_edit_forms_persist_and_ignore_private_fields(self):
        initial_ids = {row["uuid"] for row in self.state()["tasks"]}
        self.post("/tasks", task="Example added task", priority="4", status="Not Started", due_date=self.today,
                  project="Example second project", catagory="Example category", task_group="Example group",
                  sub_group="Example subgroup", relevant_link="https://example.invalid/", estimated_time="1.5",
                  uuid="not-an-allowed-id", archived="1", completed="1", table="finance")
        added = next(row for row in self.state()["tasks"] if row["uuid"] not in initial_ids)
        self.assertRegex(added["uuid"], r"^preview-task-[a-f0-9]{32}$")
        self.assertFalse(added["today"])
        self.assertFalse(added["done"])
        row_uuid = added["uuid"]
        form = self.client.get(f"/tasks/{row_uuid}/edit")
        self.assertEqual(form.status_code, 200)
        self.assertIn('value="Example added task"', form.text)
        self.post("/home/today", source="task", uuid=row_uuid, selected="true", day=self.today)
        response = self.post(f"/tasks/{row_uuid}", task="Example edited <task>", status="In Progress", priority="6",
                            due_date="", project="Example changed project", estimated_time="2.5", uuid="untrusted-id")
        self.assertIn("Example edited &lt;task&gt;", response.text)
        self.assertIn("Example edited &lt;task&gt;", self.client.get("/home").text)
        changed = self.task(row_uuid)
        self.assertTrue(changed["today"])
        self.assertEqual(changed["title"], "Example edited <task>")
        self.assertEqual(changed["priority"], 6)
        self.assertEqual(changed["due"], "")
        raw = self.db.get_task(row_uuid)
        self.assertEqual(raw["project"], "Example changed project")
        self.assertEqual(raw["estimated_time"], 2.5)
        self.assertEqual(raw["archived"], 0)
        self.assertNotIn("table", raw)
        self.assertIn(row_uuid, {row["uuid"] for row in self.db.list_project_rows([raw["project"]])})
        response = self.post(f"/tasks/{row_uuid}/complete")
        self.assertTrue(self.task(row_uuid)["done"])
        self.assertNotIn(row_uuid, {row["uuid"] for row in self.db.list_project_rows([raw["project"]])})
        self.undo(response)
        self.assertEqual(self.db.get_task(row_uuid), raw)

    @isolated_preview
    def test_habit_mark_unmark_is_idempotent_and_coherent(self):
        before = self.state()
        for _ in range(2):
            result = self.post("/discipline/preview-habit/today", action="mark", day="1999-01-01",
                               task="Ignored example title", catagory="Ignored example category").json()
            self.assertEqual(result["day"], self.today)
            self.assertTrue(result["marked"])
            self.assertEqual(result["streak"], 4)
            self.assertTrue(self.state()["habits"][0]["done"])
            self.assertEqual(self.db.list_disciplines_pending_today(), [])
            self.assertEqual(self.db.list_completion_tasks_for_day(self.today), {"Practice a skill"})
            self.assertIn(self.today, self.db.list_completions_for_year(date.fromisoformat(self.today).year)["Practice a skill"])
            self.assertEqual(next(row["count"] for row in self.db.weekly_discipline_counts() if row["date"] == self.today), 1)
            self.assertEqual(self.db.get_discipline("preview-habit")["current_streak"], 4)
        for _ in range(2):
            self.assertFalse(self.post("/discipline/preview-habit/today", action="unmark").json()["marked"])
            self.assertEqual(self.state(), before)
            self.assertEqual(len(self.db.list_disciplines_pending_today()), 1)
            self.assertNotIn(self.today, self.db.list_completions_for_year(date.fromisoformat(self.today).year)["Practice a skill"])
        self.assertEqual(self.client.get("/discipline").status_code, 200)

    @isolated_preview
    def test_global_csrf_scoped_session_and_forbidden_actions(self):
        before = self.state()
        for path in ("/home/today", "/home/reschedule", "/tasks", "/tasks/preview-task-1",
                     "/tasks/preview-task-1/complete", "/discipline/preview-habit/today", "/undo/example"):
            with self.subTest(path=path):
                for headers in ({}, {"X-CSRF-Token": "incorrect"}):
                    self.assertEqual(self.client.post(path, headers=headers).status_code, 403)
        for path in ("/admin/update", "/gnw", "/feedback", "/chat", "/chat/send", "/cards/mtg/refresh",
                     "/tasks/preview-task-1/delete", "/tasks/preview-task-1/archive", "/tasks/example/status",
                     "/tasks/preview-task-1/snooze", "/tasks/bulk", "/recurring/unknown-id", "/discipline/toggle"):
            self.assertEqual(self.client.post(path, headers=self.headers).status_code, 403)
        self.assertEqual(self.client.post("/home/today", headers={**self.headers, "Origin": "https://example.invalid"}).status_code, 403)
        self.assertEqual(self.client.get("/home", headers={"Host": "example.invalid"}).status_code, 403)
        self.assertEqual(self.state(), before)
        self.client.cookies.clear()
        response = self.client.post("/tasks", data={"task": "Example rejected task"}, headers=self.headers)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(len(self.client.cookies), 0)
        self.assertEqual(self.state(), before)

    @isolated_preview
    def test_unknown_ids_never_fall_through_to_shared_storage(self):
        before = self.state()
        for source in ("task", "recurring"):
            for row_uuid in ("preview-task-missing", "unknown-id"):
                for path, fields in (("/home/today", {"selected": "true", "day": self.today}),
                                     ("/home/reschedule", {"due_date": self.today})):
                    response = self.client.post(path, data={"source": source, "uuid": row_uuid, **fields}, headers=self.headers)
                    self.assertEqual(response.status_code, 404, response.text)
        for path in ("/tasks/preview-task-missing", "/tasks/preview-task-missing/complete"):
            self.assertEqual(self.client.post(path, data={"task": "Example missing"}, headers=self.headers).status_code, 404)
        for path in ("/tasks/unknown-id/edit", "/recurring/unknown-id/edit"):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertIsNone(self.db.get_discipline("unknown-id"))
        self.assertEqual(self.client.post("/undo/unknown-id", headers=self.headers).status_code, 410)
        self.assertEqual(self.state(), before)

    @isolated_preview
    def test_dependencies_block_transitions_but_not_reschedule(self):
        self.host.operations.add_dependency(
            dependent_uuid="preview-task-1", dependent_source="task", dependent_label="Example dependent",
            blocker_uuid="preview-task-3", blocker_source="task", blocker_label="Example blocker",
        )
        before = self.db.get_task("preview-task-1")
        self.assertEqual(self.task()["blockers"], ["Plan the next release"])
        for path, fields in (("/tasks/preview-task-1/complete", {}),
                             ("/tasks/preview-task-1", {"status": "Completed", "task": "Example rejected edit"})):
            response = self.client.post(path, data=fields, headers=self.headers)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("HX-Trigger", response.headers)
            self.assertEqual(self.db.get_task("preview-task-1"), before)
        response = self.post("/home/reschedule", source="task", uuid="preview-task-1", due_date="")
        self.undo(response)
        self.assertEqual(self.db.get_task("preview-task-1"), before)
        self.post("/tasks/preview-task-3/complete")
        self.assertEqual(self.task()["blockers"], [])
        self.post("/tasks/preview-task-1/complete")
        self.assertTrue(self.task()["today"])
        self.assertTrue(self.task()["done"])

    @isolated_preview
    def test_failed_readbacks_never_report_success(self):
        before = self.state()
        with patch.object(self.db, "update_task", return_value=None):
            response = self.client.post("/home/reschedule", data={"source": "task", "uuid": "preview-task-1", "due_date": ""}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("HX-Trigger", response.headers)
        with patch.object(self.db, "mark_completion", return_value=True):
            response = self.client.post("/discipline/preview-habit/today", data={"action": "mark"}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.state(), before)

    @isolated_preview
    def test_task_actions_leave_other_domains_and_database_guard_untouched(self):
        def snapshots():
            return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in self.directory.iterdir() if path.is_file() and not path.name.startswith("operations.db")}

        before = snapshots()
        self.assertTrue({"cards.db", "characters.db", "review.db"}.issubset(before))
        self.assertFalse({"finance.db", "feedback.db", "maintainer.db"}.intersection(before))
        self.post("/home/today", source="task", uuid="preview-task-3", selected="true", day=self.today)
        response = self.post("/home/reschedule", source="task", uuid="preview-task-1", due_date="")
        self.undo(response)
        response = self.post("/tasks/preview-task-1/complete")
        self.undo(response)
        self.post("/tasks", task="Example isolated task")
        self.post("/discipline/preview-habit/today", action="mark")
        self.post("/discipline/preview-habit/today", action="unmark")
        self.assertEqual(snapshots(), before)
        self.db.get_engine.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "Shared storage disabled"):
            self.db.get_engine()
        self.db.get_engine.reset_mock()

    @isolated_preview
    def test_snapshots_are_copies_and_task_storage_is_bounded(self):
        from fastapi import HTTPException

        before = self.db.get_task("preview-task-1")
        row = self.db.get_task("preview-task-1")
        row["task"] = "Example detached mutation"
        self.db.list_tasks()[0]["priority"] = 10
        self.assertEqual(self.db.get_task("preview-task-1"), before)
        with self.assertRaises(ValueError):
            self.db.restore_task_row("finance", before)
        with self.assertRaises(ValueError):
            self.db.restore_task_row("tasks", {**before, "uuid": "unknown-id"})
        while len(self.db.list_tasks()) < 100:
            self.db.create_task({"task": "Example bounded task"})
        with self.assertRaises(HTTPException) as failure:
            self.db.create_task({"task": "Example over limit"})
        self.assertEqual(failure.exception.status_code, 422)
        self.assertEqual(len(self.db.list_tasks()), 100)


if __name__ == "__main__":
    if WORKER:
        sys.addaudithook(guard_preview_io)
        unittest.main(argv=[sys.argv[0], f"HomePreviewIntegrationTests.{sys.argv[-1]}"], verbosity=2)
    else:
        unittest.main()