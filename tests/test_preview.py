"""Offline tests for isolated Preview deployment boundaries."""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from luigi_web import application, auth, env_file, preview


class PreviewSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "LUIGI_WEB_UI_TOKEN": "main-secret",
            "LUIGI_WEB_DEPLOY_TOKEN": "deploy-secret",
        })
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()

    def test_deployment_settings_are_not_admin_managed(self) -> None:
        managed = {item.name for item in env_file.KNOWN_KEYS}
        self.assertNotIn("LUIGI_WEB_DEPLOY_TOKEN", managed)
        self.assertNotIn("LUIGI_WEB_PREVIEW_HELPER", managed)
        self.assertIn("LUIGI_WEB_DEPLOY_TOKEN", env_file.PROTECTED_KEYS)

    def test_deploy_unlock_uses_derived_short_session(self) -> None:
        response = auth.deploy_unlock_response("deploy-secret")
        cookie = response.headers["set-cookie"]
        self.assertNotIn("deploy-secret", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Max-Age=3600", cookie)
        self.assertTrue(auth.is_deploy_authenticated(auth._deploy_session_value()))

    def test_branch_validation_rejects_option_and_traversal_inputs(self) -> None:
        for value in ("-danger", "feature/../main", "feature\\branch", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    preview.validate_branch(value)
        self.assertEqual(preview.validate_branch("feature/calendar-v2"), "feature/calendar-v2")

    def test_helper_adapter_parses_metadata_only_json(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"ok": True, "branches": ["main", "feature/test"]}),
            stderr="",
        )
        with (
            patch.object(preview, "helper_available", return_value=True),
            patch.object(preview, "helper_path", return_value=Path("preview-helper")),
            patch("subprocess.run", return_value=completed) as runner,
        ):
            branches = preview.branches()
        self.assertEqual(branches, ["main", "feature/test"])
        command = runner.call_args.args[0]
        self.assertEqual(command[-1], "branches")
        self.assertNotIn("LUIGI_WEB_UI_TOKEN", runner.call_args.kwargs["env"])

    def test_main_session_cannot_mutate_preview_without_deploy_unlock(self) -> None:
        client = TestClient(application.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")
        client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        with patch.object(preview, "mutate") as mutate:
            response = client.post(
                "/admin/preview/restart",
                headers={"X-CSRF-Token": "csrf-value"},
            )
        self.assertEqual(response.status_code, 403)
        mutate.assert_not_called()

    def test_htmx_unlock_sets_deploy_cookie_and_redirects(self) -> None:
        client = TestClient(application.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")
        client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        response = client.post(
            "/admin/preview/unlock",
            data={"token": "deploy-secret"},
            headers={"X-CSRF-Token": "csrf-value", "HX-Request": "true"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["HX-Redirect"], "/admin/preview")
        self.assertNotIn("deploy-secret", response.headers["set-cookie"])

    def test_unlocked_preview_mutation_uses_allowlisted_adapter(self) -> None:
        client = TestClient(application.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")
        client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        client.cookies.set(auth.DEPLOY_COOKIE_NAME, auth._deploy_session_value())
        state = {
            "configured": True, "ok": True, "exists": True,
            "branch": "feature/test", "commit": "abc1234",
            "service_active": True, "healthy": True, "port": 8081,
        }
        with (
            patch.object(preview, "mutate", return_value=state) as mutate,
            patch.object(preview, "status", return_value=state),
            patch.object(preview, "helper_available", return_value=True),
            patch.object(preview, "branches", return_value=["feature/test"]),
        ):
            response = client.post(
                "/admin/preview/restart",
                headers={"X-CSRF-Token": "csrf-value"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Preview restart completed", response.text)
        mutate.assert_called_once_with("restart", branch="")


if __name__ == "__main__":
    unittest.main()