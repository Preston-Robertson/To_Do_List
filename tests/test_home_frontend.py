from pathlib import Path
import json
import re
import unittest

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parents[1]
PLANNING = ROOT / "luigi_web" / "modules" / "planning"


def synthetic_state():
    return {
        "today": "2026-09-18", "selection_available": True, "selection_error": None,
        "tasks": [
            {"id": f"{source}:example-id", "uuid": "example-id", "source": source,
             "title": f"Example {source}", "notes": "", "due": "2026-09-20",
             "today": False, "priority": 1, "done": False, "status": "Not Started",
             "category": "Example category", "blockers": [], "endpoint": endpoint}
            for source, endpoint in (("task", "/tasks"), ("recurring", "/recurring"))
        ],
        "habits": [{"id": "example-habit", "title": "Example habit", "done": False,
                    "frequency_per_week": 3, "streak": 2}],
        "shortcuts": [{"label": "Tasks", "href": "/tasks", "icon": "list-checks"}],
    }


def render_home(state=None):
    templates = Environment(loader=FileSystemLoader([
        PLANNING / "templates", ROOT / "luigi_web" / "core" / "templates",
    ]), autoescape=select_autoescape())
    return templates.get_template("home.html").render(
        home_state=state if state is not None else synthetic_state(),
        today_iso="2026-09-18", home_date="Friday, September 18, 2026",
        page_title="Home", active_nav="home", landing_path="/home",
        navigation_groups=[], module_count=2, module_enabled=lambda name: False,
        shell_asset_version="synthetic", asset_version="synthetic",
    )


def synthetic_home_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    layout = None
    app.mount("/static", StaticFiles(directory=ROOT / "luigi_web" / "core" / "static"))
    app.mount("/module-assets/planning", StaticFiles(directory=PLANNING / "static"))

    @app.get("/home", response_class=HTMLResponse)
    def home():
        return HTMLResponse(render_home(), headers={"Cache-Control": "no-store"})

    @app.get("/home/data")
    def data():
        from fastapi.responses import JSONResponse
        return JSONResponse(synthetic_state(), headers={"Cache-Control": "no-store"})

    @app.get("/home/layout")
    def get_layout():
        return {"saved": layout is not None, "layout": layout}

    @app.put("/home/layout")
    async def put_layout(request: Request):
        nonlocal layout
        layout = await request.json()
        return {"saved": True, "layout": layout}

    @app.get("/tasks/new", response_class=HTMLResponse)
    @app.get("/tasks/{row_uuid}/edit", response_class=HTMLResponse)
    def task_form(row_uuid: str | None = None):
        from luigi_web.modules.tasks import recurrence, repository

        templates = Environment(loader=FileSystemLoader(
            ROOT / "luigi_web" / "modules" / "tasks" / "templates"
        ), autoescape=select_autoescape())
        templates.globals.update(
            recurring_days_list=repository.recurring_days_list,
            recurrence_schedule_type=repository.recurrence_schedule_type,
            WEEKDAY_LABELS=repository.WEEKDAY_LABELS,
            MONTH_ORDINAL_OPTIONS=recurrence.MONTH_ORDINAL_OPTIONS,
        )
        return templates.get_template("partials/task_form.html").render(
            t={"uuid": row_uuid, "task": "Example task", "priority": 1} if row_uuid else {},
            endpoint_root="/tasks", is_new=row_uuid is None, statuses=["Not Started", "Completed"],
        )

    @app.post("/tasks")
    @app.post("/tasks/{row_uuid}")
    def reject_fixture_write(row_uuid: str | None = None):
        return HTMLResponse("Fixture save rejected", status_code=422)

    return app


class HomeLayoutFrontendTests(unittest.TestCase):
    def test_nested_widgets_stay_in_their_original_lane(self):
        script = (PLANNING / "static" / "home-layout.js").read_text(encoding="utf-8")
        self.assertIn('grid.querySelectorAll(".widget[data-widget]")', script)
        self.assertIn("[id, widget.parentElement]", script)
        self.assertIn("parents.get(id).append(widget)", script)
        self.assertIn("parents.get(candidate) === parents.get(id)", script)
        self.assertNotIn("grid.append(widget)", script)

    def test_layout_retains_all_thirteen_storage_ids(self):
        script = (PLANNING / "static" / "home-layout.js").read_text(encoding="utf-8")
        labels = script.split("const LABELS = {", 1)[1].split("};", 1)[0]
        ids = re.findall(r'(?:"([\w-]+)"|(\w+)):\s*"', labels)
        self.assertEqual({quoted or bare for quoted, bare in ids}, {
            "overdue", "upcoming", "open-tasks", "disc-pending", "gnw-playing",
            "gnw-watching", "disc-streaks", "follow-ups", "recent-done",
            "weekly-review", "disc-week", "task-week", "activity",
        })
        self.assertIn('const KEY = "luigi.home.layout"', script)
        self.assertIn("version: 1, order: [...IDS], hidden: [], pinned: []", script)
        self.assertIn('"open-tasks": "Tasks"', labels)
        self.assertIn('"disc-pending": "Habits"', labels)


