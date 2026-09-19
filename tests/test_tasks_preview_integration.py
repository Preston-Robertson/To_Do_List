"""Tasks controllers against bounded synthetic storage, without ASGI startup."""
from __future__ import annotations

import functools
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

from test_home_preview_integration import guard_preview_io
from test_task_editor import FormMarkup


ROOT = Path(__file__).resolve().parents[1]
WORKER = __name__ == "__main__" and "--preview-worker" in sys.argv


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


class TasksPreviewIntegrationTests(unittest.TestCase):
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
        response = self.client.get("/tasks")
        self.assertEqual(response.status_code, 200, response.text)
        self.headers = {"X-CSRF-Token": self.client.cookies["luigi_csrf"]}

    def tearDown(self):
        if WORKER and hasattr(self, "db"):
            self.db.get_engine.assert_not_called()

    def post(self, path, *, expected=200, **data):
        response = self.client.post(path, data=data, headers=self.headers)
        self.assertEqual(response.status_code, expected, response.text)
        return response

    def snapshot(self):
        return self.db.list_tasks(), self.db.list_recurring()

    def undo(self, response):
        op_id = json.loads(response.headers["HX-Trigger"])["showUndo"]["op_id"]
        self.post(f"/undo/{op_id}")

    def assert_rejected(self, path, fields, expected=422):
        before = self.snapshot()
        response = self.client.post(path, data=fields, headers=self.headers)
        self.assertEqual(response.status_code, expected, response.text)
        self.assertNotIn("HX-Trigger", response.headers)
        self.assertEqual(self.snapshot(), before)

    @isolated_preview
    def test_repeat_creation_uses_recurring_storage_after_reload(self):
        before_tasks = self.db.list_tasks()
        before_ids = {row["uuid"] for row in self.db.list_recurring()}
        self.post("/tasks", task="Example weekly review", recurring="1",
                  recurring_schedule_type="interval", recurring_interval="7")
        self.assertEqual(self.db.list_tasks(), before_tasks)
        added = [row for row in self.db.list_recurring() if row["uuid"] not in before_ids]
        self.assertEqual(len(added), 1)
        row = added[0]
        self.assertRegex(row["uuid"], r"^preview-recurring-[a-f0-9]{32}$")
        self.assertEqual(row["recurring_interval"], 7)
        self.assertIsNone(self.db.get_task(row["uuid"]))
        self.assertEqual(self.db.get_recurring(row["uuid"]), row)
        response = self.client.get("/tasks")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Example weekly review", response.text)
        response = self.client.get(f"/recurring/{row['uuid']}/edit")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('value="Example weekly review"', response.text)

    @isolated_preview
    def test_seeds_forms_and_examples_keep_sources_and_today_separate(self):
        tasks, recurring = self.snapshot()
        self.assertEqual([row["uuid"] for row in tasks], [f"preview-task-{index}" for index in range(1, 6)])
        self.assertEqual(len(recurring), 1)
        self.assertEqual(recurring[0]["uuid"], "preview-recurring-1")
        self.assertEqual(recurring[0]["recurring_interval"], 7)
        self.assertTrue(recurring[0]["task"].startswith("Example "))
        self.assertTrue(set(self.db._TASK_COLUMNS).issubset(recurring[0]))
        today_rows = self.client.get("/home/data").json()["tasks"]
        self.assertEqual(len(today_rows), 6)
        self.assertEqual({(row["source"], row["uuid"]) for row in today_rows if row["today"]},
                         {("task", "preview-task-1"), ("task", "preview-task-2")})
        for endpoint, repeat in (("/tasks", False), ("/recurring", True)):
            response = self.client.get(f"{endpoint}/new")
            self.assertEqual(response.status_code, 200, response.text)
            markup = FormMarkup(response.text)
            self.assertEqual(markup.matching("form")[0]["hx-post"], endpoint)
            toggle = markup.matching("input", name="recurring", type="checkbox")[0]
            self.assertEqual("checked" in toggle, repeat)
            self.assertEqual("disabled" in toggle, repeat)
            self.assertEqual(markup.matching("input", name="recurring", type="hidden")[0]["value"], str(int(repeat)))
        for path in ("/tasks/preview", "/recurring/preview-recurring-1/edit", "/calendar", "/projects"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
        markup = self.client.get("/tasks").text
        self.assertNotIn('data-home-today', markup)
        self.assertNotIn('hx-post="/home/today"', markup)

    @isolated_preview
    def test_one_off_and_quick_capture_ignore_stale_repeat_and_private_fields(self):
        recurring = self.db.list_recurring()
        for endpoint, expected in (("/tasks", 200), ("/tasks/quick", 204)):
            before_ids = {row["uuid"] for row in self.db.list_tasks()}
            self.post(endpoint, expected=expected, task="Example one-off capture", recurring="1" if endpoint == "/tasks/quick" else "0",
                      recurring_schedule_type="invalid", recurring_interval="invalid",
                      recurring_days=["0", "invalid"], recurring_month_ordinal="invalid",
                      recurring_month_weekday="invalid", uuid="untrusted-id", completed="1", archived="1")
            added = [row for row in self.db.list_tasks() if row["uuid"] not in before_ids]
            self.assertEqual(len(added), 1)
            row = added[0]
            self.assertRegex(row["uuid"], r"^preview-task-[a-f0-9]{32}$")
            self.assertEqual((row["source"], row["recurring"], row["completed"], row["archived"]), ("task", 0, 0, 0))
            self.assertTrue(all(row[field] is None for field in (
                "recurring_interval", "recurring_days", "recurring_month_ordinal", "recurring_month_weekday")))
            self.assertIsNone(self.db.get_recurring(row["uuid"]))
            self.assertEqual(self.db.list_recurring(), recurring)
            response = self.client.get(f"/tasks/{row['uuid']}/edit")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(FormMarkup(response.text).matching("input", name="recurring"))
            self.assertIn(row["uuid"], self.client.get("/tasks").text)
        self.assert_rejected("/tasks/quick", {"task": " "})
        self.assert_rejected("/tasks/preview-task-1", {"recurring": "1", "recurring_interval": "7"})

    @isolated_preview
    def test_weekday_multikey_create_and_edit_preserve_every_selected_day(self):
        fields = [("task", "Example weekdays"), ("recurring", "0"), ("recurring", "1"),
                  ("recurring_schedule_type", "weekdays"), ("recurring_days", "4"),
                  ("recurring_days", "0"), ("recurring_days", "4"), ("recurring_interval", "invalid")]
        response = self.client.post("/tasks", content=urlencode(fields),
            headers={**self.headers, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.db.create_recurring.call_args.args[0]["recurring_days"], ["4", "0", "4"])
        row = next(row for row in self.db.list_recurring() if row["task"] == "Example weekdays")
        self.assertEqual(row["recurring_days"], "0,4")
        self.assertIsNone(row["recurring_interval"])
        endpoint = f"/recurring/{row['uuid']}"
        self.post(endpoint, task="Example edited weekdays", recurring="1", recurring_schedule_type="weekdays",
                  recurring_days=["6", "2", "0"], recurring_month_ordinal="invalid")
        self.assertEqual(self.db.update_recurring.call_args.args[1]["recurring_days"], ["6", "2", "0"])
        self.assertEqual(self.db.get_recurring(row["uuid"])["recurring_days"], "0,2,6")
        response = self.client.get(f"{endpoint}/edit")
        self.assertEqual(response.status_code, 200, response.text)
        days = FormMarkup(response.text).matching("input", name="recurring_days")
        self.assertEqual([day["value"] for day in days if "checked" in day], ["0", "2", "6"])
        self.assert_rejected(endpoint, {"recurring": "1", "recurring_schedule_type": "interval", "recurring_interval": ""})

    @isolated_preview
    def test_legacy_recurring_creation_and_monthly_edit_normalize_modes(self):
        before_tasks = self.db.list_tasks()
        self.post("/recurring", task="Example monthly review", recurring="1", recurring_schedule_type="monthly",
                  recurring_month_ordinal="-1", recurring_month_weekday="4", recurring_interval="invalid", recurring_days=["1", "3"])
        row = next(row for row in self.db.list_recurring() if row["task"] == "Example monthly review")
        self.assertEqual((row["recurring_month_ordinal"], row["recurring_month_weekday"]), (-1, 4))
        self.assertIsNone(row["recurring_interval"])
        self.assertIsNone(row["recurring_days"])
        endpoint = f"/recurring/{row['uuid']}"
        self.post(endpoint, task="Example interval review", recurring="1", recurring_schedule_type="interval", recurring_interval="14")
        changed = self.db.get_recurring(row["uuid"])
        self.assertEqual(changed["recurring_interval"], 14)
        self.assertIsNone(changed["recurring_month_ordinal"])
        self.assertIsNone(changed["recurring_month_weekday"])
        self.post(endpoint, recurring="0", recurring_schedule_type="monthly",
                  recurring_month_ordinal="invalid", recurring_month_weekday="invalid", recurring_interval="invalid")
        changed = self.db.get_recurring(row["uuid"])
        self.assertEqual((changed["source"], changed["recurring"]), ("recurring", 0))
        self.assertTrue(all(changed[field] is None for field in (
            "recurring_interval", "recurring_days", "recurring_month_ordinal", "recurring_month_weekday")))
        self.post(endpoint, recurring="1", recurring_schedule_type="weekdays", recurring_days=["1", "5"])
        self.assertEqual(self.db.get_recurring(row["uuid"])["recurring_days"], "1,5")
        self.assertEqual(self.db.list_tasks(), before_tasks)
        self.assertIsNone(self.db.get_task(row["uuid"]))

    @isolated_preview
    def test_invalid_schedules_and_fields_return_422_without_writing(self):
        invalid = [{"recurring_schedule_type": "unexpected"}]
        invalid += [{"recurring_schedule_type": "interval", "recurring_interval": value}
                    for value in ("", "0", "-1", "1.5", "invalid")]
        invalid += [{"recurring_schedule_type": "weekdays", "recurring_days": value}
                    for value in ([], ["invalid", "8", "-1"])]
        invalid += [{"recurring_schedule_type": "monthly", "recurring_month_ordinal": ordinal,
                     "recurring_month_weekday": weekday} for ordinal, weekday in (("0", "1"), ("5", "1"), ("1", "7"), ("", ""))]
        for endpoint in ("/tasks", "/recurring", "/recurring/preview-recurring-1"):
            for schedule in invalid:
                with self.subTest(endpoint=endpoint, schedule=schedule):
                    self.assert_rejected(endpoint, {"task": "Example rejected schedule", "recurring": "1", **schedule})
            for field, value in (("task", " "), ("priority", "11"), ("priority", "-1"),
                                 ("estimated_time", "nan"), ("estimated_time", "-1"),
                                 ("due_date", "2026-02-30"), ("status", "Done")):
                self.assert_rejected(endpoint, {"task": "Example rejected field", "recurring": "1",
                    "recurring_schedule_type": "interval", "recurring_interval": "7", field: value})
        self.assert_rejected("/tasks", {"task": "Example invalid Repeat", "recurring": "invalid"})

    @isolated_preview
    def test_status_completion_reopen_and_undo_preserve_schedules(self):
        for source, row_uuid in (("task", "preview-task-2"), ("recurring", "preview-recurring-1")):
            getter = self.db.get_task if source == "task" else self.db.get_recurring
            endpoint = f"/{'tasks' if source == 'task' else 'recurring'}/{row_uuid}"
            initial = getter(row_uuid)
            schedule = {field: value for field, value in initial.items() if field.startswith("recurring")}
            for status in self.db.STATUS_VALUES:
                self.post(f"{endpoint}/status", expected=204, status=status)
                row = getter(row_uuid)
                self.assertEqual(row["status"], status)
                self.assertEqual(row["completed"], int(status == "Completed"))
                self.assertEqual(bool(row["completed_time"]), status == "Completed")
                self.assertEqual({field: row[field] for field in schedule}, schedule)
            self.assert_rejected(f"{endpoint}/status", {"status": "Done"}, expected=400)
            self.post(f"{endpoint}/status", expected=204, status="Not Started")
            before = getter(row_uuid)
            response = self.post(f"{endpoint}/complete")
            completed = getter(row_uuid)
            self.assertEqual(completed["completed"], 1)
            self.assertEqual(completed["status"], "Completed")
            self.assertEqual({field: completed[field] for field in schedule}, schedule)
            reopened_response = self.post(f"{endpoint}/complete")
            reopened = getter(row_uuid)
            self.assertEqual((reopened["completed"], reopened["status"], reopened["completed_time"]), (0, "Not Started", None))
            self.undo(reopened_response)
            self.assertEqual(getter(row_uuid), completed)
            self.undo(response)
            self.assertEqual(getter(row_uuid), before)
            self.assertEqual(self.client.get("/tasks").status_code, 200)

    @isolated_preview
    def test_cross_source_dependencies_block_transitions_atomically(self):
        for dependent_source, dependent_uuid, blocker_source, blocker_uuid in (
            ("recurring", "preview-recurring-1", "task", "preview-task-3"),
            ("task", "preview-task-2", "recurring", "preview-recurring-1"),
        ):
            self.host.operations.add_dependency(
                dependent_uuid=dependent_uuid, dependent_source=dependent_source, dependent_label="Example dependent",
                blocker_uuid=blocker_uuid, blocker_source=blocker_source, blocker_label="Example blocker")
            endpoint = f"/{'tasks' if dependent_source == 'task' else 'recurring'}/{dependent_uuid}"
            for status in ("In Progress", "Completed"):
                self.assert_rejected(f"{endpoint}/status", {"status": status})
                self.assert_rejected(endpoint, {"task": "Example rejected edit", "status": status})
            self.assert_rejected(f"{endpoint}/complete", {})
        self.post("/tasks/preview-task-3/complete")
        response = self.post("/recurring/preview-recurring-1/complete")
        self.post("/tasks/preview-task-2/status", expected=204, status="In Progress")
        self.undo(response)
        self.assert_rejected("/tasks/preview-task-2/complete", {})

    @isolated_preview
    def test_new_actions_require_csrf_scoped_session_and_same_origin(self):
        before = self.snapshot()
        paths = ("/tasks", "/tasks/quick", "/recurring", "/recurring/preview-recurring-1",
                 "/recurring/preview-recurring-1/status", "/recurring/preview-recurring-1/complete",
                 "/tasks/preview-task-1/status")
        for path in paths:
            for headers in ({}, {"X-CSRF-Token": "incorrect"},
                            {**self.headers, "Origin": "http://localhost:58306"}):
                response = self.client.post(path, data={"task": "Example rejected action", "status": "Completed"}, headers=headers)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertNotIn("HX-Trigger", response.headers)
        self.assertEqual(self.client.get("/tasks", headers={"Host": "example.invalid"}).status_code, 403)
        self.client.cookies.clear()
        self.client.cookies.set("luigi_preview_session_58307", "synthetic-wrong-port")
        self.client.cookies.set("luigi_session", os.environ["LUIGI_WEB_UI_TOKEN"])
        for path in paths:
            response = self.client.post(path, headers={**self.headers, "Authorization": "Bearer " + os.environ["LUIGI_WEB_UI_TOKEN"]})
            self.assertEqual(response.status_code, 401, response.text)
            self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.snapshot(), before)

    @isolated_preview
    def test_existing_home_recurring_reschedule_and_undo_preserve_schedule(self):
        row_uuid = "preview-recurring-1"
        before = self.db.get_recurring(row_uuid)
        home = self.client.get("/home/data").json()
        self.post("/home/today", source="recurring", uuid=row_uuid, day=home["today"], selected="true")
        response = self.post("/home/reschedule", source="recurring", uuid=row_uuid, due_date="2030-10-14")
        self.assertEqual(self.db.get_recurring(row_uuid), {**before, "due_date": "2030-10-14"})
        self.undo(response)
        self.assertEqual(self.db.get_recurring(row_uuid), before)
        home = self.client.get("/home/data").json()
        self.assertTrue(next(row for row in home["tasks"] if row["uuid"] == row_uuid)["today"])

    @isolated_preview
    def test_task_actions_remain_opt_in_and_remote_clients_are_rejected(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from scripts.preview_workspace import configure_preview_security

        app = FastAPI()
        configure_preview_security(app, "synthetic-opt-in-session")
        with TestClient(app, base_url="http://127.0.0.1:58308", follow_redirects=False) as client:
            self.assertEqual(client.get("/").status_code, 303)
            headers = {"X-CSRF-Token": client.cookies["luigi_csrf"]}
            for path in ("/tasks", "/tasks/quick", "/tasks/preview-task-1/status", "/recurring",
                         "/recurring/preview-recurring-1", "/recurring/preview-recurring-1/status",
                         "/recurring/preview-recurring-1/complete"):
                self.assertEqual(client.post(path, headers=headers).status_code, 403)
        async def remote_client(scope, receive, send):
            await app({**scope, "client": ("192.0.2.1", 50000)}, receive, send)

        with TestClient(remote_client, base_url="http://127.0.0.1:58308") as client:
            self.assertEqual(client.get("/").status_code, 403)

    @isolated_preview
    def test_forbidden_endpoints_and_source_mismatches_never_touch_storage(self):
        blocked = ("/tasks/bulk", "/tasks/quick/extra", "/recurring/extra", "/recurring/bulk", "/home/today/extra",
                   "/admin/update", "/gnw", "/feedback", "/chat", "/chat/send", "/cards/mtg/refresh",
                   "/characters/refresh", "/preview/deploy", "/finance/import",
                   "/tasks/preview-recurring-1/status", "/recurring/preview-task-1/complete")
        for path in blocked:
            self.assert_rejected(path, {}, expected=403)
        for endpoint in ("/tasks/preview-task-1", "/recurring/preview-recurring-1"):
            for suffix in ("delete", "archive", "restore", "snooze", "status/extra", "complete/extra"):
                self.assert_rejected(f"{endpoint}/{suffix}", {}, expected=403)
            for method in ("PUT", "PATCH", "DELETE"):
                self.assertEqual(self.client.request(method, endpoint, headers=self.headers).status_code, 403)
        for endpoint in ("/tasks/preview-task-missing", "/recurring/preview-recurring-missing"):
            for suffix in ("", "/status", "/complete"):
                self.assert_rejected(endpoint + suffix, {"status": "Completed", "task": "Example missing"}, expected=404)
            self.assertEqual(self.client.get(endpoint + "/edit").status_code, 404)
        self.assertIsNone(self.db.get_task("preview-recurring-1"))
        self.assertIsNone(self.db.get_recurring("preview-task-1"))
        for source, row_uuid in (("task", "preview-recurring-1"), ("recurring", "preview-task-1"), ("recurring", "unknown-id")):
            self.assert_rejected("/home/reschedule", {"source": source, "uuid": row_uuid, "due_date": ""}, expected=404)

    @isolated_preview
    def test_creation_readback_failure_has_no_success_signal(self):
        with patch.object(self.db, "create_recurring", return_value="preview-recurring-unverified"), \
                patch.object(self.db, "get_recurring", return_value=None):
            self.assert_rejected("/tasks", {"task": "Example unverified", "recurring": "1", "recurring_interval": "7"}, expected=503)

    @isolated_preview
    def test_recurring_snapshots_are_detached_bounded_and_source_qualified(self):
        from fastapi import HTTPException

        before = self.db.get_recurring("preview-recurring-1")
        self.db.get_recurring(before["uuid"])["task"] = "Example detached edit"
        self.db.list_recurring()[0]["recurring_interval"] = 99
        self.assertEqual(self.db.get_recurring(before["uuid"]), before)
        for table, snapshot, generated in (("tasks", before, ()), ("finance", before, ()),
            ("recurring_tasks", {**before, "uuid": "unknown-id"}, ()),
            ("recurring_tasks", {**before, "source": "task"}, ()),
            ("recurring_tasks", before, ("preview-task-1",))):
            with self.assertRaises(ValueError):
                self.db.restore_task_row(table, snapshot, generated_task_uuids=generated)
        while len(self.db.list_recurring()) < 100:
            self.db.create_recurring({"task": "Example bounded recurring", "recurring_interval": "7"})
        with self.assertRaises(HTTPException) as failure:
            self.db.create_recurring({"task": "Example over limit", "recurring_interval": "7"})
        self.assertEqual(failure.exception.status_code, 422)
        self.assertEqual(len(self.db.list_recurring()), 100)
        self.assertEqual(len(self.db.list_tasks()), 5)

    @isolated_preview
    def test_recurring_actions_leave_other_synthetic_domains_unchanged(self):
        def file_snapshots():
            return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in self.directory.iterdir() if path.is_file() and not path.name.startswith("operations.db")}

        before = file_snapshots()
        self.assertTrue({"cards.db", "characters.db", "review.db"}.issubset(before))
        self.assertFalse({"finance.db", "feedback.db", "maintainer.db"}.intersection(before))
        self.post("/tasks", task="Example isolated recurring", recurring="1", recurring_interval="7")
        self.post("/recurring/preview-recurring-1", task="Example isolated edit", recurring="1", recurring_interval="14")
        response = self.post("/recurring/preview-recurring-1/complete")
        self.undo(response)
        self.post("/recurring/preview-recurring-1/status", expected=204, status="In Progress")
        self.assertEqual(file_snapshots(), before)


if __name__ == "__main__":
    if WORKER:
        sys.addaudithook(guard_preview_io)
        unittest.main(argv=[sys.argv[0], f"TasksPreviewIntegrationTests.{sys.argv[-1]}"], verbosity=2)
    else:
        unittest.main()