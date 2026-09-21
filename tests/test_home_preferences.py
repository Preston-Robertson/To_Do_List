from __future__ import annotations

import json
import os
from pathlib import Path
from importlib.resources import files as module_files
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from luigi_web.core.static_assets import ModuleStaticFiles
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request

from luigi_web.modules.planning import home_preferences as preferences
from luigi_web.modules.planning.preference_routes import router


def render_synthetic_home(media: bool = True) -> str:
    package = Path(__file__).resolve().parents[1] / "luigi_web"
    templates = Environment(
        loader=FileSystemLoader([
            Path(str(module_files("luigi_web.modules.planning"))) / "templates",
            package / "core" / "templates",
        ]),
        autoescape=select_autoescape(),
    )
    context = dict.fromkeys((
        "overdue_tasks", "upcoming_tasks", "open_tasks", "disciplines_pending",
        "discipline_streaks", "follow_ups", "recent_completions", "disciplines_at_risk",
        "gnw_playing", "gnw_watching", "disc_week", "task_week", "recent_activity",
    ), [])
    context.update({
        "page_title": "Home", "active_nav": "home", "landing_path": "/home",
        "navigation_groups": [], "module_count": 3, "module_enabled": lambda name: False,
        "asset_version": "synthetic", "shell_asset_version": "synthetic",
        "week_of": "2026-09-14", "today_iso": "2026-09-17", "gnw_enabled": media,
        "weekly_review": {
            "start_iso": "2026-09-10", "end_iso": "2026-09-16", "completed_total": 0,
            "discipline_total": 0, "discipline_days": 0, "carried_over": 0,
            "upcoming_next_week": 0, "top_categories": [],
        },
        "open_tasks": [{"uuid": "example-task", "task": "Example task", "priority": 1,
                        "status": "Not Started", "catagory": "Example category"}],
        "home_date": "Thursday, September 17, 2026",
        "home_state": {
            "today": "2026-09-17", "tasks": [], "habits": [],
            "shortcuts": [{"label": "Games", "href": "/games", "icon": "gamepad-2"}] if media else [],
            "selection_available": True, "selection_error": None,
        },
    })
    return templates.get_template("home.html").render(**context)


def synthetic_home_app() -> FastAPI:
    from luigi_web.application import csrf_middleware

    package = Path(__file__).resolve().parents[1] / "luigi_web"
    app = FastAPI()
    app.middleware("http")(csrf_middleware)
    app.include_router(router)
    app.mount("/static", ModuleStaticFiles(directory=package / "core" / "static"))
    app.mount("/module-assets/planning", StaticFiles(directory=Path(str(module_files("luigi_web.modules.planning"))) / "static"))

    @app.get("/home", response_class=HTMLResponse)
    def home(media: bool = True):
        response = HTMLResponse(render_synthetic_home(media))
        response.set_cookie("luigi_session", "synthetic-home-test", httponly=True, samesite="strict")
        return response

    return app


class HomePreferenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "settings"
        self.path = self.root / "home-layout.json"
        self.data_patch = patch.object(preferences.paths, "DATA_DIR", self.root)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)

    def test_missing_is_not_saved_and_does_not_create_directory(self):
        self.assertIsNone(preferences.load())
        self.assertFalse(self.root.exists())
        self.assertEqual(len(preferences.defaults()["order"]), 13)

    def test_round_trip_retains_unrendered_widgets(self):
        layout = preferences.defaults()
        layout.update(hidden=["gnw-playing"], pinned=["gnw-watching"])
        layout["order"].reverse()
        self.assertEqual(preferences.save(layout), layout)
        self.assertEqual(preferences.load(), layout)
        self.assertEqual(json.loads(self.path.read_bytes()), layout)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_validation_rejects_unknown_duplicate_and_record_fields(self):
        for field, value in (
            ("version", True), ("version", 2), ("order", ["unknown"]),
            ("hidden", ["overdue", "overdue"]), ("pinned", ["unknown"]),
            ("hidden", "overdue"), ("pinned", [{}]), ("record", "Example task"),
        ):
            with self.subTest(field=field, value=value):
                layout = preferences.defaults()
                layout[field] = value
                with self.assertRaises(preferences.LayoutValidationError):
                    preferences.save(layout)
        self.assertFalse(self.root.exists())

    def test_decode_rejects_malformed_duplicate_properties_and_oversized_input(self):
        for raw in (b"{", b"null", b"[]", b"\xff", b" " * 4097,
                    b'{"version":1,"version":1,"order":[],"hidden":[],"pinned":[]}'):
            with self.subTest(raw=raw[:50]):
                with self.assertRaises(preferences.LayoutValidationError):
                    preferences.decode(raw)

    def test_partial_order_gets_fixed_defaults_without_losing_preferences(self):
        layout = preferences.defaults()
        layout.update(order=["activity"], hidden=["gnw-playing"])
        saved = preferences.save(layout)
        self.assertEqual(saved["order"][0], "activity")
        self.assertEqual(set(saved["order"]), set(preferences.WIDGET_IDS))
        self.assertEqual(saved["hidden"], ["gnw-playing"])

    def test_invalid_save_preserves_previous_bytes(self):
        preferences.save(preferences.defaults())
        previous = self.path.read_bytes()
        with self.assertRaises(preferences.LayoutValidationError):
            preferences.save({"version": 1})
        self.assertEqual(self.path.read_bytes(), previous)

    def test_replace_and_fsync_failure_preserve_previous_bytes(self):
        preferences.save(preferences.defaults())
        previous = self.path.read_bytes()
        changed = preferences.defaults()
        changed["hidden"] = ["overdue"]
        for operation in ("replace", "fsync"):
            with self.subTest(operation=operation):
                with patch.object(preferences.os, operation, side_effect=OSError("private path")):
                    with self.assertRaisesRegex(preferences.LayoutStorageError, "^Home layout is unavailable.$"):
                        preferences.save(changed)
                self.assertEqual(self.path.read_bytes(), previous)
                self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_post_commit_verification_failure_rolls_back(self):
        preferences.save(preferences.defaults())
        previous = self.path.read_bytes()
        changed = preferences.defaults()
        changed["pinned"] = ["activity"]
        read_bytes = preferences._read_bytes
        destination_reads = 0

        def fail_verification(path):
            nonlocal destination_reads
            if path == self.path:
                destination_reads += 1
                if destination_reads == 2:
                    return b"{}"
            return read_bytes(path)

        with patch.object(preferences, "_read_bytes", side_effect=fail_verification):
            with self.assertRaises(preferences.LayoutStorageError):
                preferences.save(changed)
        self.assertEqual(self.path.read_bytes(), previous)

    def test_corrupt_and_oversized_stored_files_fail_closed(self):
        self.root.mkdir()
        for raw in (b"{}", b" " * 4097):
            self.path.write_bytes(raw)
            with self.assertRaises(preferences.LayoutStorageError):
                preferences.load()
            with self.assertRaises(preferences.LayoutStorageError):
                preferences.save(preferences.defaults())
            self.assertEqual(self.path.read_bytes(), raw)

    def test_read_failure_is_generic(self):
        with patch.object(Path, "open", side_effect=PermissionError("private path")):
            with self.assertRaisesRegex(preferences.LayoutStorageError, "^Home layout is unavailable.$"):
                preferences.load()

    def test_new_file_is_removed_when_verification_fails(self):
        read_bytes = preferences._read_bytes
        destination_reads = 0

        def fail_verification(path):
            nonlocal destination_reads
            if path == self.path:
                destination_reads += 1
                if destination_reads == 2:
                    return None
            return read_bytes(path)

        with patch.object(preferences, "_read_bytes", side_effect=fail_verification):
            with self.assertRaises(preferences.LayoutStorageError):
                preferences.save(preferences.defaults())
        self.assertFalse(self.path.exists())


class HomePreferenceRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "settings"
        self.data_patch = patch.object(preferences.paths, "DATA_DIR", self.root)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.auth_patch = patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "synthetic-home-test"})
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer synthetic-home-test"}

    def test_routes_require_auth_without_file_access(self):
        with patch.object(preferences, "load") as load, patch.object(preferences, "save") as save:
            self.assertEqual(self.client.get("/home/layout").status_code, 401)
            self.assertEqual(self.client.put("/home/layout", json=preferences.defaults()).status_code, 401)
            load.assert_not_called()
            save.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_missing_and_explicit_defaults_are_distinct_and_no_store(self):
        response = self.client.get("/home/layout", headers=self.headers)
        self.assertEqual(response.json(), {"saved": False, "layout": None})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertFalse(self.root.exists())
        saved = self.client.put("/home/layout", headers=self.headers, json=preferences.defaults())
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()["saved"])
        self.assertEqual(saved.headers["cache-control"], "no-store")
        loaded = self.client.get("/home/layout", headers=self.headers)
        self.assertEqual(loaded.json(), saved.json())

    def test_malformed_unknown_duplicate_payloads_do_not_write(self):
        bodies = [b"{", b"[]", b'{"version":1,"version":1}']
        for field, value in (("hidden", ["unknown"]), ("order", ["overdue", "overdue"]),
                             ("pinned", ["activity", "activity"]), ("version", True)):
            layout = preferences.defaults()
            layout[field] = value
            bodies.append(json.dumps(layout).encode())
        for body in bodies:
            with self.subTest(body=body):
                response = self.client.put("/home/layout", content=body,
                                           headers={**self.headers, "Content-Type": "application/json"})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"], "Invalid Home layout.")
        self.assertFalse(self.root.exists())

    def test_size_limit_also_applies_to_streamed_body(self):
        for content in (b" " * 4097, iter([b" " * 2048, b" " * 2049])):
            response = self.client.put("/home/layout", content=content,
                                       headers={**self.headers, "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 413)
        self.assertFalse(self.root.exists())

    def test_wrong_content_type_rejected(self):
        response = self.client.put("/home/layout", content="{}", headers=self.headers)
        self.assertEqual(response.status_code, 415)
        self.assertFalse(self.root.exists())

    def test_io_errors_are_generic_and_do_not_claim_saved(self):
        with patch.object(Path, "open", side_effect=PermissionError("private path")):
            response = self.client.get("/home/layout", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Home layout is unavailable."})
        with patch.object(preferences.os, "replace", side_effect=PermissionError("private path")):
            response = self.client.put("/home/layout", headers=self.headers, json=preferences.defaults())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Home layout could not be saved."})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertFalse((self.root / "home-layout.json").exists())


class HomePreferenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        environment = patch.dict(os.environ, {
            "LUIGI_WEB_UI_TOKEN": "synthetic-home-test", "LUIGI_WEB_DATA_DIR": str(self.root),
            "LUIGI_WEB_MODULES_FILE": str(self.root / "modules.json"), "PYTHON_DOTENV_DISABLED": "1",
        })
        environment.start()
        self.addCleanup(environment.stop)
        data_patch = patch.object(preferences.paths, "DATA_DIR", self.root)
        data_patch.start()
        self.addCleanup(data_patch.stop)

    def test_templates_compile_with_conditional_media_and_no_inline_assistant(self):
        for media in (True, False):
            with self.subTest(media=media):
                html = render_synthetic_home(media)
                self.assertEqual(html.count('class="widget" data-widget='), 5)
                self.assertIn('data-home-preferences="/home/layout"', html)
                self.assertIn('data-home-data="/home/data"', html)
                self.assertEqual('href="/games"' in html, media)
                self.assertNotIn('class="widget-toggle"', html)
                self.assertNotIn('id="chat-panel"', html)
                self.assertIn('home-layout.js?v=synthetic', html)

    def test_global_csrf_protects_cookie_saves_and_bearer_still_works(self):
        with TestClient(synthetic_home_app()) as client:
            response = client.get("/home")
            self.assertEqual(response.status_code, 200)
            self.assertFalse((self.root / "home-layout.json").exists())
            response = client.put("/home/layout", json=preferences.defaults())
            self.assertEqual(response.status_code, 403)
            response = client.put("/home/layout", json=preferences.defaults(),
                                  headers={"X-CSRF-Token": client.cookies["luigi_csrf"]})
            self.assertEqual(response.status_code, 200)
            response = client.put("/home/layout", json=preferences.defaults(),
                                  headers={"Authorization": "Bearer synthetic-home-test"})
            self.assertEqual(response.status_code, 200)

    def test_home_assets_and_mount_have_no_preference_io(self):
        with patch.object(preferences, "load") as load, patch.object(preferences, "save") as save:
            with TestClient(synthetic_home_app()) as client:
                for url in ("/home", "/module-assets/planning/home-layout.js", "/module-assets/planning/home-layout.css"):
                    self.assertEqual(client.get(url).status_code, 200)
            load.assert_not_called()
            save.assert_not_called()

    def test_real_host_mounts_planning_assets_and_preference_router(self):
        from luigi_web import application as host

        client = TestClient(host.app)
        self.addCleanup(client.close)
        headers = {"Authorization": "Bearer synthetic-home-test"}
        with patch.object(preferences, "load", return_value=None) as load:
            for url in ("/module-assets/planning/home-layout.js", "/module-assets/planning/home-layout.css"):
                self.assertEqual(client.get(url, headers=headers).status_code, 200)
            load.assert_not_called()
            response = client.get("/home/layout", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"saved": False, "layout": None})
            load.assert_called_once_with()

    def test_home_uses_live_state_without_assistant_or_preference_io(self):
        from luigi_web import application as host
        from luigi_web.modules.planning import routes

        request = Request({"type": "http", "method": "GET", "path": "/home", "headers": [], "app": host.app})
        templates = Mock()
        state = {"today": "2026-09-17", "tasks": [], "habits": [], "shortcuts": [],
                 "selection_available": True, "selection_error": None}
        with (
            patch.object(host, "templates", templates),
            patch.object(routes, "get_home_state", return_value=state) as load_state,
            patch.object(preferences, "load") as load,
        ):
            routes.home_page(request)
        context = templates.TemplateResponse.call_args.args[1]
        self.assertFalse(any(key.startswith("chat_") for key in context))
        self.assertIs(context["home_state"], state)
        self.assertEqual(context["today_iso"], "2026-09-17")
        load_state.assert_called_once_with(request)
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()