"""Stable updates exercised against disposable local Git repositories only."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from luigi_web.modules.admin.updates import update_from_main


@unittest.skipUnless(shutil.which("git"), "Git is required for isolated branch tests")
class StableUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.checkout = self.root / "checkout"
        self.source.mkdir()
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith(("GIT_", "LUIGI_"))}
        self.environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                                GIT_AUTHOR_NAME="Example Developer", GIT_AUTHOR_EMAIL="example@example.invalid",
                                GIT_COMMITTER_NAME="Example Developer", GIT_COMMITTER_EMAIL="example@example.invalid",
                                GIT_TERMINAL_PROMPT="0")
        self.git(self.source, "init", "-b", "main")
        self.commit(self.source, "baseline")
        self.baseline = self.git(self.source, "rev-parse", "HEAD")
        self.git(self.root, "clone", "--no-hardlinks", str(self.source), str(self.checkout))

    def git(self, directory, *arguments):
        result = subprocess.run(["git", "-c", f"core.hooksPath={os.devnull}", *arguments],
                                cwd=directory, env=self.environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, directory, value):
        (directory / "example.txt").write_text(value + "\n", encoding="utf-8")
        self.git(directory, "add", "example.txt")
        self.git(directory, "commit", "-m", value)

    def test_only_main_is_selected_and_previous_commit_is_retained(self):
        self.commit(self.source, "stable change")
        stable = self.git(self.source, "rev-parse", "HEAD")
        self.git(self.source, "switch", "-c", "testing")
        self.commit(self.source, "unreleased testing change")
        self.git(self.checkout, "switch", "-c", "testing")
        ok, steps = update_from_main(self.checkout, self.environment)
        self.assertTrue(ok, steps)
        self.assertEqual(self.git(self.checkout, "branch", "--show-current"), "main")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), stable)
        self.assertEqual(self.git(self.checkout, "rev-parse", "--abbrev-ref", "@{upstream}"), "origin/main")
        checkpoints = self.git(self.checkout, "for-each-ref", "--format=%(objectname)", "refs/heads/rollback/")
        self.assertEqual(checkpoints, self.baseline)
        self.assertEqual((self.checkout / "example.txt").read_text().strip(), "stable change")

    def test_dirty_checkout_is_preserved(self):
        (self.checkout / "example.txt").write_text("local edits", encoding="utf-8")
        ok, steps = update_from_main(self.checkout, self.environment)
        self.assertFalse(ok)
        self.assertIn("clean", steps[-1]["out"])
        self.assertEqual((self.checkout / "example.txt").read_text(), "local edits")

    def test_testing_only_commits_are_not_reset_or_merged(self):
        self.git(self.checkout, "switch", "-c", "testing")
        self.commit(self.checkout, "local testing")
        before = self.git(self.checkout, "rev-parse", "HEAD")
        self.commit(self.source, "stable change")
        ok, steps = update_from_main(self.checkout, self.environment)
        self.assertFalse(ok)
        self.assertIn("diverge", steps[-1]["out"])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), before)
        self.assertEqual(self.git(self.checkout, "branch", "--show-current"), "testing")

    def test_detached_rollback_is_not_advanced_automatically(self):
        self.git(self.checkout, "switch", "--detach", self.baseline)
        self.assertFalse(update_from_main(self.checkout, self.environment)[0])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.baseline)

    def test_existing_divergent_main_is_not_discarded(self):
        self.commit(self.checkout, "local main change")
        local_main = self.git(self.checkout, "rev-parse", "HEAD")
        self.git(self.checkout, "switch", "-c", "testing", self.baseline)
        self.commit(self.source, "stable change")
        self.assertFalse(update_from_main(self.checkout, self.environment)[0])
        self.assertEqual(self.git(self.checkout, "rev-parse", "main"), local_main)

    def test_missing_main_never_falls_back_to_testing(self):
        self.git(self.source, "branch", "-m", "testing")
        ok, steps = update_from_main(self.checkout, self.environment)
        self.assertFalse(ok)
        self.assertIn("No other branch", steps[-1]["out"])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.baseline)

    def test_admin_route_uses_stable_helper_and_stops_dependencies_on_failure(self):
        from luigi_web import application as host
        from luigi_web.modules.admin import routes
        from starlette.requests import Request

        request = Request({"type": "http", "method": "POST", "path": "/admin/update", "headers": []})
        with patch.object(host, "REPO_DIR", self.checkout), patch(
            "luigi_web.modules.admin.updates.update_from_main", return_value=(False, [])
        ) as update, patch.object(host, "_run") as install, patch.object(
            host.templates, "TemplateResponse", side_effect=lambda template, context: context
        ), patch.object(host, "_git_head_short", return_value="example"), patch.object(
            host, "_git_status_line", return_value="Synthetic commit"
        ):
            response = routes.admin_update(request)
        self.assertFalse(response["ok"])
        update.assert_called_once()
        self.assertEqual(update.call_args.args[0], self.checkout)
        install.assert_not_called()
