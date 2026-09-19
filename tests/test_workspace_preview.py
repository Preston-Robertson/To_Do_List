"""Preview sessions and opt-in synthetic task actions remain isolated."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from luigi_web import auth
from scripts.preview_workspace import configure_preview_security


class WorkspacePreviewLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "synthetic-preview-session"}))
        self.app = FastAPI()

        @self.app.middleware("http")
        async def csrf_check(request: Request, call_next):
            if request.method == "PUT" and request.cookies.get(auth.COOKIE_NAME) and not auth.csrf_matches(
                request.cookies.get(auth.CSRF_COOKIE_NAME), request.headers.get("x-csrf-token")
            ):
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
            return await call_next(request)

        @self.app.get("/home", dependencies=[Depends(auth.require_auth)])
        @self.app.get("/home/preview", dependencies=[Depends(auth.require_auth)])
        def home():
            return {"preview": True}

        @self.app.put("/home/layout", dependencies=[Depends(auth.require_auth)])
        def layout():
            return {"saved": True}

        configure_preview_security(self.app, "synthetic-preview-session")
        self.client = TestClient(self.app, base_url="http://127.0.0.1:58306", follow_redirects=False)

    def test_login_submission_enters_demo_without_real_credentials(self) -> None:
        response = self.client.post("/login", data={"token": "synthetic-unused-input"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/home")
        self.assertEqual(self.client.get("/home").status_code, 200)
        self.assertNotIn("synthetic-unused-input", response.text)

    def test_fresh_deep_links_and_login_page_need_no_password(self) -> None:
        response = self.client.get("/home/preview")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertEqual(self.client.get("/login").headers["location"], "/home")

    def test_unrelated_session_cookie_does_not_replace_preview_session(self) -> None:
        self.client.cookies.set(auth.COOKIE_NAME, "synthetic-other-preview")
        response = self.client.get("/home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.cookies.get(auth.COOKIE_NAME), "synthetic-other-preview")
        self.assertIsNotNone(self.client.cookies.get("luigi_preview_session_58306"))

    def test_expired_preview_session_is_renewed_on_page_load(self) -> None:
        self.client.cookies.set("luigi_preview_session_58306", "synthetic-old-session", domain="127.0.0.1", path="/")
        self.assertEqual(self.client.get("/home").status_code, 200)
        self.assertEqual(self.client.cookies.get("luigi_preview_session_58306"), "synthetic-preview-session")

    def test_no_cookie_mutation_cannot_bootstrap_access(self) -> None:
        self.assertEqual(self.client.put("/home/layout", json={}).status_code, 401)

    def test_existing_preview_writes_still_require_csrf(self) -> None:
        self.client.get("/home")
        self.assertEqual(self.client.put("/home/layout", json={}).status_code, 403)
        response = self.client.put("/home/layout", json={}, headers={"X-CSRF-Token": self.client.cookies.get(auth.CSRF_COOKIE_NAME)})
        self.assertEqual(response.status_code, 200)

    def test_task_and_deployment_actions_remain_blocked(self) -> None:
        self.client.get("/home")
        for path in ("/tasks/example/complete", "/admin/update", "/cards/mtg/refresh", "/chat"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path).status_code, 403)

    def test_untrusted_host_and_cross_origin_login_are_rejected(self) -> None:
        self.assertEqual(self.client.get("/", headers={"Host": "example.invalid"}).status_code, 403)
        self.assertEqual(self.client.post("/login", headers={"Origin": "https://example.invalid"}).status_code, 403)

    def test_signout_does_not_clear_other_app_sessions(self) -> None:
        self.client.get("/home")
        self.client.cookies.set(auth.COOKIE_NAME, "synthetic-other-app")
        response = self.client.post("/logout")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.client.cookies.get(auth.COOKIE_NAME), "synthetic-other-app")
        self.assertIsNone(self.client.cookies.get("luigi_preview_session_58306"))


class WorkspacePreviewActionSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "synthetic-preview-session"}))
        app = FastAPI()

        @app.middleware("http")
        async def csrf_check(request: Request, call_next):
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not auth.csrf_matches(
                request.cookies.get(auth.CSRF_COOKIE_NAME), request.headers.get("x-csrf-token")
            ):
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
            return await call_next(request)

        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"], dependencies=[Depends(auth.require_auth)])
        def action(path: str):
            return {"path": path}

        configure_preview_security(app, "synthetic-preview-session", enable_task_actions=True)
        self.client = TestClient(app, base_url="http://127.0.0.1:58306", follow_redirects=False)
        self.addCleanup(self.client.close)

    def test_allowed_actions_require_preview_session_and_csrf(self) -> None:
        paths = ("/home/today", "/home/reschedule", "/tasks", "/tasks/preview-task-1",
                 "/tasks/preview-task-1/complete", "/tasks/quick", "/tasks/preview-task-1/status",
                 "/recurring", "/recurring/preview-recurring-1", "/recurring/preview-recurring-1/status",
                 "/recurring/preview-recurring-1/complete", "/discipline/preview-habit/today", "/undo/synthetic-opaque")
        for path in paths:
            self.assertEqual(self.client.post(path).status_code, 401)
            self.assertEqual(len(self.client.cookies), 0)
        self.client.get("/home")
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path).status_code, 403)
                response = self.client.post(path, headers={"X-CSRF-Token": self.client.cookies[auth.CSRF_COOKIE_NAME]})
                self.assertEqual(response.status_code, 200)

    def test_route_allowlist_is_exact_and_post_only(self) -> None:
        self.client.get("/home")
        headers = {"X-CSRF-Token": self.client.cookies[auth.CSRF_COOKIE_NAME]}
        for path in ("/tasks/bulk", "/tasks/quick/extra", "/tasks/example/complete", "/tasks/preview-task-1/delete",
                 "/tasks/preview-task-1/archive", "/tasks/example/status", "/home/today/extra",
                 "/recurring/unknown-id", "/recurring/preview-task-1/complete", "/discipline/preview-habit/delete",
                     "/undo/example/extra", "/admin/update", "/gnw", "/feedback", "/chat", "/chat/send",
                     "/cards/mtg/refresh"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, headers=headers).status_code, 403)
        for method in ("PUT", "PATCH", "DELETE"):
            self.assertEqual(self.client.request(method, "/tasks/preview-task-1", headers=headers).status_code, 403)

    def test_normal_cookie_and_bearer_cannot_replace_scoped_session(self) -> None:
        self.client.cookies.set(auth.COOKIE_NAME, "synthetic-preview-session")
        response = self.client.post("/tasks", headers={"Authorization": "Bearer synthetic-preview-session"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("set-cookie", response.headers)

    def test_cross_origin_mutation_is_rejected_with_valid_session(self) -> None:
        self.client.get("/home")
        response = self.client.post("/home/today", headers={
            "X-CSRF-Token": self.client.cookies[auth.CSRF_COOKIE_NAME], "Origin": "http://localhost:58306",
        })
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()