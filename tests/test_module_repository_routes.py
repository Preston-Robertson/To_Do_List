from __future__ import annotations

import builtins
import hashlib
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import asynccontextmanager
from importlib import import_module
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import Mock, patch

import luigi_web
from fastapi import FastAPI
from fastapi.routing import APIRoute
from luigi_web.core.static_assets import ModuleStaticFiles
from fastapi.testclient import TestClient
from starlette.requests import Request

from luigi_web import auth
from luigi_web.core import module_installer as installer
from luigi_web.core import module_repositories as repositories
from luigi_web.core import repository_routes
from luigi_web.core.module_registry import Module, build_registry
from luigi_web.core.templating import create_templates
from luigi_web.paths import STATIC_DIR

environment = import_module("luigi_web.modules.admin.environment")
admin_routes = import_module("luigi_web.modules.admin.routes")

BASE = "/modules/repositories"
TOKEN = "synthetic-repository-test-token"
SOURCE = {"id": "example", "module_id": "example", "repository": "https://github.com/example-owner/example-module"}
INSTALL = {"source_id": "example", "tag": "v1.0", "asset": "luigi_web_example-1.0-py3-none-any.whl", "sha256": "a" * 64}


def synthetic_app():
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    with patch("luigi_web.core.module_registry.discover_modules", return_value=()):
        application.state.registry = application.state.modules = build_registry("none")
    application.include_router(repository_routes.router)
    application.mount("/static", ModuleStaticFiles(directory=str(STATIC_DIR)), name="static")
    return application


def stage_synthetic_release(version="1.0"):
    from test_module_installer import build_wheel

    content = build_wheel(version=version)
    sha256 = hashlib.sha256(content).hexdigest()
    job = repositories.queue_install("example", "v" + version, f"luigi_web_example-{version}-py3-none-any.whl", sha256, True)
    with patch.object(installer, "_download", side_effect=lambda job, path: path.write_bytes(content)):
        installer.apply_job(job["id"])
    return sha256


