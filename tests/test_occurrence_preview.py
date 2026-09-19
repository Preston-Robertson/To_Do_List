"""Disposable occurrence previews, isolated from shared storage and services."""
from __future__ import annotations

import functools
import hashlib
from html.parser import HTMLParser
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import timedelta
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORKER = __name__ == "__main__" and "--occurrence-worker" in sys.argv


class Markup(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.elements = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def isolated_preview(test):
    @functools.wraps(test)
    def run(self):
        if WORKER:
            return test(self)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("LUIGI_WEB_")}
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--occurrence-worker", self.id().split(".")[-1]],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    return run


class OccurrencePreviewTests(unittest.TestCase):
    def preview(self, *, occurrence_demo=True):
        from fastapi.testclient import TestClient
        from luigi_web.modules.tasks import occurrences
        from scripts.preview_workspace import preview_context

        for name in ("ensure_storage", "lineage", "transaction"):
            guard = self.enterContext(patch.object(occurrences, name, side_effect=AssertionError("Occurrence SQL forbidden")))
            self.addCleanup(guard.assert_not_called)
        self.addCleanup(lambda: self.assertFalse(self.directory.exists(), "Temporary preview was not removed"))
        app = self.enterContext(preview_context(occurrence_demo=occurrence_demo))
        from luigi_web import application

        self.host = application
        self.db = application.db
        self.store = self.db.get_recurring.side_effect.__self__
        guard = self.enterContext(patch.object(self.db, "reactivate_due_recurring", side_effect=AssertionError("Shared generation forbidden")))
        self.addCleanup(guard.assert_not_called)
        self.directory = Path(os.environ["LUIGI_WEB_DATA_DIR"])
        self.client = TestClient(app, base_url="http://127.0.0.1:58309", follow_redirects=False)
        self.addCleanup(self.client.close)
        response = self.client.get("/home/data")
        self.assertEqual(response.status_code, 200, response.text)
        self.headers = {"X-CSRF-Token": self.client.cookies["luigi_csrf"]}
        return response.json()

    def tearDown(self):
        if WORKER and hasattr(self, "db"):
            self.db.get_engine.assert_not_called()

    def past_source(self, **changes):
        today = self.host.clock.local_today()
        row_uuid = self.db.create_recurring({
            "task": "Example recurring occurrence", "recurring_interval": "1", "status": "Completed",
            "priority": "4", "catagory": "Example category", "project": "Example project",
            "task_group": "Example group", "sub_group": "Example subgroup", "estimated_time": "2.5",
            "relevant_link": "https://example.invalid/", "description": "Example execution note",
        })
        row = self.db.get_recurring(row_uuid)
        row.update(completed_time=f"{today - timedelta(days=3)}T12:00:00",
                   due_date=(today - timedelta(days=4)).isoformat(),
                   start_time=f"{today - timedelta(days=4)}T09:00:00", logged_hours=2)
        row.update(changes)
        self.db.restore_task_row("recurring_tasks", row)
        return row

    def post(self, path, **data):
        return self.client.post(path, data=data, headers=self.headers)

    @isolated_preview
    def test_default_preview_remains_six_tasks_and_two_today(self):
        state = self.preview(occurrence_demo=False)
        self.assertEqual(len(state["tasks"]), 6)
        self.assertEqual({row["uuid"] for row in state["tasks"] if row["today"]},
                         {"preview-task-1", "preview-task-2"})
        self.assertEqual([row["uuid"] for row in self.db.list_recurring()], ["preview-recurring-1"])
        self.assertEqual(self.host._reactivate_recurring(), 0)
        self.assertEqual(self.store.occurrence_links, {})

    @isolated_preview
    def test_demo_generates_one_distinct_open_child_without_resetting_source(self):
        state = self.preview()
        self.assertEqual(len(state["tasks"]), 8)
        self.assertEqual(sum(row["today"] for row in state["tasks"]), 2)
        source = self.db.get_recurring("preview-recurring-past")
        child = self.db.get_recurring(source["_recurrence_next_uuid"])
        self.assertEqual(source["status"], "Completed")
        self.assertEqual(source["completed"], 1)
        self.assertTrue(source["completed_time"].startswith(source["due_date"]))
        self.assertTrue(source["_recurrence_generated"])
        self.assertNotEqual(child["uuid"], source["uuid"])
        self.assertTrue(child["uuid"].startswith("preview-recurring-"))
        self.assertEqual(child["_recurrence_parent_uuid"], source["uuid"])
        self.assertEqual(child["_recurrence_series_uuid"], source["uuid"])
        self.assertFalse(child["_recurrence_generated"])
        self.assertEqual(child["status"], "Not Started")
        self.assertEqual(child["completed"], 0)
        self.assertIsNone(child["completed_time"])
        self.assertIsNone(child["start_time"])
        self.assertEqual(child["logged_hours"], 0)
        self.assertEqual(child["due_date"], state["today"])
        self.assertEqual(child["recurring_interval"], 1)
        before = self.db.list_recurring()
        self.assertEqual(self.host._reactivate_recurring(), 0)
        self.assertEqual(self.db.list_recurring(), before)
        self.assertTrue(all(set(self.db._TASK_COLUMNS).issubset(row) for row in before))
        self.assertTrue(all(not field.startswith("_recurrence_")
                            for row in self.store.recurring.values() for field in row))

    @isolated_preview
    def test_real_payload_and_schedule_are_used_once_under_concurrent_retries(self):
        from luigi_web.modules.tasks import occurrences

        self.preview(occurrence_demo=False)
        source = self.past_source()
        today = self.host.clock.local_today()
        due = self.db.reactivation_date(source)
        created_at = f"{today}T14:00:00"
        expected = dict.fromkeys(self.db._TASK_COLUMNS)
        expected.update(occurrences.occurrence_payload(source, due, created_at), source="recurring")
        expected["uuid"] = f"preview-recurring-{expected['uuid']}"
        with patch.object(occurrences, "occurrence_payload", wraps=occurrences.occurrence_payload) as payload, \
             patch.object(self.db, "now_iso", return_value=created_at), \
             ThreadPoolExecutor(max_workers=8) as executor:
            counts = list(executor.map(self.store.generate_due, [today] * 8))
        self.assertEqual(sorted(counts), [0] * 7 + [1])
        payload.assert_called_once_with(source, due, created_at)
        self.assertEqual(self.store.recurring[source["uuid"]], source)
        self.assertEqual(self.store.recurring[expected["uuid"]], expected)
        self.assertEqual(len(self.store.occurrence_links), 1)
        child = self.db.get_recurring(expected["uuid"])
        child.update(status="Completed", completed=1, completed_time=source["completed_time"])
        self.db.restore_task_row("recurring_tasks", child)
        self.assertEqual(self.store.generate_due(today), 1)
        child = self.db.get_recurring(child["uuid"])
        successor = self.db.get_recurring(child["_recurrence_next_uuid"])
        self.assertTrue(child["_recurrence_generated"])
        self.assertEqual(child["_recurrence_parent_uuid"], source["uuid"])
        self.assertEqual(successor["_recurrence_parent_uuid"], child["uuid"])
        self.assertEqual(successor["_recurrence_series_uuid"], source["uuid"])
        self.assertEqual(self.store.generate_due(today), 0)
        self.assertEqual(self.store.recurring[source["uuid"]], source)

    @isolated_preview
    def test_ineligible_rows_owner_gate_and_failed_payload_do_not_create_links(self):
        from luigi_web.modules.tasks import occurrences

        self.preview(occurrence_demo=False)
        for changes in (
            {"completed": 0, "status": "Not Started", "completed_time": None},
            {"archived": 1}, {"recurring": 0}, {"recurring_interval": None},
            {"recurring_interval": 0}, {"recurring_interval": -1}, {"recurring_interval": "invalid"},
            {"completed_time": "invalid"}, {"completed_time": None},
            {"completed_time": f"{self.host.clock.local_today()}T12:00:00"},
        ):
            self.past_source(**changes)
        before = self.db.list_recurring()
        self.assertEqual(self.store.generate_due(), 0)
        self.assertEqual(self.db.list_recurring(), before)
        source = self.past_source()
        before = self.db.list_recurring()
        with patch.dict(os.environ, {"LUIGI_WEB_RECURRENCE_OWNER": "external"}):
            self.assertEqual(self.store.generate_due(), 0)
        with patch.object(occurrences, "occurrence_payload", side_effect=ValueError("Example invalid schedule")):
            with self.assertRaisesRegex(ValueError, "Example invalid schedule"):
                self.store.generate_due()
        self.assertEqual(self.db.list_recurring(), before)
        self.assertEqual(self.store.occurrence_links, {})
        self.assertEqual(self.store.generate_due(), 1)
        self.assertEqual(self.store.recurring[source["uuid"]], source)
        self.assertEqual(self.store.generate_due(), 0)

    @isolated_preview
    def test_home_and_templates_show_locked_history_and_link_to_editable_child(self):
        state = self.preview()
        source = self.db.get_recurring("preview-recurring-past")
        child_uuid = source["_recurrence_next_uuid"]
        home_source = next(row for row in state["tasks"] if row["uuid"] == source["uuid"])
        home_child = next(row for row in state["tasks"] if row["uuid"] == child_uuid)
        self.assertTrue(home_source["history_locked"])
        self.assertTrue(home_source["done"])
        self.assertEqual(home_source["completed_at"], source["completed_time"])
        self.assertNotIn("history_locked", home_child)
        self.assertFalse(home_child["done"])
        self.assertFalse(home_child["today"])
        self.assertNotIn("_recurrence_next_uuid", home_source)
        history = self.client.get(f"/recurring/{source['uuid']}/edit")
        self.assertEqual(history.status_code, 200)
        self.assertIn("Next instance created", history.text)
        self.assertIn(source["completed_time"], history.text)
        self.assertIn(f'href="/recurring/{child_uuid}/edit"', history.text)
        elements = Markup(history.text).elements
        self.assertFalse(any(tag in {"form", "input", "select", "textarea"} for tag, attrs in elements))
        self.assertFalse(any("hx-post" in attrs for tag, attrs in elements))
        child_editor = self.client.get(f"/recurring/{child_uuid}/edit")
        self.assertEqual(child_editor.status_code, 200)
        self.assertIn(f'hx-post="/recurring/{child_uuid}"', child_editor.text)
        self.assertIn("data-task-editor", child_editor.text)
        for template in ("task_list", "task_card"):
            markup = self.host.templates.env.get_template(f"partials/{template}.html").render(
                t=source, rows=[source], endpoint_root="/recurring",
            )
            self.assertIn("Next instance created", markup)
            self.assertIn(f"Completed {source['completed_time'][:10]}", markup)
            elements = Markup(markup).elements
            self.assertTrue(any(tag == "button" and "disabled" in attrs for tag, attrs in elements))
            self.assertFalse(any(attrs.get("hx-post") == f"/recurring/{source['uuid']}/complete" for tag, attrs in elements))
        for path in ("/home", "/tasks", "/home/preview", "/tasks/preview"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")

    @isolated_preview
    def test_history_mutations_and_stale_undo_are_rejected_without_changing_rows(self):
        from luigi_web.modules.tasks import occurrences

        self.preview()
        source = self.db.get_recurring("preview-recurring-past")
        before = self.db.list_recurring()
        for path, data in (
            (f"/recurring/{source['uuid']}/complete", {}),
            (f"/recurring/{source['uuid']}/status", {"status": "Not Started"}),
            (f"/recurring/{source['uuid']}", {"task": "Example changed history"}),
            ("/home/reschedule", {"source": "recurring", "uuid": source["uuid"], "due_date": ""}),
        ):
            response = self.post(path, **data)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertNotIn("HX-Trigger", response.headers)
        response = self.post(f"/recurring/{source['uuid']}/status", status="Completed")
        self.assertEqual(response.status_code, 204)
        for status in ("Not Started", "In Progress"):
            with self.assertRaisesRegex(ValueError, occurrences.HISTORY_MESSAGE):
                self.db.set_recurring_status(source["uuid"], status)
        with self.assertRaisesRegex(ValueError, occurrences.HISTORY_MESSAGE):
            self.db.update_recurring(source["uuid"], {"task": "Example rejected edit"})
        snapshot = {**source, "completed": 0, "status": "Not Started", "completed_time": None}
        with self.assertRaisesRegex(ValueError, occurrences.HISTORY_MESSAGE):
            self.db.restore_task_row("recurring_tasks", snapshot)
        op_id = self.host._stash_undo("recurring_tasks", snapshot, "Example stale completion")
        entry = self.host._UNDO_QUEUE[op_id]
        response = self.post(f"/undo/{op_id}")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertNotIn("HX-Trigger", response.headers)
        self.assertIs(self.host._UNDO_QUEUE[op_id], entry)
        self.assertEqual(self.db.list_recurring(), before)

    @isolated_preview
    def test_child_actions_remain_whitelisted_and_other_domains_unchanged(self):
        self.preview()

        def snapshots():
            return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in self.directory.iterdir() if path.is_file() and not path.name.startswith("operations.db")}

        files_before = snapshots()
        self.assertTrue({"cards.db", "characters.db", "review.db"}.issubset(files_before))
        self.assertFalse({"finance.db", "feedback.db", "maintainer.db"}.intersection(files_before))
        source = self.db.get_recurring("preview-recurring-past")
        child_uuid = source["_recurrence_next_uuid"]
        self.assertEqual(self.post(f"/recurring/{child_uuid}", task="Example edited occurrence").status_code, 200)
        before = self.db.get_recurring(child_uuid)
        response = self.post(f"/recurring/{child_uuid}/complete")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.db.get_recurring(child_uuid)["completed"])
        self.assertEqual(self.host._reactivate_recurring(), 0)
        op_id = json.loads(response.headers["HX-Trigger"])["showUndo"]["op_id"]
        self.assertEqual(self.post(f"/undo/{op_id}").status_code, 200)
        self.assertEqual(self.db.get_recurring(child_uuid), before)
        self.assertEqual(self.db.get_recurring(source["uuid"]), source)
        self.assertEqual(self.post("/recurring/preview-recurring-missing/complete").status_code, 404)
        for action in ("delete", "archive", "snooze"):
            self.assertEqual(self.post(f"/recurring/{child_uuid}/{action}").status_code, 403)
        self.assertEqual(self.client.post(f"/recurring/{child_uuid}/complete").status_code, 403)
        self.assertEqual(self.client.get("/__preview/occurrences").status_code, 404)
        self.assertEqual(snapshots(), files_before)
        self.assertTrue(all(not field.startswith("_recurrence_")
                            for row in self.store.recurring.values() for field in row))

    @isolated_preview
    def test_generation_respects_preview_capacity_and_detached_readouts(self):
        from fastapi import HTTPException

        self.preview(occurrence_demo=False)
        source = self.past_source()
        while len(self.db.list_recurring()) < 99:
            self.db.create_recurring({"task": "Example bounded recurrence", "recurring_interval": "7"})
        self.assertEqual(self.store.generate_due(), 1)
        before = self.db.list_recurring()
        row = self.db.get_recurring(source["uuid"])
        row["_recurrence_next_uuid"] = "preview-recurring-missing"
        detached = self.db.list_recurring()
        detached[-1]["task"] = "Example detached change"
        self.assertEqual(self.db.list_recurring(), before)
        self.assertEqual(self.store.generate_due(), 0)
        self.assertEqual(len(self.db.list_recurring()), 100)
        with self.assertRaises(HTTPException) as failure:
            self.db.create_recurring({"task": "Example overflow", "recurring_interval": "1"})
        self.assertEqual(failure.exception.status_code, 422)

    @isolated_preview
    def test_demo_cli_check_never_starts_a_server(self):
        from luigi_web.modules.tasks import occurrences
        from scripts import preview_workspace

        for name in ("ensure_storage", "lineage", "transaction"):
            guard = self.enterContext(patch.object(occurrences, name, side_effect=AssertionError("Occurrence SQL forbidden")))
            self.addCleanup(guard.assert_not_called)
        output = StringIO()
        with patch.object(sys, "argv", ["preview_workspace.py", "--check", "--occurrence-demo"]), \
             patch.object(preview_workspace, "preview_context", wraps=preview_workspace.preview_context) as context, \
             patch("uvicorn.Server", side_effect=AssertionError("Preview server startup forbidden")) as server, \
             redirect_stdout(output):
            preview_workspace.main()
        context.assert_called_once_with(occurrence_demo=True)
        server.assert_not_called()
        self.assertIn("Validated 16 synthetic workspace endpoints without external services.", output.getvalue())


if __name__ == "__main__":
    if WORKER:
        sys.path.insert(0, str(ROOT))
        from test_home_preview_integration import guard_preview_io

        sys.addaudithook(guard_preview_io)
        unittest.main(argv=[sys.argv[0], f"OccurrencePreviewTests.{sys.argv[-1]}"], verbosity=2)
    else:
        unittest.main()