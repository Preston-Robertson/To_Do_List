"""Synthetic, offline release-controller contracts; no remote mutations."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from luigi_web import feedback, maintainer
from luigi_web.modules.feedback import release, review


BASE = "a" * 40
HEAD = "b" * 40
MERGE = "c" * 40
PATCH = b"diff --git a/example.py b/example.py\n--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
REAL_API = release._api


class FakeGitHub:
    def __init__(self, run):
        self.calls = []
        self.merged = False
        self.tag = None
        self.tag_error = False
        self.merge_error = False
        self.overrides = {}
        self.run = run

    def __call__(self, config, endpoint, *, releasing=False, method="GET", fields=None, missing=False):
        self.calls.append((endpoint, method, fields, releasing))
        if endpoint in self.overrides:
            result = self.overrides[endpoint]
            if isinstance(result, Exception):
                raise result
            return copy.deepcopy(result)
        if endpoint == "pulls/7/merge":
            self.merged = True
            if self.merge_error:
                raise release.ReleaseError("Synthetic uncertain merge")
            return {"merged": True, "sha": MERGE}
        if endpoint == "pulls/7":
            repository = {"full_name": "example/luigi-web"}
            return {
                "number": 7, "html_url": "https://github.com/example/luigi-web/pull/7",
                "state": "closed" if self.merged else "open", "merged": self.merged,
                "head": {"ref": self.run["branch"], "sha": HEAD, "repo": repository},
                "base": {"ref": "main", "sha": BASE, "repo": repository},
                "draft": False, "mergeable": True, "mergeable_state": "clean",
                "merge_commit_sha": MERGE if self.merged else None,
            }
        if endpoint == "branches/main":
            return {"name": "main", "protected": True, "commit": {"sha": MERGE if self.merged else BASE}}
        if endpoint == "branches/main/protection":
            return {
                "enforce_admins": {"enabled": True},
                "required_status_checks": {"strict": True, "contexts": ["offline-regression"], "checks": []},
                "required_pull_request_reviews": None,
            }
        if endpoint.startswith(f"commits/{HEAD}/check-runs?"):
            return {"total_count": 1, "check_runs": [{
                "name": "offline-regression", "head_sha": HEAD, "status": "completed",
                "conclusion": "success", "app": {"id": 15368},
            }]}
        if endpoint.startswith(f"commits/{HEAD}/status?"):
            return {"total_count": 0, "statuses": [], "state": "pending", "sha": HEAD}
        if endpoint == f"git/commits/{MERGE}":
            return {"sha": MERGE, "parents": [{"sha": BASE}, {"sha": HEAD}]}
        if endpoint.startswith("git/ref/tags/"):
            return copy.deepcopy(self.tag)
        if endpoint == "git/refs":
            if self.tag_error:
                raise release.ReleaseError("Synthetic tag error")
            self.tag = {"ref": fields["ref"], "object": {"type": "commit", "sha": fields["sha"]}}
            return copy.deepcopy(self.tag)
        raise AssertionError("Unexpected fixed endpoint: " + endpoint)


class FakeGit:
    def __init__(self, run, path):
        self.run = run
        self.path = path
        self.calls = []
        self.committed = False
        self.pushed = False
        self.patch = PATCH
        self.remote_base = BASE
        self.remote_branch = None
        self.local_head = BASE
        self.config_text = "core.repositoryformatversion\n0\0"

    def __call__(self, config, arguments, *, cwd=None, auth=False, check=True, label=""):
        self.calls.append((arguments, auth))
        arguments = arguments[4:]
        output = ""
        if arguments[0] == "config":
            output = self.config_text
        elif arguments == ["rev-parse", "--path-format=absolute", "--git-common-dir"]:
            output = (config.repository_root / ".git").as_posix()
        elif arguments == ["rev-parse", "--show-toplevel"]:
            output = self.path.as_posix()
        elif arguments == ["rev-parse", "HEAD"]:
            output = HEAD if self.committed else self.local_head
        elif arguments[0] == "merge-base":
            output = BASE
        elif arguments[0] == "symbolic-ref":
            output = self.run["branch"]
        elif arguments[0] == "ls-remote":
            output = f"{self.remote_base}\trefs/heads/main\n"
            branch_head = HEAD if self.pushed else self.remote_branch
            if branch_head:
                output += f"{branch_head}\trefs/heads/{self.run['branch']}\n"
        elif arguments[0] == "diff":
            destination = next((argument[9:] for argument in arguments if argument.startswith("--output=")), None)
            if destination:
                Path(destination).write_bytes(self.patch)
        elif "commit" in arguments:
            self.committed = True
        elif arguments[0] == "rev-list":
            output = f"{HEAD} {BASE}"
        elif arguments[0] == "push":
            self.pushed = True
        elif arguments[0] != "add":
            raise AssertionError("Unexpected Git operation")
        return subprocess.CompletedProcess(arguments, 0, output, "")


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("luigi_web.modules.feedback.test_preview.verify_ready", return_value=True))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = {key: value for key, value in os.environ.items() if key.upper() in {
            "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT", "COMSPEC",
        }}
        environment.update({
            "LUIGI_WEB_FEEDBACK_DB": str(self.root / "feedback.db"),
            "LUIGI_WEB_MAINTAINER_DB": str(self.root / "maintainer.db"),
            "HOME": str(self.root), "USERPROFILE": str(self.root),
            "TEMP": str(self.root), "TMP": str(self.root),
            "APPDATA": str(self.root), "LOCALAPPDATA": str(self.root),
            "LUIGI_MAINTAINER_REQUIRED_CHECKS": "offline-regression",
            "LUIGI_MAINTAINER_GITHUB_TOKEN": "synthetic-publisher-token",
            "LUIGI_RELEASE_GITHUB_TOKEN": "synthetic-release-token",
            "LUIGI_MAINTAINER_COPILOT_TOKEN": "synthetic-agent-token",
            "LUIGI_MAINTAINER_SMTP_PASSWORD": "synthetic-mail-password",
        })
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        self.config = release.worker.WorkerConfig(
            self.root, "https://github.com/example/luigi-web.git",
        )
        feedback_id = feedback.create_item({"category": "Idea", "message": "Synthetic change"})
        job_id = maintainer.enqueue_feedback(feedback.get_item(feedback_id), "Synthetic validation")
        maintainer.claim_next_job()
        self.run = review.create_run(job_id, BASE, options_requested=2)
        results = [{
            "command_id": command, "passed": True, "total": 1, "failed": 0,
            "skipped": 0, "output_digest": "d" * 64,
        } for command in sorted(review.REQUIRED_CHECKS)]
        self.option = review.add_option(
            self.run["id"], label="Synthetic option", summary="Synthetic summary",
            diff_sha256=hashlib.sha256(PATCH).hexdigest(), artifact_id=str(uuid.uuid4()), test_results=results,
        )
        review.add_option(
            self.run["id"], label="Alternative", summary="Synthetic alternative",
            diff_sha256="f" * 64, artifact_id=str(uuid.uuid4()), test_results=results,
        )
        self.run = review.finish_generation(self.run["id"])
        review.approve_option(self.run["id"], self.option["id"], self.run["revision"], "1.01")
        self.run = review.claim_publish()
        self.api = FakeGitHub(self.run)
        self.api_mock = self.enterContext(patch.object(release, "_api", side_effect=self.api))
        self.command = self.enterContext(patch.object(release.worker, "_run", side_effect=AssertionError("Unexpected command")))
        self.worktree = self.config.worktrees_root / uuid.UUID(self.run["id"]).hex
        self.worktree.mkdir(parents=True)

    def _testing(self):
        self.run = review.record_published(
            self.run["id"], HEAD, self.run["branch"], "https://github.com/example/luigi-web/pull/7", 7,
        )
        return self.run

    def releasing(self):
        self._testing()
        review.record_preview(self.run["id"], HEAD, review.preview_path(self.run["id"]))
        self.run = review.record_test_result(self.run["id"], HEAD, True)
        review.approve_release(self.run["id"], self.run["revision"], HEAD, "1.01", confirmed=True)
        self.run = review.claim_release()
        return self.run

    def prepare_publish(self):
        self.git = FakeGit(self.run, self.worktree)
        self.enterContext(patch.object(release.worker, "_git", side_effect=self.git))
        self.changed = self.enterContext(patch.object(release.worker, "changed_files", return_value=["example.py"]))
        self.policy = self.enterContext(patch.object(release.worker, "enforce_change_policy"))
        self.static = self.enterContext(patch.object(release.worker, "validate_static_changes"))
        self.command.side_effect = None
        self.command.return_value = subprocess.CompletedProcess([], 0, "https://github.com/example/luigi-web/pull/7\n", "")
        pull = self.api(self.config, "pulls/7")
        pull["draft"] = True
        self.api.overrides["pulls/7"] = pull

    def assert_no_mutations(self):
        self.assertFalse(any(method != "GET" for _, method, _, _ in self.api.calls))

    def test_actual_store_claim_and_literal_version(self):
        current, option = release._load(self.run, "publishing")
        self.assertEqual(current, self.run)
        self.assertEqual(option, self.option)
        self.assertEqual(current["expected_tag"], "v1.01")

    def test_stale_or_unclaimed_snapshot_rejected(self):
        for fields in ({"revision": 1}, {"state": "publish_queued"}, {"branch": "client-branch"}):
            with self.subTest(fields=fields), self.assertRaises(release.ReleaseError):
                release._load({**self.run, **fields}, "publishing")
        review.mark_attention(self.run["id"], "Synthetic stop")
        with self.assertRaises(release.ReleaseError):
            release._load(self.run, "publishing")

    def test_binding_rechecked_from_store(self):
        for fields in (
            {"diff_sha256": "0" * 64}, {"validation_digest": "0" * 64},
            {"tests_passed": False}, {"run_id": str(uuid.uuid4())},
        ):
            with self.subTest(fields=fields), patch.object(
                review, "get_option", return_value={**self.option, **fields},
            ), self.assertRaises(release.ReleaseError):
                release._load(self.run, "publishing")

    def test_only_fixed_github_configuration(self):
        self.assertEqual(release._repository(self.config), "example/luigi-web")
        for url in (
            "https://example.test/example/luigi-web.git",
            "https://example:synthetic@github.com/example/luigi-web.git",
            "https://github.com:443/example/luigi-web.git",
            "https://github.com/example/luigi-web.git?command=other",
        ):
            with self.subTest(url=url), self.assertRaises(release.ReleaseError):
                release._repository(release.worker.WorkerConfig(self.root, url))

    def test_publish_exact_patch_and_receipt(self):
        self.prepare_publish()
        receipt = release.publish_candidate(self.config, self.run, self.option, self.worktree)
        self.assertEqual(receipt, {
            "head_commit": HEAD, "branch": self.run["branch"],
            "pr_number": 7, "pr_url": "https://github.com/example/luigi-web/pull/7",
        })
        self.assertEqual(review.get_run(self.run["id"])["state"], "publishing")
        self.assertEqual(self.policy.call_count, 2)
        self.assertEqual(self.static.call_count, 2)
        commands = [arguments for arguments, _ in self.git.calls]
        self.assertTrue(any("--intent-to-add" in command for command in commands))
        push = next(command for command in commands if "push" in command)
        self.assertIn(f"--force-with-lease=refs/heads/{self.run['branch']}:", push)
        self.assertIn(f"{HEAD}:refs/heads/{self.run['branch']}", push)
        commit = next(command for command in commands if "commit" in command)
        self.assertEqual(commit[-1], f"Approved feedback {self.run['id']} (1.01)")
        command = self.command.call_args.args[0]
        self.assertIn("--draft", command)
        self.assertEqual(command[command.index("--label") + 1], "pending")

    def test_publish_rejects_patch_mismatch_before_commit(self):
        self.prepare_publish()
        self.git.patch = PATCH + b"changed\n"
        with self.assertRaisesRegex(release.ReleaseError, "raw Git diff"):
            release.publish_candidate(self.config, self.run, self.option, self.worktree)
        self.assertFalse(self.git.committed)
        self.assertFalse(self.git.pushed)

    def test_publish_rejects_history_changes(self):
        self.prepare_publish()
        self.git.local_head = HEAD
        with self.assertRaises(release.ReleaseError):
            release.publish_candidate(self.config, self.run, self.option, self.worktree)
        self.assertFalse(self.git.committed)

    def test_publish_never_rewrites_existing_remote_branch(self):
        self.prepare_publish()
        self.git.remote_branch = HEAD
        with self.assertRaises(release.ReleaseError):
            release.publish_candidate(self.config, self.run, self.option, self.worktree)
        self.assertFalse(self.git.pushed)
        self.assertFalse(self.git.committed)

    def test_publish_rejects_changed_main(self):
        self.prepare_publish()
        self.git.remote_base = HEAD
        with self.assertRaises(release.ReleaseError):
            release.publish_candidate(self.config, self.run, self.option, self.worktree)
        self.assertFalse(self.git.pushed)

    def test_publish_failure_after_push_is_explicit_partial(self):
        self.prepare_publish()
        self.command.side_effect = release.worker.WorkerError("Synthetic error")
        with self.assertRaises(release.PublishPartialError) as caught:
            release.publish_candidate(self.config, self.run, self.option, self.worktree)
        self.assertEqual(caught.exception.head_commit, HEAD)
        self.assertEqual(caught.exception.branch, self.run["branch"])
        self.assertEqual(sum("push" in command for command, _ in self.git.calls), 1)

    def test_publish_rejects_dependency_and_controller_paths(self):
        self.prepare_publish()
        for path in ("requirements-modules.txt", "pyproject.toml", "setup.py", "module-repos/feedback/src/worker.py"):
            self.changed.return_value = [path]
            with self.subTest(path=path), self.assertRaises(release.ReleaseError):
                release.publish_candidate(self.config, self.run, self.option, self.worktree)
        self.assertFalse(self.git.committed)

    def test_live_checks_return_controller_digest_not_preview(self):
        self._testing()
        receipt = release.verify_testing(self.config, self.run)
        self.assertEqual(receipt["head_commit"], HEAD)
        self.assertEqual(receipt["base_commit"], BASE)
        self.assertIs(receipt["checks_passed"], True)
        self.assertEqual(len(receipt["checks_digest"]), 64)
        self.assertNotIn("preview_commit", receipt)
        self.assert_no_mutations()

    def test_live_identity_mismatch_rejected(self):
        self._testing()
        for section, field, value in (
            ("head", "sha", BASE), ("head", "ref", "other"),
            ("head", "repo", {"full_name": "other/repo"}),
            ("base", "ref", "other"), ("base", "sha", HEAD),
            ("base", "repo", {"full_name": "other/repo"}),
        ):
            self.api.overrides.clear()
            pull = self.api(self.config, "pulls/7")
            pull[section][field] = value
            self.api.overrides["pulls/7"] = pull
            with self.subTest(field=field, section=section), self.assertRaises(release.ReleaseError):
                release.verify_testing(self.config, self.run)
        self.assert_no_mutations()

    def test_missing_failed_skipped_pending_or_wrong_sha_checks_rejected(self):
        self._testing()
        endpoint = f"commits/{HEAD}/check-runs?per_page=100&page=1&filter=latest"
        good = self.api(self.config, endpoint)
        cases = [{"total_count": 0, "check_runs": []}]
        for fields in (
            {"conclusion": "failure"}, {"conclusion": "skipped"}, {"conclusion": "neutral"},
            {"status": "in_progress"}, {"head_sha": BASE}, {"name": "not-required"},
        ):
            cases.append({"total_count": 1, "check_runs": [{**good["check_runs"][0], **fields}]})
        for result in cases:
            self.api.overrides[endpoint] = result
            with self.subTest(result=result), self.assertRaises(release.ReleaseError):
                release.verify_testing(self.config, self.run)
        self.assert_no_mutations()

    def test_combined_status_requires_success_and_exact_sha(self):
        self._testing()
        endpoint = f"commits/{HEAD}/status?per_page=100&page=1"
        for status in (
            {"total_count": 0, "statuses": [], "sha": BASE, "state": "pending"},
            {"total_count": 1, "statuses": [{"context": "external", "state": "failure"}], "sha": HEAD, "state": "failure"},
        ):
            self.api.overrides[endpoint] = status
            with self.subTest(status=status), self.assertRaises(release.ReleaseError):
                release.verify_testing(self.config, self.run)

    def test_protection_failures_and_unchanged_main_required(self):
        self._testing()
        good = self.api(self.config, "branches/main/protection")
        cases = [
            ("branches/main/protection", release.ReleaseError("Synthetic denied")),
            ("branches/main/protection", {**good, "required_status_checks": {"contexts": [], "strict": True}}),
            ("branches/main/protection", {**good, "enforce_admins": {"enabled": False}}),
            ("branches/main/protection", {**good, "required_status_checks": {"contexts": ["offline-regression"], "strict": False}}),
            ("branches/main", {"name": "main", "protected": False, "commit": {"sha": BASE}}),
            ("branches/main", {"name": "main", "protected": True, "commit": {"sha": HEAD}}),
        ]
        for endpoint, response in cases:
            self.api.overrides = {endpoint: response}
            with self.subTest(endpoint=endpoint), self.assertRaises(release.ReleaseError):
                release.verify_testing(self.config, self.run)
        self.assert_no_mutations()

    def test_full_bounded_check_pagination(self):
        self._testing()
        prefix = f"commits/{HEAD}/check-runs?per_page=100&page="
        good = self.api(self.config, prefix + "1&filter=latest")["check_runs"][0]
        self.api.overrides[prefix + "1&filter=latest"] = {
            "total_count": 101, "check_runs": [{**good, "name": f"other-{number}"} for number in range(100)],
        }
        self.api.overrides[prefix + "2&filter=latest"] = {"total_count": 101, "check_runs": [good]}
        self.assertTrue(release.verify_testing(self.config, self.run)["checks_passed"])
        self.api.overrides[prefix + "2&filter=latest"] = {"total_count": 101, "check_runs": []}
        with self.assertRaises(release.ReleaseError):
            release.verify_testing(self.config, self.run)

    def test_default_context_is_not_inferred_from_workflow_title(self):
        self._testing()
        del os.environ["LUIGI_MAINTAINER_REQUIRED_CHECKS"]
        self.assertEqual(release._required_checks(), ("offline-regression",))
        self.assertTrue(release.verify_testing(self.config, self.run)["checks_passed"])

    def test_expired_preview_blocks_merge_before_remote_mutation(self):
        self.releasing()
        with patch("luigi_web.modules.feedback.test_preview.verify_ready", side_effect=ValueError("Synthetic expired preview")):
            with self.assertRaises(release.ReleaseError):
                release.merge_release(self.config, self.run)
        self.assert_no_mutations()

    def test_merge_requires_claimed_exact_release_approval(self):
        self._testing()
        with self.assertRaises(release.ReleaseError):
            release.merge_release(self.config, self.run)
        self.assert_no_mutations()

    def test_merge_and_literal_tag_verified_without_store_mutation(self):
        self.releasing()
        self.assertEqual(release.merge_release(self.config, self.run), {"merge_commit": MERGE, "tag": "v1.01"})
        mutations = [(endpoint, method, fields) for endpoint, method, fields, _ in self.api.calls if method != "GET"]
        self.assertEqual(mutations, [
            ("pulls/7/merge", "PUT", {"sha": HEAD, "merge_method": "merge"}),
            ("git/refs", "POST", {"ref": "refs/tags/v1.01", "sha": MERGE}),
        ])
        self.assertTrue(all(releasing for _, _, _, releasing in self.api.calls))
        self.assertEqual(review.get_run(self.run["id"])["state"], "releasing")

    def test_existing_tag_prevents_merge(self):
        self.releasing()
        self.api.tag = {"ref": "refs/tags/v1.01", "object": {"type": "commit", "sha": BASE}}
        with self.assertRaisesRegex(release.ReleaseError, "already exists"):
            release.merge_release(self.config, self.run)
        self.assert_no_mutations()

    def test_unknown_mergeability_or_draft_blocks_release(self):
        self.releasing()
        good = self.api(self.config, "pulls/7")
        for fields in ({"mergeable": None}, {"mergeable": False}, {"draft": True}, {"mergeable_state": "blocked"}):
            self.api.overrides["pulls/7"] = {**good, **fields}
            with self.subTest(fields=fields), self.assertRaises(release.ReleaseError):
                release.merge_release(self.config, self.run)
        self.assert_no_mutations()

    def test_tag_failure_carries_verified_merge_and_can_be_reconciled(self):
        self.releasing()
        self.api.tag_error = True
        with self.assertRaises(release.ReleasePartialError) as caught:
            release.merge_release(self.config, self.run)
        self.assertEqual(caught.exception.merge_commit, MERGE)
        self.assertEqual(caught.exception.tag, "v1.01")
        self.run = review.mark_attention(self.run["id"], "Synthetic partial release")
        self.api.calls.clear()
        receipt = release.reconcile_release(self.config, self.run)
        self.assertEqual(receipt["status"], "merged_untagged")
        self.assertEqual(receipt["merge_commit"], MERGE)
        self.assert_no_mutations()

    def test_uncertain_merge_not_retried_and_reconciliation_is_read_only(self):
        self.releasing()
        self.api.merge_error = True
        with self.assertRaises(release.ReleasePartialError) as caught:
            release.merge_release(self.config, self.run)
        self.assertIsNone(caught.exception.merge_commit)
        self.run = review.mark_attention(self.run["id"], "Synthetic uncertainty")
        self.api.calls.clear()
        self.assertEqual(release.reconcile_release(self.config, self.run)["status"], "merged_untagged")
        self.assert_no_mutations()

    def test_reconcile_complete_or_conflicting_tag(self):
        self.releasing()
        release.merge_release(self.config, self.run)
        self.run = review.mark_attention(self.run["id"], "Synthetic interrupted receipt")
        self.api.calls.clear()
        self.assertEqual(release.reconcile_release(self.config, self.run)["status"], "released")
        self.api.tag["object"]["sha"] = BASE
        self.assertEqual(release.reconcile_release(self.config, self.run)["status"], "tag_conflict")
        self.assert_no_mutations()

    def test_role_credentials_are_separate_and_never_arguments(self):
        for releasing, expected in ((False, "synthetic-publisher-token"), (True, "synthetic-release-token")):
            environment = release._gh_env(self.config, releasing=releasing)
            self.assertEqual(environment["GH_TOKEN"], expected)
            self.assertNotIn("LUIGI_MAINTAINER_COPILOT_TOKEN", environment)
            self.assertNotIn("LUIGI_MAINTAINER_SMTP_PASSWORD", environment)
            self.assertNotIn("LUIGI_MAINTAINER_GITHUB_TOKEN", environment)
            self.assertNotIn("LUIGI_RELEASE_GITHUB_TOKEN", environment)
        del os.environ["LUIGI_RELEASE_GITHUB_TOKEN"]
        with self.assertRaises(release.ReleaseError):
            release._gh_env(self.config, releasing=True)

    def test_api_uses_fixed_host_argv_and_only_role_token(self):
        self.command.side_effect = None
        self.command.return_value = subprocess.CompletedProcess([], 0, 'HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n{"merged":true}', "")
        self.assertEqual(REAL_API(self.config, "pulls/7/merge", releasing=True, method="PUT", fields={
            "sha": HEAD, "merge_method": "merge",
        }), {"merged": True})
        command = self.command.call_args.args[0]
        arguments = self.command.call_args.kwargs
        self.assertEqual(command[:7], ["gh", "api", "--hostname", "github.com", "--include", "--method", "PUT"])
        self.assertIn("repos/example/luigi-web/pulls/7/merge", command)
        self.assertIn("sha=" + HEAD, command)
        self.assertNotIn("--admin", command)
        self.assertNotIn("--force", command)
        self.assertNotIn("shell", arguments)
        self.assertEqual(arguments["env"]["GH_TOKEN"], "synthetic-release-token")
        self.assertFalse(any("synthetic-" in argument for argument in command))

    def test_api_only_explicit_404_is_absence(self):
        self.command.side_effect = None
        for code in (401, 403, 409, 422, 500):
            self.command.return_value = subprocess.CompletedProcess([], 1, f'HTTP/2.0 {code} Error\n\n{{}}', 'sensitive-ignored')
            with self.subTest(code=code), self.assertRaisesRegex(release.ReleaseError, "unavailable") as caught:
                REAL_API(self.config, "git/ref/tags/v1.01", releasing=True, missing=True)
            self.assertNotIn("sensitive", str(caught.exception))
        self.command.return_value = subprocess.CompletedProcess([], 1, 'HTTP/2.0 404 Not Found\n\n{}', '')
        self.assertIsNone(REAL_API(self.config, "git/ref/tags/v1.01", releasing=True, missing=True))
        with self.assertRaises(release.ReleaseError):
            REAL_API(self.config, "branches/main/protection", releasing=True)

    def test_api_rejects_oversized_malformed_and_nonobject_output(self):
        self.command.side_effect = None
        for output in (
            "not-http", 'HTTP/2.0 200 OK\n\n[]', 'HTTP/2.0 200 OK\n\ninvalid',
            'HTTP/2.0 200 OK\n\n' + json.dumps({"message": "x" * release.MAX_API_BYTES}),
        ):
            self.command.return_value = subprocess.CompletedProcess([], 0, output, '')
            with self.subTest(size=len(output)), self.assertRaises(release.ReleaseError):
                REAL_API(self.config, "branches/main", releasing=True)

    def test_git_configuration_cannot_execute_or_redirect(self):
        self.prepare_publish()
        for name in (
            "filter.candidate.clean", "diff.candidate.textconv", "include.path",
            "includeIf.gitdir:other.path", "extensions.worktreeConfig", "http.extraHeader",
            "url.https://example.test/.insteadOf", "core.sshCommand", "remote.origin.vcs",
            "push.followTags", "push.recurseSubmodules", "submodule.example.url",
        ):
            self.git.config_text = name + "\nsynthetic-value\0"
            with self.subTest(name=name), self.assertRaisesRegex(release.ReleaseError, "configuration"):
                release.publish_candidate(self.config, self.run, self.option, self.worktree)
        self.assertFalse(self.git.committed)
        self.assertFalse(self.git.pushed)
        self.command.assert_not_called()

    def test_raw_diff_hash_preserves_crlf_bytes(self):
        self.prepare_publish()
        self.git.patch = PATCH.replace(b"\n", b"\r\n")
        self.assertEqual(release._patch_digest(self.config, self.worktree, "HEAD"), hashlib.sha256(self.git.patch).hexdigest())
        self.assertNotEqual(release._patch_digest(self.config, self.worktree, "HEAD"), self.option["diff_sha256"])
        diff = self.git.calls[-1][0]
        self.assertIn("--binary", diff)
        self.assertIn("--full-index", diff)
        self.assertIn("--no-textconv", diff)

    def test_publication_rechecks_claim_immediately_before_push(self):
        self.prepare_publish()
        original = self.git.__call__

        def change_revision(config, arguments, **kwargs):
            result = original(config, arguments, **kwargs)
            if "commit" in arguments:
                review.mark_attention(self.run["id"], "Synthetic concurrent stop")
            return result

        with patch.object(release.worker, "_git", side_effect=change_revision), self.assertRaises(release.ReleaseError):
            release.publish_candidate(self.config, self.run, self.option, self.worktree)
        self.assertFalse(self.git.pushed)

    def test_staged_or_committed_drift_never_pushes(self):
        self.prepare_publish()
        digest = self.option["diff_sha256"]
        for responses in ([digest, "0" * 64], [digest, digest, "0" * 64]):
            self.git.committed = False
            with self.subTest(responses=responses), patch.object(
                release, "_patch_digest", side_effect=responses,
            ), self.assertRaises(release.ReleaseError):
                release.publish_candidate(self.config, self.run, self.option, self.worktree)
            self.assertFalse(self.git.pushed)

    def test_worker_hook_disabling_is_reused(self):
        self.command.side_effect = None
        self.command.return_value = subprocess.CompletedProcess([], 0, BASE, '')
        release._git(self.config, ["rev-parse", "HEAD"], cwd=self.worktree)
        command = self.command.call_args.args[0]
        self.assertIn("core.hooksPath=/dev/null", command)
        self.assertIn("credential.helper=", command)
        self.assertIn("diff.external=", command)
        environment = self.command.call_args.kwargs["env"]
        self.assertFalse(any("TOKEN" in name or "PASSWORD" in name for name in environment))

    def test_check_app_binding_and_combined_status_context(self):
        self._testing()
        protection = self.api(self.config, "branches/main/protection")
        protection["required_status_checks"]["checks"] = [{"context": "offline-regression", "app_id": 999}]
        self.api.overrides["branches/main/protection"] = protection
        with self.assertRaises(release.ReleaseError):
            release.verify_testing(self.config, self.run)
        self.api.overrides.clear()
        self.api.overrides[f"commits/{HEAD}/check-runs?per_page=100&page=1&filter=latest"] = {"total_count": 0, "check_runs": []}
        self.api.overrides[f"commits/{HEAD}/status?per_page=100&page=1"] = {
            "total_count": 1, "sha": HEAD, "state": "success",
            "statuses": [{"context": "offline-regression", "state": "success"}],
        }
        self.assertTrue(release.verify_testing(self.config, self.run)["checks_passed"])

    def test_required_github_reviews_cannot_be_replaced_by_local_approval(self):
        self.releasing()
        protection = self.api(self.config, "branches/main/protection")
        protection["required_pull_request_reviews"] = {"required_approving_review_count": 1}
        self.api.overrides["branches/main/protection"] = protection
        self.command.side_effect = None
        decision = {
            "reviewDecision": "REVIEW_REQUIRED", "headRefOid": HEAD,
            "headRefName": self.run["branch"], "baseRefName": "main", "number": 7,
            "url": self.run["pr_url"],
        }
        self.command.return_value = subprocess.CompletedProcess([], 0, json.dumps(decision), '')
        with self.assertRaisesRegex(release.ReleaseError, "human reviews"):
            release.merge_release(self.config, self.run)
        self.assert_no_mutations()
        decision["reviewDecision"] = "APPROVED"
        self.command.return_value = subprocess.CompletedProcess([], 0, json.dumps(decision), '')
        self.assertEqual(release.merge_release(self.config, self.run)["merge_commit"], MERGE)
        self.assertEqual(self.command.call_args.kwargs["env"]["GH_TOKEN"], "synthetic-release-token")

    def test_wrong_merge_parents_do_not_receive_a_tag(self):
        self.releasing()
        self.api.overrides[f"git/commits/{MERGE}"] = {"sha": MERGE, "parents": [{"sha": HEAD}]}
        with self.assertRaises(release.ReleasePartialError):
            release.merge_release(self.config, self.run)
        self.assertFalse(any(endpoint == "git/refs" for endpoint, _, _, _ in self.api.calls))

    def test_wrong_tag_identity_is_partial_not_success(self):
        self.releasing()
        self.api.overrides["git/refs"] = {"ref": "refs/tags/v1.01", "object": {"type": "commit", "sha": BASE}}
        with self.assertRaises(release.ReleasePartialError) as caught:
            release.merge_release(self.config, self.run)
        self.assertEqual(caught.exception.merge_commit, MERGE)
        self.assertEqual(caught.exception.tag, "v1.01")

    def test_reconciliation_of_unmerged_run_never_mutates(self):
        self._testing()
        self.run = review.mark_attention(self.run["id"], "Synthetic stop")
        self.assertEqual(release.reconcile_release(self.config, self.run)["status"], "not_merged")
        self.assert_no_mutations()




if __name__ == "__main__":
    unittest.main()
