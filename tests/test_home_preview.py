"""Synthetic-only contracts for the isolated Home proposal."""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import unittest
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType
from unittest import mock

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from luigi_web.core.module_registry import Module, ModuleRegistry
from luigi_web.modules.planning import home_preview


class PreviewMarkup(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.seed = ""
        self.in_seed = False
        self.ids = []
        self.forms = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.append(attributes["id"])
        if tag == "script" and attributes.get("id") == "hp-seed":
            self.in_seed = True
        if tag == "form" and attributes.get("id", "").startswith("hp-"):
            self.forms.append(attributes)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_seed = False

    def handle_data(self, data):
        if self.in_seed:
            self.seed += data


class HomePreviewTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "synthetic-preview-session"}))
        self.enterContext(mock.patch.object(home_preview.clock, "local_today", return_value=date(2026, 9, 17)))
        self.application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.application.state.modules = ModuleRegistry([])
        self.application.include_router(home_preview.router)
        self.module_directory = Path(home_preview.__file__).parent
        self.application.mount("/module-assets/planning", StaticFiles(directory=self.module_directory / "static"))
        self.client = self.enterContext(TestClient(self.application, follow_redirects=False))
        self.authorization = {"Authorization": "Bearer synthetic-preview-session"}

    def captured_context(self):
        with mock.patch.object(home_preview.templates, "TemplateResponse", return_value=HTMLResponse("synthetic")) as render:
            response = self.client.get("/home/preview", headers=self.authorization)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(render.call_args.kwargs["name"], "home_preview.html")
        return render.call_args.kwargs["context"]

    def test_authentication_required(self):
        with mock.patch.object(home_preview.templates, "TemplateResponse") as render:
            self.assertEqual(self.client.get("/home/preview").status_code, 401)
            response = self.client.get("/home/preview", headers={"Accept": "text/html"})
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/login")
            render.assert_not_called()

    def test_seed_is_unique_synthetic_and_relative_to_today(self):
        context = self.captured_context()
        seed = context["preview_seed"]
        self.assertEqual(context["page_title"], "Home preview")
        self.assertEqual(context["active_nav"], "home")
        self.assertEqual(seed["today"], "2026-09-17")
        self.assertEqual(len(seed["tasks"]), 5)
        self.assertEqual(len({task["id"] for task in seed["tasks"]}), 5)
        self.assertTrue(all(task["title"].startswith("Example:") for task in seed["tasks"]))
        self.assertEqual(seed["tasks"][0]["due"], "2026-09-16")
        self.assertEqual(seed["tasks"][3]["due"], "2026-09-18")
        self.assertTrue(seed["tasks"][2]["today"])
        self.assertEqual(seed["tasks"][2]["due"], "")
        self.assertEqual(len(seed["habits"]), 2)
        self.assertEqual(context["preview_shortcuts"], [])

    def test_continue_links_only_use_enabled_static_modules(self):
        self.application.state.modules.is_enabled = lambda name: name in {"media", "cards"}
        shortcuts = self.captured_context()["preview_shortcuts"]
        self.assertEqual([link["href"] for link in shortcuts], ["/games", "/cards"])
        self.application.state.modules.is_enabled = lambda name: name == "cards"
        self.assertEqual([link["label"] for link in self.captured_context()["preview_shortcuts"]], ["Trading cards"])

    def test_route_is_get_only(self):
        self.assertEqual(self.client.post("/home/preview", headers=self.authorization).status_code, 405)
        self.assertEqual(len(home_preview.router.routes), 1)

    def test_template_render_and_preview_controls(self):
        response = self.client.get("/home/preview", headers=self.authorization)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("Synthetic preview", response.text)
        self.assertIn('href="/home"', response.text)
        markup = PreviewMarkup(response.text)
        self.assertEqual(len(markup.ids), len(set(markup.ids)))
        self.assertEqual(json.loads(markup.seed)["tasks"][0]["id"], "example-task-1")
        self.assertEqual(len(markup.forms), 3)
        self.assertTrue(all(form["method"] == "dialog" and "action" not in form for form in markup.forms))
        for identifier in ("hp-details", "hp-reschedule", "hp-add", "hp-task-list", "hp-habit-list", "hp-agenda", "hp-progress", "hp-status"):
            self.assertIn(identifier, markup.ids)

    def test_shell_has_no_automatic_live_reminder_requests(self):
        self.application.state.modules = ModuleRegistry([
            Module(id="tasks", label="Tasks", description="Synthetic module", router="example:router"),
        ])
        response = self.client.get("/home/preview", headers=self.authorization)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("/reminders/count", response.text)
        self.assertNotIn('hx-trigger="load', response.text)

    def test_data_access_and_provider_calls_are_never_used(self):
        forbidden = mock.Mock(side_effect=AssertionError("Preview attempted data access"))
        database = ModuleType("synthetic_blocked_database")
        database.__getattr__ = forbidden
        blocked_modules = {name: database for name in (
            "luigi_web.application", "luigi_web.db", "luigi_web.review", "luigi_web.finance",
            "luigi_web.modules.tasks.repository", "luigi_web.modules.planning.repository",
            "luigi_web.modules.finance.repository", "luigi_web.modules.cards.repository",
            "luigi_web.modules.characters.repository", "luigi_web.modules.assistant.providers",
        )}
        with (
            mock.patch.dict(sys.modules, blocked_modules),
            mock.patch("sqlite3.connect", forbidden),
            mock.patch("sqlalchemy.create_engine", forbidden),
            mock.patch("sqlalchemy.engine.Engine.connect", forbidden),
            mock.patch("socket.create_connection", forbidden),
        ):
            response = self.client.get("/home/preview", headers=self.authorization)
        self.assertEqual(response.status_code, 200)
        forbidden.assert_not_called()

    def test_each_request_rebuilds_fixtures(self):
        first = self.captured_context()["preview_seed"]
        first["tasks"][0]["title"] = "Example changed only in this response"
        first["tasks"][0]["done"] = True
        second = self.captured_context()["preview_seed"]
        self.assertFalse(second["tasks"][0]["done"])
        self.assertEqual(second["tasks"][0]["title"], "Example: review the launch checklist")

    def test_seed_is_json_escaped(self):
        original = home_preview.templates.TemplateResponse
        example = "Example </script><em>sample</em>"

        def render(**kwargs):
            kwargs["context"]["preview_seed"]["tasks"][0]["title"] = example
            return original(**kwargs)

        with mock.patch.object(home_preview.templates, "TemplateResponse", side_effect=render):
            response = self.client.get("/home/preview", headers=self.authorization)
        self.assertNotIn(example, response.text)
        self.assertEqual(json.loads(PreviewMarkup(response.text).seed)["tasks"][0]["title"], example)

    def test_assets_are_local_and_preview_script_has_no_io(self):
        for filename, media_type in (("home-preview.css", "text/css"), ("home-preview.js", "javascript")):
            response = self.client.get("/module-assets/planning/" + filename)
            self.assertEqual(response.status_code, 200)
            self.assertIn(media_type, response.headers["content-type"])
        script = (self.module_directory / "static" / "home-preview.js").read_text(encoding="utf-8")
        self.assertNotRegex(script, r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|localStorage|sessionStorage|indexedDB)\b")
        self.assertNotRegex(script, r"innerHTML|outerHTML|insertAdjacentHTML|document\.cookie|\beval\s*\(")
        self.assertIn("textContent", script)
        self.assertIn("htmx:beforeRequest", script)
        self.assertIn("event.preventDefault()", script)
        template = (self.module_directory / "templates" / "home_preview.html").read_text(encoding="utf-8")
        self.assertNotRegex(template, r"https?://|hx-(get|post|put|patch|delete)")
        self.assertNotRegex(template, r"\son(?:click|change|submit)=")
        icon_directory = self.module_directory.parents[1] / "core" / "static" / "icons" / "lucide"
        for name in re.findall(r"shell_icon\('([\w-]+)'\)", template):
            self.assertTrue((icon_directory / (name + ".svg")).is_file(), name)

    def test_controller_has_no_data_or_host_imports(self):
        tree = ast.parse(Path(home_preview.__file__).read_text(encoding="utf-8"))
        imports = {
            (node.module or "") + "." + alias.name
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        self.assertEqual(imports, {
            "__future__.annotations", "datetime.timedelta", "fastapi.APIRouter", "fastapi.Depends",
            "fastapi.Request", "fastapi.responses.HTMLResponse", ".clock", "auth.require_auth", "core.templating",
        })
        self.assertFalse(any(isinstance(node, ast.Import) for node in ast.walk(tree)))


if __name__ == "__main__":
    unittest.main()