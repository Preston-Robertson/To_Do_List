"""Synthetic, offline contracts for history-preserving recurrence UI."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from copy import deepcopy
from datetime import date
import json
import sys
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

import luigi_web
from luigi_web.auth import require_auth
from luigi_web.modules.planning import home_routes, home_service
from luigi_web.modules.tasks import occurrences, routes as task_routes


TASKS_ROOT = Path(__file__).resolve().parents[1] / "luigi_web" / "modules" / "tasks"
HISTORY_MESSAGE = "This occurrence has a successor and its history cannot be changed."


class Markup(HTMLParser):
    def __init__(self, markup: str) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []
        self.feed(markup)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def occurrence_row(**changes: object) -> dict:
    return {
        "uuid": "example-parent", "task": "Example recurring task", "recurring": 1,
        "completed": 1, "status": "Done", "priority": 0,
        "completed_time": "2030-10-14T12:30:00", "due_date": "2030-10-14",
        "recurring_interval": 7, "_recurrence_generated": True,
        "_recurrence_next_uuid": "example-child", "_endpoint_root": "/recurring",
        **changes,
    }


def template_environment() -> Environment:
    environment = Environment(
        loader=FileSystemLoader(TASKS_ROOT / "templates"),
        autoescape=select_autoescape(("html",)),
    )
    environment.globals.update(
        reactivation_date=lambda task: None if task.get("_recurrence_generated") else "2030-10-21",
        recurrence_schedule_label=lambda task: "Every 7 days",
        recurrence_schedule_type=lambda task: "interval",
        recurring_days_list=lambda value: [],
        completion_day_policy=lambda: "Today",
        WEEKDAY_LABELS=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        MONTH_ORDINAL_OPTIONS=((1, "First"), (-1, "Last")),
    )
    return environment


def render_partial(name: str, row: dict, endpoint: str = "/recurring") -> str:
    environment = template_environment()
    return environment.get_template(f"partials/{name}.html").render(
        t=row, rows=[row], endpoint_root=endpoint, is_new=False,
        statuses=("Not Started", "In Progress", "Done"),
    )


class OccurrenceTemplateTests(unittest.TestCase):
    def test_list_history_metadata_can_wrap(self) -> None:
        parsed = Markup(render_partial("task_list", occurrence_row()))
        metadata = next(attrs for _, attrs in parsed.elements if attrs.get("class") == "task-list-submeta")
        self.assertEqual(metadata.get("style"), "flex-wrap: wrap")

    def test_generated_card_blocks_board_drag_without_blocking_details_click(self) -> None:
        for locked in (False, True):
            with self.subTest(locked=locked):
                parsed = Markup(render_partial("task_card", occurrence_row(_recurrence_generated=locked)))
                article = next(attrs for tag, attrs in parsed.elements if tag == "article")
                if locked:
                    self.assertEqual(article["draggable"], "false")
                    for event in ("onpointerdown", "onmousedown", "ontouchstart"):
                        self.assertEqual(article[event], "event.stopPropagation()")
                    self.assertNotIn("onclick", article)
                else:
                    self.assertNotIn("draggable", article)
                    self.assertNotIn("onpointerdown", article)

    def test_locked_rows_offer_only_details_archive_and_delete(self) -> None:
        for template in ("task_card", "task_list"):
            with self.subTest(template=template):
                markup = render_partial(template, occurrence_row())
                parsed = Markup(markup)
                self.assertIn("Next instance created", markup)
                self.assertIn("Completed 2030-10-14", markup)
                self.assertIn('hx-get="/recurring/example-parent/edit"', markup)
                mutations = [attrs["hx-post"] for _, attrs in parsed.elements if "hx-post" in attrs]
                self.assertEqual(mutations, ["/recurring/example-parent/archive", "/recurring/example-parent/delete"])
                self.assertTrue(all(attrs.get("hx-confirm") for _, attrs in parsed.elements if "hx-post" in attrs))
                self.assertFalse(any("data-task-select" in attrs for _, attrs in parsed.elements))
                self.assertTrue(any(tag == "button" and "disabled" in attrs and attrs.get("title") == HISTORY_MESSAGE for tag, attrs in parsed.elements))

    def test_pending_next_instance_label_and_plain_row_controls(self) -> None:
        row = occurrence_row()
        row.pop("_recurrence_generated")
        for template in ("task_card", "task_list"):
            with self.subTest(template=template):
                markup = render_partial(template, row)
                self.assertIn("New instance due 2030-10-21", markup)
                self.assertNotIn("Reactivates", markup)
                self.assertNotIn("Next instance created", markup)
                self.assertIn('hx-post="/recurring/example-parent/complete"', markup)
                self.assertIn("Completed 2030-10-14", markup)

    def test_history_detail_is_text_only_with_completion_and_successor(self) -> None:
        markup = render_partial("task_form", occurrence_row())
        parsed = Markup(markup)
        self.assertIn(HISTORY_MESSAGE, markup)
        self.assertIn("Next instance created", markup)
        self.assertIn("2030-10-14T12:30:00", markup)
        self.assertIn('href="/recurring/example-child/edit"', markup)
        self.assertNotIn("example-parent", "".join(parsed.text))
        self.assertNotIn("example-child", "".join(parsed.text))
        self.assertFalse(any(tag in {"form", "input", "select", "textarea"} for tag, _ in parsed.elements))
        self.assertFalse(any("hx-post" in attrs or "data-task-editor" in attrs for _, attrs in parsed.elements))

    def test_history_detail_without_successor_reference_remains_read_only(self) -> None:
        markup = render_partial("task_form", occurrence_row(_recurrence_next_uuid=None))
        self.assertIn(HISTORY_MESSAGE, markup)
        self.assertNotIn("View next instance", markup)
        self.assertNotIn("hx-post", markup)

    def test_plain_recurring_row_keeps_existing_editor(self) -> None:
        row = occurrence_row()
        row.pop("_recurrence_generated")
        markup = render_partial("task_form", row)
        self.assertIn('hx-post="/recurring/example-parent"', markup)
        self.assertIn("data-task-editor", markup)
        self.assertNotIn(HISTORY_MESSAGE, markup)

    def test_one_off_editor_is_unchanged(self) -> None:
        markup = render_partial("task_form", {"uuid": "example-task", "priority": 0}, "/tasks")
        self.assertIn('hx-post="/tasks/example-task"', markup)
        self.assertIn("data-task-editor", markup)


class OccurrenceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.row = occurrence_row()
        self.db = SimpleNamespace(
            get_recurring=Mock(side_effect=lambda uuid: deepcopy(self.row)),
            get_task=Mock(side_effect=lambda uuid: deepcopy(self.row)),
            STATUS_VALUES=("Not Started", "In Progress", "Done"),
        )
        for name in ("update_task", "set_task_status", "toggle_task_completed", "snooze_task",
                     "update_recurring", "set_recurring_status", "toggle_recurring_completed",
                     "snooze_recurring", "restore_task_row"):
            setattr(self.db, name, Mock(side_effect=ValueError(occurrences.HISTORY_MESSAGE)))
        self.host = SimpleNamespace(
            db=self.db, _require_v2=Mock(), _form_dict=task_routes._form_dict,
            _validate_recurring_form=Mock(), _stash_undo=Mock(),
            _hx_trigger=lambda **events: json.dumps(events),
            _UNDO_QUEUE={}, _UNDO_LOCK=Lock(), _sweep_undo=Mock(),
            operations=SimpleNamespace(restore_task_records=Mock()),
            clock=SimpleNamespace(local_today=lambda: date(2030, 10, 14)),
            templates=SimpleNamespace(TemplateResponse=lambda name, context, **kwargs: HTMLResponse(
                render_partial(Path(name).stem, context["t"], context["endpoint_root"]), **kwargs)),
        )
        self.host._pop_undo = lambda op_id: self.host._UNDO_QUEUE.pop(op_id, None)
        self.enterContext(patch.dict(sys.modules, {"luigi_web.application": self.host}))
        self.enterContext(patch.object(luigi_web, "application", self.host, create=True))
        app = FastAPI()
        self.app = app
        app.dependency_overrides[require_auth] = lambda: None
        app.include_router(task_routes.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)


class OccurrenceRouteTests(OccurrenceFixture):
    def test_backend_history_guard_is_safe_for_each_task_mutation(self) -> None:
        for endpoint in ("tasks", "recurring"):
            for action, payload in (("", {"task": "Example edit"}), ("/status", {"status": "Not Started"}),
                                    ("/complete", {}), ("/snooze", {"days": "1"})):
                with self.subTest(endpoint=endpoint, action=action):
                    response = self.client.post(f"/{endpoint}/example-parent{action}", data=payload)
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json(), {"detail": "Completed occurrence history cannot be changed."})
                    self.assertNotIn("HX-Trigger", response.headers)
        self.host._stash_undo.assert_not_called()

    def test_occurrence_storage_failures_are_generic_503(self) -> None:
        for name, action, payload in (
            ("update_recurring", "", {"task": "Example edit"}),
            ("set_recurring_status", "/status", {"status": "Not Started"}),
            ("toggle_recurring_completed", "/complete", {}),
            ("snooze_recurring", "/snooze", {"days": "1"}),
        ):
            with self.subTest(action=action):
                getattr(self.db, name).side_effect = occurrences.OccurrenceStorageError("synthetic internal detail")
                response = self.client.post(f"/recurring/example-parent{action}", data=payload)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("synthetic internal detail", response.text)
                self.assertNotIn("HX-Trigger", response.headers)
        self.host._stash_undo.assert_not_called()

    def test_recurring_read_failure_is_generic_and_does_not_mutate(self) -> None:
        self.db.get_recurring.side_effect = occurrences.OccurrenceStorageError("synthetic internal detail")
        for action in ("complete", "snooze"):
            response = self.client.post(f"/recurring/example-parent/{action}")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("synthetic internal detail", response.text)
        self.db.toggle_recurring_completed.assert_not_called()
        self.db.snooze_recurring.assert_not_called()

    def test_recurring_get_returns_readonly_history_and_deleted_successor_is_404(self) -> None:
        response = self.client.get("/recurring/example-parent/edit")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Next instance created", response.text)
        self.assertNotIn("hx-post", response.text)
        self.db.get_recurring.side_effect = None
        self.db.get_recurring.return_value = None
        self.assertEqual(self.client.get("/recurring/example-child/edit").status_code, 404)

    def test_undo_history_and_storage_rejections_retain_original_entry(self) -> None:
        for error, status in ((ValueError(occurrences.HISTORY_MESSAGE), 409),
                              (occurrences.OccurrenceStorageError("synthetic internal detail"), 503)):
            with self.subTest(status=status):
                entry = {"table": "recurring_tasks", "snapshot": self.row, "expires_at": 1234}
                self.host._UNDO_QUEUE["example-undo"] = entry
                self.db.restore_task_row.side_effect = error
                response = self.client.post("/undo/example-undo")
                self.assertEqual(response.status_code, status)
                self.assertNotIn("synthetic internal detail", response.text)
                self.assertNotIn("HX-Trigger", response.headers)
                self.assertIs(self.host._UNDO_QUEUE["example-undo"], entry)
        self.host.operations.restore_task_records.assert_not_called()

    def test_undo_other_errors_do_not_expose_raw_details(self) -> None:
        self.host._UNDO_QUEUE["example-undo"] = {"table": "recurring_tasks", "snapshot": self.row}
        self.db.restore_task_row.side_effect = RuntimeError("synthetic internal detail")
        response = self.client.post("/undo/example-undo")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("synthetic internal detail", response.text)


class HomeOccurrenceProjectionTests(unittest.TestCase):
    def project(self, *, tasks: list[dict] | None = None, recurring: list[dict] | None = None) -> list[dict]:
        with patch.object(home_service.db, "list_tasks", return_value=tasks or []), \
             patch.object(home_service.db, "list_recurring", return_value=recurring or []), \
             patch.object(home_service.db, "list_disciplines", return_value=[]), \
             patch.object(home_service.db, "list_completion_tasks_for_day", return_value=[]), \
             patch.object(home_service.operations, "list_today_selections", return_value=set()), \
             patch.object(home_service.operations, "list_dependencies", return_value=[]):
            return home_service.load_home_state(today=date(2030, 10, 14))["tasks"]

    def test_locked_projection_is_source_qualified_and_uses_only_optional_fields(self) -> None:
        rows = self.project(tasks=[occurrence_row()], recurring=[occurrence_row()])
        self.assertNotIn("history_locked", rows[0])
        self.assertNotIn("completed_at", rows[0])
        self.assertTrue(rows[1]["history_locked"])
        self.assertTrue(rows[1]["done"])
        self.assertEqual(rows[1]["completed_at"], "2030-10-14T12:30:00")
        self.assertEqual(set(rows[1]) - set(rows[0]), {"history_locked", "completed_at"})
        self.assertNotIn("_recurrence_next_uuid", rows[1])

    def test_plain_rows_preserve_shape_and_children_remain_mutable(self) -> None:
        plain = occurrence_row()
        plain.pop("_recurrence_generated")
        rows = self.project(recurring=[plain, occurrence_row(uuid="example-child", _recurrence_generated=False)])
        self.assertNotIn("history_locked", rows[0])
        self.assertNotIn("completed_at", rows[0])
        self.assertNotIn("history_locked", rows[1])
        self.assertEqual(rows[1]["completed_at"], "2030-10-14T12:30:00")

    def test_bad_or_missing_completion_timestamp_is_omitted(self) -> None:
        for timestamp in (None, "", "invalid"):
            with self.subTest(timestamp=timestamp):
                row = self.project(recurring=[occurrence_row(completed_time=timestamp)])[0]
                self.assertTrue(row["history_locked"])
                self.assertNotIn("completed_at", row)


class HomeOccurrenceRouteTests(OccurrenceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.app.include_router(home_routes.router)
        self.selection_write = self.enterContext(patch.object(home_routes.operations, "set_today_selection", return_value=True))
        self.enterContext(patch.object(home_routes.operations, "list_today_selections", return_value={("task", "example-parent")}))

    def test_generated_history_rejects_today_and_reschedule_before_writes(self) -> None:
        for path, payload in (("today", {"selected": "true", "day": "2030-10-14"}),
                              ("today", {"selected": "false", "day": "2030-10-14"}),
                              ("reschedule", {"due_date": "2030-10-21"}),
                              ("reschedule", {"due_date": "2030-10-14"})):
            with self.subTest(path=path, payload=payload):
                response = self.client.post(f"/home/{path}", data={"source": "recurring", "uuid": "example-parent", **payload})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json(), {"detail": "Completed occurrence history cannot be changed."})
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertNotIn("HX-Trigger", response.headers)
        self.selection_write.assert_not_called()
        self.db.update_recurring.assert_not_called()
        self.db.update_task.assert_not_called()
        self.host._stash_undo.assert_not_called()

    def test_same_uuid_one_off_selection_is_not_locked(self) -> None:
        response = self.client.post("/home/today", data={"source": "task", "uuid": "example-parent", "selected": "true", "day": "2030-10-14"})
        self.assertEqual(response.status_code, 200)
        self.selection_write.assert_called_once()

    def test_backend_race_history_guard_remains_409(self) -> None:
        self.row["_recurrence_generated"] = False
        response = self.client.post("/home/reschedule", data={"source": "recurring", "uuid": "example-parent", "due_date": "2030-10-21"})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("HX-Trigger", response.headers)
        self.db.update_recurring.assert_called_once()
        self.host._stash_undo.assert_not_called()

    def test_storage_failure_on_reschedule_is_safe_503(self) -> None:
        self.row["_recurrence_generated"] = False
        self.db.update_recurring.side_effect = occurrences.OccurrenceStorageError("synthetic internal detail")
        response = self.client.post("/home/reschedule", data={"source": "recurring", "uuid": "example-parent", "due_date": "2030-10-21"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("synthetic internal detail", response.text)
        self.assertNotIn("HX-Trigger", response.headers)


def synthetic_occurrence_app() -> FastAPI:
    from fastapi.staticfiles import StaticFiles
    from test_home_frontend import render_home, synthetic_state

    root = TASKS_ROOT.parents[2]
    state = synthetic_state()
    parent = {
        **state["tasks"][1], "id": "recurring:example-parent", "uuid": "example-parent",
        "title": "Example recurring task", "done": True, "status": "Completed",
        "history_locked": True, "completed_at": "2026-09-14T12:30:00",
        "due": "2026-09-14",
    }
    child = {
        **state["tasks"][1], "id": "recurring:example-child", "uuid": "example-child",
        "title": "Example next instance", "due": "2026-09-21",
    }
    state["tasks"] = [state["tasks"][0], parent, child]
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=root / "luigi_web" / "core" / "static"))
    app.mount("/module-assets/planning", StaticFiles(directory=TASKS_ROOT.parent / "planning" / "static"))
    app.mount("/module-assets/tasks", StaticFiles(directory=TASKS_ROOT / "static"))

    @app.get("/home", response_class=HTMLResponse)
    def home():
        return render_home(state)

    @app.get("/home/data")
    def home_data():
        return state

    @app.get("/home/layout")
    def home_layout():
        return {"saved": False, "layout": None}

    @app.get("/recurring/{row_uuid}/edit", response_class=HTMLResponse)
    def detail(row_uuid: str):
        return render_partial("task_form", occurrence_row(
            uuid=row_uuid, completed_time="2026-09-14T12:30:00", due_date="2026-09-14",
            _recurrence_generated=row_uuid == "example-parent",
        ))

    @app.get("/tasks", response_class=HTMLResponse)
    def task_rows():
        environment = template_environment()
        environment.loader = FileSystemLoader([
            TASKS_ROOT / "templates", root / "luigi_web" / "core" / "templates",
        ])
        return environment.from_string(
            '{% extends "base.html" %}{% block content %}'
            '<h1>Example recurring history</h1><div class="kanban-board" id="kanban-board">'
            '<section class="kanban-column" data-status="Completed"><h2>Completed</h2>'
            '<div class="kanban-column-body sortable" data-status="Completed" data-endpoint="/recurring">'
            '{% include "partials/task_card.html" %}</div></section>'
            '<section class="kanban-column" data-status="Not Started"><h2>Not Started</h2>'
            '<div class="kanban-column-body sortable" data-status="Not Started" data-endpoint="/recurring">'
            '<p class="empty">No tasks</p></div></section></div>'
            '{% include "partials/task_list.html" %}{% endblock %}'
        ).render(
            t=occurrence_row(), rows=[occurrence_row()], endpoint_root="/recurring",
            statuses=("Not Started", "In Progress", "Completed"),
            page_title="Example recurring history", active_nav="tasks", landing_path="/home",
            navigation_groups=[], module_count=2, module_enabled=lambda name: False,
            shell_asset_version="synthetic", asset_version="synthetic",
        )

    return app


class OccurrenceBrowserFixtureTests(unittest.TestCase):
    def test_browser_fixture_uses_only_synthetic_templates_and_assets(self) -> None:
        with TestClient(synthetic_occurrence_app()) as client:
            for path in ("/home", "/tasks", "/module-assets/planning/home.js", "/module-assets/tasks/task-editor.js"):
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code, 200)
            parent = client.get("/home/data").json()["tasks"][1]
            self.assertTrue(parent["history_locked"])
            self.assertEqual(parent["completed_at"], "2026-09-14T12:30:00")
            detail = client.get("/recurring/example-parent/edit").text
            self.assertIn("Next instance created", detail)
            self.assertNotIn("data-task-editor", detail)


if __name__ == "__main__":
    unittest.main()