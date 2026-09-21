"""Offline tests for the autonomous maintainer security boundary."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from luigi_web import feedback, maintainer
from luigi_web.maintainer_agent import (
    AgentOutcome,
    PermissionRequiredError,
    SafeWorkspace,
    WorkspacePolicyError,
    system_message,
)
from luigi_web.maintainer_worker import WorkerConfig, WorktreeContext, run_once
from luigi_web.maintainer_worker import _askpass_path


class MaintainerQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "LUIGI_WEB_FEEDBACK_DB": os.path.join(self.temp_dir.name, "feedback.db"),
            "LUIGI_WEB_MAINTAINER_DB": os.path.join(self.temp_dir.name, "maintainer.db"),
        })
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp_dir.cleanup()

    def _enqueue(self, message: str) -> str:
        feedback_uuid = feedback.create_item({"category": "Idea", "message": message})
        return maintainer.enqueue_feedback(feedback.get_item(feedback_uuid), "Tests pass.")

    def test_claims_one_job_at_a_time_and_records_terminal_state(self) -> None:
        first_uuid = self._enqueue("First synthetic request")
        second_uuid = self._enqueue("Second synthetic request")

        claimed = maintainer.claim_next_job()
        self.assertEqual(claimed["uuid"], first_uuid)
        self.assertEqual(claimed["attempt_count"], 1)
        self.assertEqual(maintainer.claim_next_job()["uuid"], second_uuid)
        self.assertIsNone(maintainer.claim_next_job())

        maintainer.set_run_context(
            first_uuid, branch_name="automation/first", base_commit="abc123",
        )
        maintainer.finish_job(
            first_uuid, status="Draft PR", summary="Synthetic change",
            head_commit="def456", pr_url="https://example.test/pull/1",
        )
        finished = maintainer.get_job(first_uuid)
        self.assertEqual(finished["status"], "Draft PR")
        self.assertEqual(finished["base_commit"], "abc123")
        self.assertEqual(finished["head_commit"], "def456")

    def test_terminal_state_rejects_unsafe_pull_request_url(self) -> None:
        row_uuid = self._enqueue("Synthetic unsafe link request")
        maintainer.claim_next_job()
        with self.assertRaisesRegex(ValueError, "credential-free HTTPS"):
            maintainer.finish_job(
                row_uuid, status="Draft PR", summary="Synthetic result",
                pr_url="javascript:alert(1)",
            )
        self.assertEqual(maintainer.get_job(row_uuid)["status"], "Running")


class SafeWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "luigi_web").mkdir()
        (self.root / "luigi_web" / "example.py").write_text(
            "VALUE = 1\n", encoding="utf-8",
        )
        (self.root / "luigi_web" / "auth.py").write_text(
            "PROTECTED = True\n", encoding="utf-8",
        )
        self.workspace = SafeWorkspace(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_allows_bounded_edit_and_rejects_escape(self) -> None:
        result = self.workspace.replace_text(
            "luigi_web/example.py", "VALUE = 1", "VALUE = 2",
        )
        self.assertEqual(result, "updated luigi_web/example.py")
        with self.assertRaises(WorkspacePolicyError):
            self.workspace.read_file("../outside.txt")
        with self.assertRaises(WorkspacePolicyError):
            self.workspace.create_file("data/copied.txt", "private")

    def test_protected_file_requires_human_attention(self) -> None:
        with self.assertRaises(PermissionRequiredError):
            self.workspace.replace_text(
                "luigi_web/auth.py", "PROTECTED = True", "PROTECTED = False",
            )
        self.assertEqual(
            self.workspace.permission_requests,
            ["Human approval is required to change luigi_web/auth.py."],
        )

    @unittest.skipIf(os.name == "nt", "Windows symlink creation needs elevation")
    def test_internal_symlink_cannot_alias_a_protected_file(self) -> None:
        (self.root / "luigi_web" / "alias.py").symlink_to(
            self.root / "luigi_web" / "auth.py",
        )
        with self.assertRaisesRegex(WorkspacePolicyError, "symbolic-link"):
            self.workspace.replace_text(
                "luigi_web/alias.py", "PROTECTED = True", "PROTECTED = False",
            )

    def test_static_check_parses_without_executing_python(self) -> None:
        (self.root / "luigi_web" / "example.py").write_text(
            "raise RuntimeError('must not execute')\n", encoding="utf-8",
        )
        result = self.workspace.check_syntax(["luigi_web/example.py"])
        self.assertEqual(result["luigi_web/example.py"], "Python parsed")

    def test_installed_feedback_instructions_do_not_depend_on_local_notes(self) -> None:
        content = system_message()
        self.assertIn("Feedback Agent Operating Instructions", content)
        self.assertIn("Never read raw Feedback", content)
        self.assertIn("PostgreSQL backup copy", content)
        self.assertIn("separately approves release", content)
        with self.assertRaises(PermissionRequiredError):
            self.workspace.create_file(
                "module-repos/feedback/src/luigi_web/modules/feedback/maintainer-policy.md",
                "Replacement instructions",
            )

    def test_extracted_security_surfaces_cannot_be_changed(self) -> None:
        for path in (
            "luigi_web/core/module_installer.py",
            "module-repos/feedback/src/luigi_web/modules/feedback/release.py",
            "module-repos/finance/src/luigi_web/modules/finance/routes.py",
            "module-repos/tasks/src/luigi_web/modules/tasks/repository.py",
            "module-repos/media/pyproject.toml",
            "module-repos/tasks/.github/workflows/release.yml",
            "luigi_web/modules/__init__.py",
            "luigi_web/__init__.py",
            "requirements-modules.txt",
            "module-repos/tasks/.gitattributes",
            "examples/maintainer-review/common.conf",
        ):
            with self.subTest(path=path), self.assertRaises(PermissionRequiredError):
                self.workspace.create_file(path, "Example protected content")
        for path in (".env.example", "LOCAL_NOTES.md", "temp/example.txt", "examples/maintainer-review/publish.env.example"):
            with self.subTest(path=path), self.assertRaises(WorkspacePolicyError):
                self.workspace.read_file(path)


class MaintainerWorkerFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.env = patch.dict(os.environ, {
            "LUIGI_WEB_FEEDBACK_DB": str(self.root / "feedback.db"),
            "LUIGI_WEB_MAINTAINER_DB": str(self.root / "maintainer.db"),
        })
        self.env.start()
        feedback_uuid = feedback.create_item({
            "category": "Bug", "message": "Synthetic worker request",
        })
        self.job_uuid = maintainer.enqueue_feedback(
            feedback.get_item(feedback_uuid), "Synthetic tests pass.",
        )
        self.config = WorkerConfig(
            state_root=self.root,
            repository_url="https://example.test/example/luigi-web.git",
        )
        self.worktree = self.root / "worktrees" / self.job_uuid
        self.worktree.mkdir(parents=True)
        self.context = WorktreeContext(
            self.worktree, "automation/feedback-example", "a" * 40,
        )

    def tearDown(self) -> None:
        self.env.stop()
        self.temp_dir.cleanup()

    def test_ready_change_publishes_draft_and_records_metadata(self) -> None:
        policy = Mock()
        validator = Mock()
        publisher = Mock(return_value=("b" * 40, "https://example.test/pull/7"))
        cleaner = Mock()
        notifier = Mock(return_value=True)
        result = run_once(
            self.config,
            agent_runner=Mock(return_value=AgentOutcome("ready", "Focused fix.")),
            preparer=Mock(return_value=self.context),
            change_reader=Mock(return_value=["luigi_web/example.py"]),
            policy_checker=policy, validator=validator, publisher=publisher,
            cleaner=cleaner, notifier=notifier,
        )

        self.assertEqual(result.status, "Draft PR")
        self.assertTrue(result.notification_sent)
        job = maintainer.get_job(self.job_uuid)
        self.assertEqual(job["head_commit"], "b" * 40)
        self.assertEqual(job["pr_url"], "https://example.test/pull/7")
        policy.assert_called_once()
        validator.assert_called_once()
        publisher.assert_called_once()
        cleaner.assert_called_once()

    def test_agent_attention_request_never_reaches_publish(self) -> None:
        publisher = Mock()
        change_reader = Mock()
        result = run_once(
            self.config,
            agent_runner=Mock(return_value=AgentOutcome(
                "needs_attention", "Protected boundary.",
                "Approve an authentication policy change?",
            )),
            preparer=Mock(return_value=self.context),
            change_reader=change_reader, publisher=publisher,
            cleaner=Mock(), notifier=Mock(return_value=False),
        )

        self.assertEqual(result.status, "Needs attention")
        job = maintainer.get_job(self.job_uuid)
        self.assertEqual(
            job["attention_question"],
            "Approve an authentication policy change?",
        )
        change_reader.assert_not_called()
        publisher.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "askpass helper is Linux-only")
    def test_askpass_permissions_are_repaired_on_every_use(self) -> None:
        path = _askpass_path(self.config)
        path.chmod(0o644)
        self.assertEqual(_askpass_path(self.config), path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