class HomeFrontendTests(unittest.TestCase):
    def test_assets_and_existing_modal_forms_are_served_in_isolation(self):
        with TestClient(synthetic_home_app()) as client:
            for asset in ("home.js", "home.css", "home-layout.js", "home-layout.css"):
                self.assertEqual(client.get(f"/module-assets/planning/{asset}").status_code, 200)
            self.assertEqual(client.get("/home").headers["cache-control"], "no-store")
            self.assertEqual(client.get("/home/data").json(), synthetic_state())
            self.assertIn('hx-target="#kanban-board"', client.get("/tasks/new").text)
            self.assertIn('hx-target="#card-example-id"', client.get("/tasks/example-id/edit").text)

    def test_live_assets_and_no_preview_or_inline_assistant(self):
        html = render_home()
        for asset in ("home.js", "home.css", "home-layout.js", "home-layout.css"):
            self.assertIn(f"/module-assets/planning/{asset}?v=synthetic", html)
            self.assertTrue((PLANNING / "static" / asset).is_file())
        for forbidden in ("home-preview", "data-preview", "/home/preview", 'id="chat-panel"', "scroll-y"):
            self.assertNotIn(forbidden, html)

    def test_live_state_is_json_escaped_and_source_qualified(self):
        state = synthetic_state()
        state["tasks"][0].update(today=True, title="Example <script>alert(1)</script>",
                                  due="2026-09-17", blockers=["Example prerequisite"])
        state["tasks"][1]["today"] = True
        html = render_home(state)
        encoded = re.search(r'<script type="application/json" id="home-state">(.*?)</script>', html, re.S).group(1)
        self.assertEqual(json.loads(encoded), state)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("Example &lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn('data-task-id="task:example-id"', html)
        self.assertIn('data-task-id="recurring:example-id"', html)
        self.assertIn("Blocked by Example prerequisite", html)
        self.assertIn(">Overdue</span>", html)

    def test_due_date_does_not_implicitly_select_today(self):
        state = synthetic_state()
        state["tasks"][0]["due"] = state["today"]
        html = render_home(state)
        task_list = html.split('id="home-task-list"', 1)[1].split("</ul>", 1)[0]
        self.assertNotIn("data-task-id", task_list)
        self.assertIn("No tasks selected for today.", html)
        self.assertIn("0 of 0 tasks complete", html)

    def test_completed_today_is_not_reported_as_unselected(self):
        state = synthetic_state()
        state["tasks"][0].update(today=True, done=True)
        html = render_home(state)
        self.assertIn("Today's selected tasks are complete.", html)
        self.assertIn("1 of 1 tasks complete", html)

    def test_customization_has_one_root_two_lanes_and_existing_hooks(self):
        html = render_home()
        self.assertEqual(html.count('data-home-layout data-home-preferences="/home/layout"'), 1)
        self.assertEqual(re.findall(r'data-home-zone="([^"]+)"', html), ["main", "support"])
        self.assertEqual(re.findall(r'class="widget" data-widget="([^"]+)"', html),
                         ["open-tasks", "disc-pending", "task-week", "upcoming", "gnw-playing"])
        for hook in ("open", "form", "list", "reset", "cancel", "save", "empty", "status", "error"):
            self.assertIn(f"data-home-layout-{hook}", html)
        for scope in ("browser", "shared"):
            self.assertIn(f'value="{scope}"', html)

    def test_existing_editor_and_live_write_contracts(self):
        html = render_home()
        script = (PLANNING / "static" / "home.js").read_text(encoding="utf-8")
        self.assertIn('hx-get="/tasks/new"', html)
        self.assertIn('hx-target="#modal-body"', html)
        self.assertIn('data-home-data="/home/data"', html)
        for contract in ('taskPath(task, "edit")', 'taskPath(task, "complete")',
                         'write("/home/today"', 'write("/home/reschedule"',
                         'selected: String(selected), day', 'due_date: due',
                         '/discipline/${encodeURIComponent(habit.id)}/today',
                         'action: marked ? "mark" : "unmark"',
                         'form.setAttribute("hx-target", "#modal-body")',
                         'form.setAttribute("hx-swap", "none")',
                         'form.setAttribute("hx-sync", "this:drop")',
                         'form.setAttribute("hx-disabled-elt", "find button[type=\'submit\']")'):
            self.assertIn(contract, script)
        self.assertIn('name="due_date"', html)
        self.assertIn('data-home-action="clear-date"', html)

    def test_confirmed_refresh_only_and_safe_event_storage_contract(self):
        script = (PLANNING / "static" / "home.js").read_text(encoding="utf-8")
        for contract in ('if (!response.ok)', 'if (!verify(result.data))', 'await loadState()',
                         'error.status === 409', 'Showing stale data', 'Reload Home before retrying',
                         'input.checked = task.done', 'input.checked = habit.done',
                         'new CustomEvent("showUndo"', 'label: "Home change saved"',
                         'cache: "no-store"', 'redirect: "error"'):
            self.assertIn(contract, script)
        for forbidden in ("localStorage", "innerHTML", 'CustomEvent("reloadBoard"', "HX-Refresh",
                          "grid.append"):
            self.assertNotIn(forbidden, script)
        self.assertEqual(script.count("sessionStorage.setItem"), 1)
        self.assertIn("sessionStorage.setItem(FILTER_KEY, filter)", script)
        self.assertIn('document.body.addEventListener("reloadBoard"', script)
        self.assertIn("if (event.detail.successful) return;", script)

    def test_ssr_without_javascript_retains_tasks_habits_progress_and_links(self):
        state = synthetic_state()
        state["tasks"][0]["today"] = True
        html = render_home(state)
        visible = html.split('<script type="application/json" id="home-state">', 1)[0]
        for text in ("Example task", "Example habit", "0 of 1 tasks complete", 'href="/tasks"', "<noscript>"):
            self.assertIn(text, visible)


if __name__ == "__main__":
    unittest.main()