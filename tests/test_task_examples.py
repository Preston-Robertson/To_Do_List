"""Offline contracts for the synthetic-only Tasks examples."""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType
from unittest import mock

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from luigi_web.core.module_registry import Module, ModuleRegistry
from luigi_web.modules.tasks import examples


class ExampleMarkup(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.ids = []
        self.forms = []
        self.seed = ""
        self.in_seed = False
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.append(attributes["id"])
        if tag == "script" and attributes.get("id") == "te-seed":
            self.in_seed = True
        if tag == "form" and attributes.get("id", "").startswith("te-"):
            self.forms.append(attributes)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_seed = False

    def handle_data(self, data):
        if self.in_seed:
            self.seed += data


class TaskExamplesTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "synthetic-examples-session"}))
        self.application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.application.state.modules = ModuleRegistry([])
        self.application.include_router(examples.router)
        self.module_directory = Path(examples.__file__).parent
        self.application.mount("/module-assets/tasks", StaticFiles(directory=self.module_directory / "static"))
        self.application.mount("/static", StaticFiles(directory=self.module_directory.parents[1] / "core" / "static"))
        self.client = self.enterContext(TestClient(self.application, follow_redirects=False))
        self.authorization = {"Authorization": "Bearer synthetic-examples-session"}

    def captured_context(self):
        def response(**kwargs):
            return HTMLResponse("Synthetic example", headers=kwargs["headers"])

        with mock.patch.object(examples.templates, "TemplateResponse", side_effect=response) as render:
            result = self.client.get("/tasks/preview", headers=self.authorization)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(render.call_args.kwargs["name"], "task_examples.html")
        return render.call_args.kwargs["context"]

    def test_authentication_required_before_render(self):
        with mock.patch.object(examples.templates, "TemplateResponse") as render:
            self.assertEqual(self.client.get("/tasks/preview").status_code, 401)
            result = self.client.get("/tasks/preview", headers={"Accept": "text/html"})
            self.assertEqual(result.status_code, 303)
            self.assertEqual(result.headers["location"], "/login")
            render.assert_not_called()

    def test_fixed_context_and_fresh_literal_state(self):
        context = self.captured_context()
        self.assertEqual(context["page_title"], "Tasks examples")
        self.assertEqual(context["active_nav"], "tasks")
        seed = context["example_seed"]
        self.assertEqual(seed, examples.example_state())
        self.assertEqual(len({task["id"] for task in seed["tasks"]}), 5)
        self.assertEqual({rule["type"] for rule in seed["rules"]}, {"completion", "dependency", "reminder"})
        self.assertTrue(all("example" in task["title"].lower() for task in seed["tasks"]))
        seed["tasks"][0]["title"] = "Changed example"
        seed["rules"].clear()
        fresh = self.captured_context()["example_seed"]
        self.assertEqual(fresh["tasks"][0]["title"], "Review example outline")
        self.assertEqual(len(fresh["rules"]), 4)

    def test_get_only_no_mutation_endpoints(self):
        self.assertEqual(len(examples.router.routes), 1)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(self.client.request(method, "/tasks/preview", headers=self.authorization).status_code, 405)

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

    def test_server_render_and_accessible_structure(self):
        response = self.client.get("/tasks/preview", headers=self.authorization)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        markup = ExampleMarkup(response.text)
        self.assertEqual(len(markup.ids), len(set(markup.ids)))
        self.assertEqual(json.loads(markup.seed), examples.example_state())
        self.assertEqual(len(markup.forms), 4)
        self.assertTrue(all(form["method"] == "dialog" and "action" not in form for form in markup.forms))
        for label in ("Synthetic example", "Review example outline", "Example project", "List workspace", "Automation", "Completion triggers", "Dependencies", "Reminders"):
            self.assertIn(label, response.text)
        self.assertIn('href="/tasks"', response.text)
        self.assertIn('aria-label="Reopen Check example references"', response.text)
        for identifier in ("te-task-dialog", "te-rule-dialog", "te-delete-dialog", "te-capture", "te-task-list", "te-completion-list", "te-dependency-list", "te-reminder-list", "te-status"):
            self.assertIn(identifier, markup.ids)
        for panel in re.findall(r'aria-controls="(te-[^"]+)"', response.text):
            self.assertIn(panel, markup.ids)
        self.assertNotRegex(response.text.lower(), r"today|datepresettoday|/home/today")

    def test_shell_has_no_reminder_polling_or_assistant(self):
        self.application.state.modules = ModuleRegistry([
            Module(id="tasks", label="Tasks", description="Synthetic module", router="example:router"),
            Module(id="assistant", label="Assistant", description="Synthetic module", router="example:router"),
        ])
        response = self.client.get("/tasks/preview", headers=self.authorization)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("/reminders/count", response.text)
        self.assertNotIn('hx-trigger="load', response.text)
        self.assertNotIn("/module-assets/assistant/", response.text)
        self.assertNotIn("data-assistant-open", response.text)

    def test_completion_rules_describe_new_instances_not_reopening(self):
        response = self.client.get("/tasks/preview", headers=self.authorization)
        self.assertIn("create a new copy of Draft example summary", response.text)
        self.assertIn('id="te-target-label">Create a new copy of</span>', response.text)
        self.assertNotIn("completes, reopen", response.text)
        self.assertIn("data-te-completed", response.text)

    def test_example_generation_preserves_template_and_rule_identity(self):
        script = (self.module_directory / "static" / "task-examples.js").read_text(encoding="utf-8")
        self.assertNotIn("taskById(rule.target).done = false", script)
        self.assertIn("...structuredClone(template), id:", script)
        self.assertIn("ruleType: template.ruleType || template.id", script)
        self.assertIn("done: false, completedAt: null", script)
        self.assertIn("if (completing === task.done) return;", script)
        self.assertIn("rule.source === (task.ruleType || task.id)", script)
        self.assertIn('kind === "dependency" && source.value === target.value', script)

    def test_no_database_or_provider_access(self):
        forbidden = mock.Mock(side_effect=AssertionError("Examples attempted data access"))
        blocked = ModuleType("synthetic_blocked_module")
        blocked.__getattr__ = forbidden
        modules = {name: blocked for name in (
            "luigi_web.application", "luigi_web.db", "luigi_web.chat_tools", "luigi_web.llm",
            "luigi_web.modules.tasks.repository", "luigi_web.modules.tasks.operations",
            "luigi_web.modules.assistant.providers", "luigi_web.modules.assistant.tools",
        )}
        with mock.patch.dict(sys.modules, modules), mock.patch("sqlite3.connect", forbidden), mock.patch("socket.create_connection", forbidden):
            response = self.client.get("/tasks/preview", headers=self.authorization)
        self.assertEqual(response.status_code, 200)
        forbidden.assert_not_called()

    def test_html_and_seed_escape_user_shaped_text(self):
        seed = examples.example_state()
        text = '</script><img src=x onerror="alert(1)">'
        seed["tasks"][0]["title"] = text
        seed["tasks"][0]["project"] = text
        with mock.patch.object(examples, "example_state", return_value=seed):
            response = self.client.get("/tasks/preview", headers=self.authorization)
        self.assertNotIn(text, response.text)
        self.assertIn("&lt;/script&gt;", response.text)
        self.assertEqual(json.loads(ExampleMarkup(response.text).seed)["tasks"][0]["title"], text)

    def test_local_assets_and_memory_only_script(self):
        response = self.client.get("/tasks/preview", headers=self.authorization)
        for filename, media_type in (("task-examples.css", "text/css"), ("task-examples.js", "javascript")):
            path = "/module-assets/tasks/" + filename
            self.assertIn(path, response.text)
            asset = self.client.get(path)
            self.assertEqual(asset.status_code, 200)
            self.assertIn(media_type, asset.headers["content-type"])
        script = (self.module_directory / "static" / "task-examples.js").read_text(encoding="utf-8")
        self.assertLessEqual(len(script.splitlines()), 350)
        self.assertNotRegex(script, r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|localStorage|sessionStorage|indexedDB|confirm)\b")
        self.assertNotRegex(script, r"innerHTML|outerHTML|insertAdjacentHTML|document\.cookie|\beval\s*\(")
        self.assertIn("textContent", script)
        self.assertIn("htmx:beforeRequest", script)
        self.assertIn("event.preventDefault()", script)
        self.assertIn("ArrowLeft", script)
        self.assertIn("ArrowRight", script)
        template = (self.module_directory / "templates" / "task_examples.html").read_text(encoding="utf-8")
        self.assertNotRegex(template, r"https?://|hx-(get|post|put|patch|delete)|\son(?:click|change|submit)=")
        self.assertNotRegex(template.lower() + script.lower(), r"today|datepresettoday|/home/today")
        icons = self.module_directory.parents[1] / "core" / "static" / "icons" / "lucide"
        for name in re.findall(r"shell_icon\('([\w-]+)'\)", template):
            self.assertTrue((icons / (name + ".svg")).is_file(), name)


if __name__ == "__main__":
    unittest.main()