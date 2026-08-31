"""Regression checks for the Admin-managed environment boundary."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from luigi_web import application, auth
from luigi_web import env_file


class EnvFileSecurityTests(unittest.TestCase):
    def test_auth_tokens_are_not_admin_managed(self) -> None:
        managed_names = {spec.name for spec in env_file.KNOWN_KEYS}
        self.assertTrue(env_file.PROTECTED_KEYS.isdisjoint(managed_names))

        rendered_names = {
            entry["spec"].name
            for group in env_file.grouped_view({})
            for entry in group["entries"]
        }
        self.assertTrue(env_file.PROTECTED_KEYS.isdisjoint(rendered_names))

    def test_managed_writer_rejects_auth_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "luigi.env"
            path.write_text("LUIGI_WEB_PORT=8080\n", encoding="utf-8")

            for key in env_file.PROTECTED_KEYS:
                with self.subTest(key=key):
                    with self.assertRaises(env_file.EnvUpdateError):
                        env_file.update_env_file(path, {key: "replacement"})

            env_file.update_env_file(path, {"LUIGI_WEB_PORT": "8081"})
            self.assertEqual(env_file.read_env_file(path)["LUIGI_WEB_PORT"], "8081")

    def test_admin_env_save_hides_unexpected_file_error_details(self) -> None:
        client = TestClient(application.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")
        client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        with (
            patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "main-secret"}),
            patch.object(env_file, "read_env_file", return_value={}),
            patch.object(
                env_file,
                "update_env_file",
                side_effect=PermissionError("denied: /synthetic/private.env"),
            ),
            self.assertLogs("luigi_web.app", level="ERROR"),
        ):
            response = client.post(
                "/admin/env",
                data={"LUIGI_WEB_PORT": "8081"},
                headers={"X-CSRF-Token": "csrf-value"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Could not update the environment file", response.text)
        self.assertNotIn("PermissionError", response.text)
        self.assertNotIn("/synthetic/private.env", response.text)


if __name__ == "__main__":
    unittest.main()