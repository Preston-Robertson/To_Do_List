"""Offline contracts for the synthetic-only Discipline history example."""
from __future__ import annotations

import ast
import base64
import json
import mimetypes
import os
import re
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType
from unittest import mock

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from luigi_web.core.module_registry import Module, ModuleRegistry
from luigi_web.core.static_assets import ModuleStaticFiles
from luigi_web.modules.discipline import examples
from luigi_web.paths import STATIC_DIR


class ExampleMarkup(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.ids = []
        self.elements = []
        self.seed = ""
        self.in_seed = False
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if "id" in attributes:
            self.ids.append(attributes["id"])
        if tag == "script" and attributes.get("id") == "dh-seed":
            self.in_seed = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_seed = False

    def handle_data(self, data):
        if self.in_seed:
            self.seed += data


class DisciplineExampleTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "synthetic-history-session"}))
        self.application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.application.state.modules = ModuleRegistry([])
        self.application.include_router(examples.router)
        self.module_directory = Path(examples.__file__).parent
        self.static_directory = STATIC_DIR
        self.application.mount("/module-assets/discipline", StaticFiles(directory=self.module_directory / "static"))
        self.application.mount("/static", ModuleStaticFiles(directory=self.static_directory))
        self.client = self.enterContext(TestClient(self.application, follow_redirects=False))
        self.authorization = {"Authorization": "Bearer synthetic-history-session"}

    def captured_context(self):
        def response(**kwargs):
            return HTMLResponse("Synthetic example", headers=kwargs["headers"])

        with mock.patch.object(examples.templates, "TemplateResponse", side_effect=response) as render:
            result = self.client.get("/discipline/history-preview", headers=self.authorization)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(render.call_args.kwargs["name"], "history_example.html")
        return render.call_args.kwargs["context"]

    def test_authentication_required_before_render(self):
        with mock.patch.object(examples.templates, "TemplateResponse") as render:
            self.assertEqual(self.client.get("/discipline/history-preview").status_code, 401)
            response = self.client.get("/discipline/history-preview", headers={"Accept": "text/html"})
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/login")
            render.assert_not_called()

    def test_get_only_no_mutation_endpoints(self):
        self.assertEqual(len(examples.router.routes), 1)
        self.assertEqual(examples.router.routes[0].methods, {"GET"})
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(self.client.request(method, "/discipline/history-preview", headers=self.authorization).status_code, 405)

    def test_fixed_context_and_fresh_literal_state_per_request(self):
        context = self.captured_context()
        self.assertEqual(context["page_title"], "Discipline history example")
        self.assertEqual(context["active_nav"], "discipline")
        seed = context["example_seed"]
        self.assertEqual(seed, examples.example_state())
        seed["completions"].clear()
        seed["weekStarts"].clear()
        seed["name"] = "Changed example"
        fresh = self.captured_context()["example_seed"]
        self.assertEqual(fresh["name"], "Example reading practice")
        self.assertEqual(len(fresh["completions"]), 15)
        self.assertEqual(len(fresh["weekStarts"]), 6)

    def test_seed_has_six_monday_weeks_unique_dates_and_distinct_timestamps(self):
        seed = examples.example_state()
        self.assertEqual(seed["weeklyTarget"], 3)
        self.assertEqual(seed["sampleToday"], "2030-04-24")
        self.assertEqual(len({record["date"] for record in seed["completions"]}), 15)
        weeks = [date.fromisoformat(value) for value in seed["weekStarts"]]
        self.assertTrue(all(week.weekday() == 0 for week in weeks))
        self.assertEqual(weeks, [weeks[0] + timedelta(weeks=offset) for offset in range(6)])
        totals = []
        for week in weeks:
            totals.append(sum(week <= date.fromisoformat(record["date"]) < week + timedelta(days=7) for record in seed["completions"]))
        self.assertEqual(totals, [3, 2, 4, 1, 3, 2])
        for record in seed["completions"]:
            self.assertEqual(record["date"], record["completedAt"][:10])
            self.assertLessEqual(record["date"], seed["sampleToday"])
            self.assertLessEqual(datetime.fromisoformat(record["completedAt"]), datetime.fromisoformat(record["loggedAt"]))
            self.assertLessEqual(datetime.fromisoformat(record["loggedAt"]), datetime.fromisoformat(seed["sampleNow"]))
        self.assertTrue(any(record["date"] != record["loggedAt"][:10] for record in seed["completions"]))

    def test_controller_imports_only_auth_templates_and_http(self):
        tree = ast.parse(Path(examples.__file__).read_text(encoding="utf-8"))
        imports = {
            (node.module or "") + "." + alias.name
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        self.assertEqual(imports, {
            "__future__.annotations", "fastapi.APIRouter", "fastapi.Depends", "fastapi.Request",
            "fastapi.responses.HTMLResponse", "auth.require_auth", "core.templating",
        })
        self.assertFalse(any(isinstance(node, ast.Import) for node in ast.walk(tree)))
        seed_function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "example_state")
        self.assertEqual(ast.literal_eval(seed_function.body[0].value), examples.example_state())

    def test_server_render_tabs_dialog_and_unique_ids(self):
        response = self.client.get("/discipline/history-preview", headers=self.authorization)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        markup = ExampleMarkup(response.text)
        self.assertEqual(len(markup.ids), len(set(markup.ids)))
        self.assertEqual(json.loads(markup.seed), examples.example_state())
        elements = {attributes["id"]: (tag, attributes) for tag, attributes in markup.elements if "id" in attributes}
        for view in ("month", "year", "log"):
            tab = elements[f"dh-{view}-tab"][1]
            panel = elements[f"dh-{view}-panel"][1]
            self.assertEqual(tab["role"], "tab")
            self.assertEqual(tab["aria-controls"], f"dh-{view}-panel")
            self.assertEqual(tab["aria-selected"], str(view == "month").lower())
            self.assertEqual(tab["tabindex"], "0" if view == "month" else "-1")
            self.assertEqual(panel["role"], "tabpanel")
            self.assertEqual(panel["aria-labelledby"], f"dh-{view}-tab")
            self.assertEqual("hidden" in panel, view != "month")
        self.assertEqual(elements["dh-day-dialog"][0], "dialog")
        self.assertEqual(elements["dh-day-dialog"][1]["aria-labelledby"], "dh-day-heading")
        self.assertEqual(elements["dh-day-form"][1]["method"], "dialog")
        self.assertNotIn("action", elements["dh-day-form"][1])
        for label in ("Synthetic example", "Example reading practice", "Back to Discipline", "Mark complete", "Remove completion", "Weekly totals", "Completed at (UTC)", "Logged at (UTC)"):
            self.assertIn(label, response.text)
        self.assertIn('href="/discipline"', response.text)
        self.assertNotRegex(response.text.lower(), r"streak|/home/today|data-home-task|delete habit|preferred weekdays")

    def test_shell_suppresses_assistant_and_reminder_side_effects(self):
        self.application.state.modules = ModuleRegistry([
            Module(id=identifier, label=identifier.title(), description="Synthetic module", router="example:router")
            for identifier in ("tasks", "discipline", "assistant", "feedback")
        ])
        response = self.client.get("/discipline/history-preview", headers=self.authorization)
        self.assertEqual(response.status_code, 200)
        for forbidden in ("/reminders/count", 'hx-trigger="load', "/module-assets/assistant/", "data-assistant-open", "/feedback/new", "/module-assets/tasks/"):
            self.assertNotIn(forbidden, response.text)

    def test_no_database_provider_or_storage_access_during_render(self):
        forbidden = mock.Mock(side_effect=AssertionError("Example attempted external data access"))
        blocked = ModuleType("synthetic_blocked_module")
        blocked.__getattr__ = forbidden
        modules = {name: blocked for name in (
            "luigi_web.application", "luigi_web.db", "luigi_web.chat_tools", "luigi_web.llm",
            "luigi_web.modules.tasks.repository", "luigi_web.modules.tasks.operations",
            "luigi_web.modules.assistant.providers", "luigi_web.modules.assistant.tools",
        )}
        with mock.patch.dict(sys.modules, modules), mock.patch("sqlite3.connect", forbidden), mock.patch("socket.create_connection", forbidden):
            response = self.client.get("/discipline/history-preview", headers=self.authorization)
        self.assertEqual(response.status_code, 200)
        forbidden.assert_not_called()

    def test_html_and_json_escape_synthetic_user_shaped_values(self):
        seed = examples.example_state()
        text = '</script><img src=x onerror="alert(1)">&example'
        seed["name"] = text
        with mock.patch.object(examples, "example_state", return_value=seed):
            response = self.client.get("/discipline/history-preview", headers=self.authorization)
        self.assertNotIn(text, response.text)
        self.assertIn("&lt;/script&gt;", response.text)
        self.assertEqual(json.loads(ExampleMarkup(response.text).seed)["name"], text)

    def test_local_assets_and_icons_resolve(self):
        response = self.client.get("/discipline/history-preview", headers=self.authorization)
        for filename, media_type in (("history-example.css", "text/css"), ("history-example.js", "javascript")):
            path = "/module-assets/discipline/" + filename
            self.assertIn(path, response.text)
            asset = self.client.get(path)
            self.assertEqual(asset.status_code, 200)
            self.assertIn(media_type, asset.headers["content-type"])
        template = (self.module_directory / "templates" / "history_example.html").read_text(encoding="utf-8")
        self.assertNotRegex(template, r"https?://|<svg|hx-(get|post|put|patch|delete)|\son(?:click|change|submit)=")
        for name in re.findall(r"shell_icon\('([\w-]+)'\)", template):
            self.assertTrue((self.static_directory / "icons" / "lucide" / (name + ".svg")).is_file(), name)

    def test_script_has_no_network_storage_unsafe_html_or_live_controls(self):
        script = (self.module_directory / "static" / "history-example.js").read_text(encoding="utf-8")
        self.assertNotRegex(script, r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|localStorage|sessionStorage|indexedDB)\b")
        self.assertNotRegex(script, r"innerHTML|outerHTML|insertAdjacentHTML|document\.cookie|\beval\s*\(|new Date\(\)|Date\.now")
        self.assertNotRegex(script, r"/home/today|data-home-task|datePresetToday|/discipline/(?:complete|uncomplete)|streak")
        self.assertIn("textContent", script)
        self.assertIn('"htmx:beforeRequest"', script)
        self.assertIn("event.preventDefault()", script)
        self.assertIn("[data-command-open]", script)
        self.assertIn("[data-command-input]", script)
        self.assertIn("event.stopImmediatePropagation()", script)
        self.assertIn("ArrowLeft", script)
        self.assertIn("ArrowRight", script)

    def test_corrections_are_date_guarded_unique_and_preserve_logged_time(self):
        script = (self.module_directory / "static" / "history-example.js").read_text(encoding="utf-8")
        for contract in (
            "value <= seed.sampleToday", "button.disabled = !editable(value)",
            "!dialog.open || !editable(selectedDay) || records.has(selectedDay)",
            "!dialog.open || !editable(selectedDay) || !records.has(selectedDay)",
            "completedAt > seed.sampleNow", "loggedAt: seed.sampleNow",
            "records.set(selectedDay", "records.delete(selectedDay)", "records = previous; previous = null",
            "new Map(seed.completions.map", "right.date.localeCompare(left.date)",
            "value >= start && value < addDays(start, 7)",
        ):
            self.assertIn(contract, script)
        self.assertIn('find("#dh-day-heading").textContent = formatDay(value)', script)
        self.assertIn("(start - first) / dayLength + offset", script)
        self.assertIn("offset + 365", script)

    def test_responsive_scoping_and_touch_targets(self):
        css = (self.module_directory / "static" / "history-example.css").read_text(encoding="utf-8")
        self.assertIn("minmax(44px, 1fr)", css)
        self.assertIn("grid-template-rows: repeat(6, 52px)", css)
        self.assertIn("overflow-x: auto", css)
        self.assertIn(".dh-example [hidden]", css)
        self.assertIn("letter-spacing: 0", css)
        self.assertNotRegex(css, r"https?://|font-size:\s*[^;]*(?:vw|cqw)|\.heatmap|\.discipline-grid")


def export_browser_fixture():
    module_directory = Path(examples.__file__).parent
    static_directory = STATIC_DIR
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    application.state.modules = ModuleRegistry([])
    application.include_router(examples.router)
    with mock.patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "synthetic-history-session"}), TestClient(application) as client:
        response = client.get("/discipline/history-preview", headers={"Authorization": "Bearer synthetic-history-session"})
        response.raise_for_status()
    assets = {}
    paths = [("/static/" + path.relative_to(static_directory).as_posix(), path) for path in static_directory.rglob("*") if path.is_file()]
    paths.extend(("/module-assets/discipline/" + name, module_directory / "static" / name) for name in ("history-example.js", "history-example.css"))
    for url, path in paths:
        assets[url] = {"contentType": mimetypes.guess_type(path.name)[0] or "application/octet-stream", "base64": base64.b64encode(path.read_bytes()).decode("ascii")}
    destination = Path(tempfile.mkdtemp(prefix="synthetic-discipline-browser-")) / "fixture.json"
    destination.write_text(json.dumps({"html": response.text, "assets": assets}), encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    if sys.argv[1:] == ["--browser-fixture"]:
        export_browser_fixture()
    else:
        unittest.main()