def create_preview_app():
    application = synthetic_app()

    @asynccontextmanager
    async def lifespan(app):
        with tempfile.TemporaryDirectory(prefix="repository-preview-") as temporary:
            clean_environment = {key: os.environ[key] for key in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR") if key in os.environ}
            clean_environment.update({key: temporary for key in ("TEMP", "TMP", "SQLITE_TMPDIR", "APPDATA", "LOCALAPPDATA")})
            clean_environment.update({"LUIGI_WEB_UI_TOKEN": TOKEN, "LUIGI_WEB_MODULE_REPOSITORY_OWNERS": "example-owner", "LUIGI_WEB_MODULE_REPOSITORIES_FILE": ""})
            with patch.dict(os.environ, clean_environment, clear=True), patch.object(repositories, "DATA_DIR", Path(temporary)), patch("importlib.metadata.EntryPoint.load", side_effect=AssertionError("No downloaded code")):
                repositories.register_repository(SOURCE, trust_confirmed=True)
                stage_synthetic_release("0.9")
                stage_synthetic_release("1.0")
                repositories.queue_install(**INSTALL, trust_confirmed=True)
                yield

    @application.middleware("http")
    async def synthetic_session(request: Request, call_next):
        headers = [(key, value) for key, value in request.scope["headers"] if key != b"cookie"]
        headers.append((b"cookie", f"{auth.COOKIE_NAME}={TOKEN}; {auth.CSRF_COOKIE_NAME}=synthetic-csrf".encode()))
        request.scope["headers"] = headers
        response = await call_next(request)
        response.set_cookie(auth.CSRF_COOKIE_NAME, "synthetic-csrf", samesite="strict")
        return response

    application.router.lifespan_context = lifespan
    return application


class RepositoryRoutesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.object(repositories, "DATA_DIR", self.root))
        self.enterContext(patch.dict(os.environ, {
            "LUIGI_WEB_UI_TOKEN": TOKEN, "LUIGI_WEB_MODULE_REPOSITORY_OWNERS": "example-owner",
            "LUIGI_WEB_MODULE_REPOSITORIES_FILE": "", "LUIGI_WEB_MODULES": "none",
        }))
        self.enterContext(patch("socket.getaddrinfo", side_effect=AssertionError("Network is forbidden")))
        original_connect = socket.socket.connect

        def local_socketpair_only(connection, address):
            caller = sys._getframe(1)
            if (caller.f_code.co_filename == socket.__file__
                    and caller.f_code.co_name in {"socketpair", "_socketpair", "_fallback_socketpair"}
                    and address[0] in {"127.0.0.1", "::1"}):
                return original_connect(connection, address)
            raise AssertionError("Network is forbidden")

        self.enterContext(patch.object(socket.socket, "connect", local_socketpair_only))
        self.enterContext(patch.object(socket.socket, "connect_ex", side_effect=AssertionError("Network is forbidden")))
        self.enterContext(patch("importlib.metadata.EntryPoint.load", side_effect=AssertionError("No entry point loading")))
        self.app = synthetic_app()
        self.client = self.enterContext(TestClient(self.app, follow_redirects=False))
        self.client.cookies.set(auth.COOKIE_NAME, TOKEN)
        self.client.cookies.set(auth.CSRF_COOKIE_NAME, "synthetic-csrf")

    def post(self, path, values, **kwargs):
        return self.client.post(BASE + path, data={"csrf_token": "synthetic-csrf", "trust_confirmed": "yes", **values}, **kwargs)

    def register(self):
        return repositories.register_repository(SOURCE, trust_confirmed=True)

    def test_independent_auth_without_admin(self):
        self.client.cookies.clear()
        with patch.object(repositories, "repository_policy") as policy, patch.object(repositories, "queue_install") as queue:
            self.assertEqual(self.client.get(BASE).status_code, 401)
            self.assertEqual(self.client.get(BASE + "/setup").status_code, 401)
            response = self.client.get(BASE, headers={"Accept": "text/html"})
            self.assertEqual((response.status_code, response.headers["location"]), (303, "/login"))
            for path, values in (("/sources", SOURCE), ("/install", INSTALL), ("/rollback", {"source_id": "example", "sha256": "a" * 64})):
                self.assertEqual(self.post(path, values).status_code, 401)
            policy.assert_not_called()
            queue.assert_not_called()
        response = self.client.get(BASE, headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.app.state.registry.enabled, ())

    def test_cookie_requires_csrf_and_valid_bearer_remains_supported(self):
        self.register()
        with patch.object(repositories, "queue_install") as queue, patch.object(repositories, "repository_policy") as policy:
            for csrf in ("", "wrong"):
                self.assertEqual(self.post("/install", {**INSTALL, "csrf_token": csrf}).status_code, 403)
            self.assertEqual(self.post("/install", {**INSTALL, "csrf_token": ""}, headers={"Authorization": "Bearer incorrect"}).status_code, 403)
            policy.assert_not_called()
            queue.assert_not_called()
        response = self.post("/install", {**INSTALL, "csrf_token": ""}, headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(repositories.list_jobs()), 1)

    def test_header_csrf_and_no_origin_form_csrf(self):
        self.register()
        self.assertEqual(self.post("/install", INSTALL).status_code, 200)
        self.assertEqual(self.post("/install", {**INSTALL, "csrf_token": ""}, headers={"X-CSRF-Token": "synthetic-csrf", "Origin": "http://testserver"}).status_code, 200)

    def test_query_auth_cannot_mutate(self):
        self.client.cookies.clear()
        with patch.object(repositories, "register_repository") as register:
            response = self.post("/sources?token=" + TOKEN, SOURCE)
            self.assertEqual(response.status_code, 403)
            register.assert_not_called()

    def test_cross_origin_opaque_and_malformed_origins_block_before_backend(self):
        origins = ("https://other.example", "null", "", "http://testserver.evil", "http://testserver/path", "http://user@testserver", "http://testserver:invalid", "http://testserver#fragment", "http://testserver?query", "http://testserver\\other")
        with patch.object(repositories, "repository_policy") as policy, patch.object(repositories, "register_repository") as register:
            for origin in origins:
                with self.subTest(origin=origin):
                    response = self.post("/sources", SOURCE, headers={"Origin": origin, "Authorization": "Bearer " + TOKEN})
                    self.assertEqual(response.status_code, 403)
            policy.assert_not_called()
            register.assert_not_called()

    def test_trust_required_on_every_mutation(self):
        with patch.object(repositories, "repository_policy") as policy:
            for path, values in (("/sources", SOURCE), ("/install", INSTALL), ("/rollback", INSTALL)):
                for trust in ("", "true", "1", "no"):
                    self.assertEqual(self.post(path, {**values, "trust_confirmed": trust}).status_code, 422)
            policy.assert_not_called()

    def test_forms_are_bounded_and_reject_extra_fields(self):
        with patch.object(repositories, "repository_policy") as policy:
            self.assertEqual(self.post("/sources", {**SOURCE, "repository": "x" * 16385}).status_code, 413)
            self.assertEqual(self.post("/sources", {str(number): "x" for number in range(21)}).status_code, 422)
            for key in ("package", "prefix", "directory", "url"):
                self.assertEqual(self.post("/sources", {**SOURCE, key: "arbitrary"}).status_code, 422)
            self.assertEqual(self.client.post(BASE + "/sources", json=SOURCE).status_code, 415)
            duplicate = urlencode({**SOURCE, "trust_confirmed": "yes", "csrf_token": "synthetic-csrf"}) + "&id=other"
            self.assertEqual(self.client.post(BASE + "/sources", content=duplicate, headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code, 422)
            oversized = (b"x" * 8192 for _ in range(3))
            self.assertEqual(self.client.post(BASE + "/sources", content=oversized, headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code, 413)
            policy.assert_not_called()

    def test_disabled_policy_is_read_only(self):
        with patch.dict(os.environ, {"LUIGI_WEB_MODULE_REPOSITORY_OWNERS": ""}), patch.object(repositories, "register_repository") as register:
            response = self.client.get(BASE)
            self.assertIn("Read-only: repository installation is disabled", response.text)
            self.assertNotIn("data-repository-form", response.text)
            self.assertIn(BASE + "/setup", response.text)
            self.assertEqual(self.post("/sources", SOURCE).status_code, 403)
            register.assert_not_called()
        self.assertFalse((self.root / "module-installations.db").exists())

    def test_owner_allowlist_and_automatic_namespace(self):
        with patch.object(repositories, "register_repository", wraps=repositories.register_repository) as register:
            rejected = self.post("/sources", {**SOURCE, "repository": "https://github.com/unapproved-owner/repo"})
            self.assertEqual(rejected.status_code, 422)
            register.assert_not_called()
            self.assertEqual(self.post("/sources", SOURCE).status_code, 200)
        source = repositories.approved_source("example")
        self.assertEqual(source["package"], "luigi_web_extensions.example")
        self.assertEqual(source["distribution"], "luigi-web-example")
        self.assertIn("No code was installed", self.post("/sources", SOURCE).text)

    def test_invalid_source_fields_do_not_reach_registration(self):
        with patch.object(repositories, "register_repository") as register:
            for change in ({"id": "../escape"}, {"module_id": "core/path"}, {"repository": "https://user:example@github.com/example-owner/repo"}, {"repository": "https://example.invalid/repo"}, {"distribution": "pip"}):
                self.assertEqual(self.post("/sources", {**SOURCE, **change}).status_code, 422)
            register.assert_not_called()

    def test_fixed_sources_allow_install_without_allowing_registration(self):
        normalized = repositories._source(SOURCE)
        with patch.object(repositories, "_policy", return_value=[normalized]), patch.dict(os.environ, {"LUIGI_WEB_MODULE_REPOSITORY_OWNERS": ""}):
            response = self.client.get(BASE)
            self.assertIn("Deployment policy", response.text)
            self.assertIn("Registration is read-only", response.text)
            self.assertEqual(self.post("/sources", SOURCE).status_code, 403)
            self.assertEqual(self.post("/install", INSTALL).status_code, 200)

    def test_queue_only_no_execution_or_network_and_selection_unchanged(self):
        self.register()
        with patch.object(installer, "apply_job") as worker, patch.object(installer, "apply_pending") as pending, patch("subprocess.Popen") as process, patch("subprocess.run") as run:
            response = self.post("/install", INSTALL)
            self.assertEqual(response.status_code, 200)
            worker.assert_not_called()
            pending.assert_not_called()
            process.assert_not_called()
            run.assert_not_called()
        self.assertIn('data-job-state="pending"', response.text)
        self.assertIn("Waiting for the deployment worker", response.text)
        self.assertEqual(repositories.list_jobs()[0]["state"], "pending")
        self.assertEqual(os.environ["LUIGI_WEB_MODULES"], "none")
        self.assertEqual(self.app.state.registry.enabled, ())
        self.assertFalse((self.root / "module-packages").exists())
        self.assertNotIn("luigi_web_extensions.example", sys.modules)

    def test_invalid_install_fields_never_queue(self):
        self.register()
        with patch.object(repositories, "queue_install") as queue:
            for change in ({"source_id": "missing"}, {"source_id": "../escape"}, {"sha256": "A" * 64}, {"sha256": "short"}, {"tag": "../tag"}, {"asset": "https://example.invalid/file.whl"}, {"asset": "other-1.0-py3-none-any.whl"}, {"asset": "luigi_web_example-1.0-cp311-cp311-win_amd64.whl"}):
                self.assertEqual(self.post("/install", {**INSTALL, **change}).status_code, 422)
            queue.assert_not_called()
        self.assertEqual(repositories.list_jobs(), [])

    def test_rejected_request_does_not_invent_disabled_policy(self):
        self.register()
        response = self.post("/install", {**INSTALL, "sha256": "invalid"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("Refresh repository state", response.text)
        self.assertNotIn("installation is disabled", response.text)

    def test_job_status_changes_only_on_explicit_get(self):
        self.register()
        self.post("/install", INSTALL)
        job_id = repositories.list_jobs()[0]["id"]
        for state in ("installing", "installed", "failed"):
            with repositories._database(write=True) as connection:
                connection.execute("UPDATE jobs SET state = ?, error = ? WHERE id = ?", (state, "synthetic-internal-error" if state == "failed" else "", job_id))
            response = self.client.get(BASE)
            self.assertIn(f'data-job-state="{state}"', response.text)
            self.assertNotIn("synthetic-internal-error", response.text)

    def seed_release(self, sha256="b" * 64, version="0.9"):
        source = repositories.approved_source("example")
        record = {"source": source, "module_id": "example", "sha256": sha256, "tag": "v" + version,
                  "asset": f"luigi_web_example-{version}-py3-none-any.whl", "version": version,
                  "site_path": "module-packages/example/" + sha256, "private_extra": "synthetic-private-marker"}
        with repositories._database(write=True) as connection:
            connection.execute("INSERT INTO releases(module_id, sha256, record) VALUES (?, ?, ?)", ("example", sha256, json.dumps(record)))
        return record

    def test_releases_api_is_read_only_and_path_free(self):
        self.register()
        self.seed_release()
        with patch.object(installer, "_verify_release", side_effect=AssertionError("No archive scan for choices")):
            releases = repositories.list_releases("example")
        self.assertEqual(set(releases[0]), {"source_id", "module_id", "sha256", "tag", "asset", "version", "selected_for_restart"})
        self.assertEqual(releases[0]["version"], "0.9")
        self.assertFalse(releases[0]["selected_for_restart"])
        self.assertNotIn("site_path", str(releases))
        self.assertEqual(repositories.list_jobs(), [])

    def test_rollback_uses_recorded_choice_and_queues_without_worker(self):
        self.register()
        self.seed_release()
        response = self.client.get(BASE)
        self.assertIn('value="' + "b" * 64 + '"', response.text)
        with patch.object(installer, "apply_job") as worker, patch.object(repositories, "queue_rollback", wraps=repositories.queue_rollback) as rollback:
            response = self.post("/rollback", {"source_id": "example", "sha256": "b" * 64})
            rollback.assert_called_once_with("example", "b" * 64, trust_confirmed=True)
            worker.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Rollback request saved to the queue", response.text)
        self.assertEqual(repositories.list_jobs()[0]["tag"], "v0.9")

    def test_invalid_or_unrecorded_rollback_never_queues(self):
        self.register()
        with patch.object(repositories, "queue_rollback") as rollback:
            for sha256 in ("invalid", "c" * 64):
                self.assertEqual(self.post("/rollback", {"source_id": "example", "sha256": sha256}).status_code, 422)
            rollback.assert_not_called()

    def test_verified_candidate_is_separate_from_running_registry(self):
        self.register()
        record = self.seed_release()
        module = Module("example", "Example", "Synthetic", "", source="External package")
        with patch("luigi_web.core.module_registry.discover_modules", return_value=(module,)):
            self.app.state.registry = build_registry("example")
        with patch.object(repositories, "installed_modules", return_value=[record]) as verified:
            response = self.client.get(BASE)
            verified.assert_called_once_with()
        self.assertIn("0.9 (verified)", response.text)
        self.assertIn("Running registry</dt><dd>Enabled", response.text)
        self.assertIn("Restart is required", response.text)
        self.assertNotIn("synthetic-private-marker", response.text)
        self.assertNotIn("module-packages/", response.text)

    def test_failures_never_expose_details_or_claim_success(self):
        self.register()
        for failure in (repositories.ModuleInstallError("synthetic-sensitive-marker"), RuntimeError("synthetic-sensitive-marker")):
            with patch.object(repositories, "queue_install", side_effect=failure):
                response = self.post("/install", INSTALL)
                self.assertIn(response.status_code, (422, 503))
                self.assertNotIn("synthetic-sensitive-marker", response.text)
                self.assertNotIn("request saved", response.text)
        with patch.object(repositories, "installed_modules", side_effect=RuntimeError("synthetic-sensitive-marker")):
            response = self.client.get(BASE)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("synthetic-sensitive-marker", response.text)
            self.assertNotIn("(verified)", response.text)

    def test_queue_readback_failure_does_not_claim_success(self):
        self.register()
        with patch.object(repositories, "get_job", side_effect=repositories.ModuleInstallError("synthetic-readback-failure")):
            response = self.post("/install", INSTALL)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("request saved", response.text)
        self.assertNotIn("synthetic-readback-failure", response.text)

    def test_setup_assets_headers_and_templates(self):
        response = self.client.get(BASE)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertNotIn(str(self.root), response.text)
        self.assertEqual(self.client.get(BASE + "/setup").status_code, 200)
        for asset in ("css/module-repositories.css", "js/module-repositories.js", "icons/lucide/rotate-ccw.svg", "icons/lucide/check.svg", "icons/lucide/blocks.svg", "icons/lucide/lock-keyhole.svg", "icons/lucide/download.svg", "icons/lucide/plus.svg"):
            self.assertEqual(self.client.get("/static/" + asset).status_code, 200)
        script = self.client.get("/static/js/module-repositories.js").text
        for forbidden in ("setInterval", "setTimeout", "location.reload", "console.log"):
            self.assertNotIn(forbidden, script)
        self.assertIn("data-repository-refresh", response.text)
        templates = create_templates()
        for name in ("module_repositories.html", "modules.html", "admin.html"):
            templates.env.get_template(name)
            if name != "module_repositories.html":
                self.assertIn(BASE, templates.env.loader.get_source(templates.env, name)[0])

    def test_real_verified_candidate_and_tampering_fail_closed(self):
        self.register()
        sha256 = stage_synthetic_release()
        response = self.client.get(BASE)
        self.assertEqual(response.status_code, 200)
        self.assertIn("1.0 (verified)", response.text)
        self.assertIn("Running registry</dt><dd>Not loaded", response.text)
        self.assertEqual(repositories.list_releases("example")[0]["selected_for_restart"], True)
        package = self.root / "module-packages" / "example" / sha256 / "luigi_web_extensions" / "example" / "manifest.py"
        package.write_text("raise RuntimeError('synthetic-tamper')\n", encoding="utf-8")
        response = self.client.get(BASE)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("(verified)", response.text)
        self.assertNotIn("synthetic-tamper", response.text)

    def test_readback_may_already_be_claimed_without_claiming_installation(self):
        self.register()
        original = repositories.get_job

        def claimed(job_id):
            with repositories._database(write=True) as connection:
                connection.execute("UPDATE jobs SET state = 'installing' WHERE id = ?", (job_id,))
            return original(job_id)

        with patch.object(repositories, "get_job", side_effect=claimed):
            response = self.post("/install", INSTALL)
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-job-state="installing"', response.text)
        self.assertNotIn("Installation completed", response.text)

    def test_old_installed_job_is_not_presented_as_current_candidate(self):
        self.register()
        stage_synthetic_release("0.9")
        stage_synthetic_release("1.0")
        response = self.client.get(BASE)
        self.assertEqual(response.status_code, 200)
        self.assertIn("1.0 (verified)", response.text)
        self.assertNotIn("0.9 (verified)", response.text)
        self.assertNotIn("Candidate selected", response.text)
        self.assertEqual(response.text.count("Installation completed."), 2)

    def test_policy_keys_are_protected_and_not_editable(self):
        policy_keys = {"LUIGI_WEB_MODULE_REPOSITORY_OWNERS", "LUIGI_WEB_MODULE_REPOSITORIES_FILE"}
        self.assertTrue(policy_keys.issubset(environment.PROTECTED_KEYS))
        self.assertTrue(policy_keys.isdisjoint(spec.name for spec in environment.KNOWN_KEYS))
        path = self.root / "synthetic.env"
        path.write_text("SYNTHETIC_UNRELATED=unchanged\n", encoding="utf-8")
        for key in policy_keys:
            with self.assertRaises(environment.EnvUpdateError):
                environment.update_env_file(path, {key: "synthetic-value"})
        self.assertEqual(path.read_text(encoding="utf-8"), "SYNTHETIC_UNRELATED=unchanged\n")

    def test_routes_are_unique(self):
        signatures = [(route.path, method) for route in self.app.routes if isinstance(route, APIRoute) for method in route.methods]
        self.assertEqual(len(signatures), len(set(signatures)))
        self.assertEqual({path for path, method in signatures}, {BASE, BASE + "/setup", BASE + "/sources", BASE + "/install", BASE + "/rollback"})


class AdminOnlyDiagnosticsTests(unittest.TestCase):
    def test_diagnostics_do_not_import_sqlalchemy_or_touch_disabled_modules(self):
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "sqlalchemy" or name.startswith("sqlalchemy."):
                raise AssertionError("Disabled database dependency was imported")
            return original_import(name, *args, **kwargs)

        templates = Mock()
        host = SimpleNamespace(
            _module_enabled=lambda module_id, request: module_id == "admin",
            _integration_result=lambda name, check: {"name": name, "ok": True},
            templates=templates,
        )
        request = Request({"type": "http", "method": "GET", "path": "/admin/integrations", "headers": [], "app": FastAPI()})
        with patch.dict(sys.modules, {"luigi_web.application": host}), patch.object(
            luigi_web, "application", host, create=True
        ), patch("builtins.__import__", side_effect=guarded_import):
            admin_routes.admin_integrations(request)
        context = templates.TemplateResponse.call_args.args[1]
        self.assertEqual([check["name"] for check in context["checks"]], ["Git checkout", "Environment file"])


if __name__ == "__main__":
    unittest.main()