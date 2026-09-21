from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid
import zlib

from luigi_web.modules.feedback import review_worker as worker
from luigi_web.modules.feedback import maintainer, review, sandbox


def synthetic_png(width, height):
    def chunk(kind, content):
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\x20\x60\x80" * width) * height)) + chunk(b"IEND", b""))


class ArtifactPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {
            "LUIGI_MAINTAINER_QUEUE_DIR": str(self.root / "queue"),
            "LUIGI_MAINTAINER_ARTIFACT_DIR": str(self.root / "images"),
            "LUIGI_WEB_DATA_DIR": str(self.root / "production"),
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.run_id = str(uuid.uuid4())
        self.artifact_id = str(uuid.uuid4())

    def test_fixed_opaque_image_path_without_creation(self):
        result = worker.artifact_path(self.run_id, self.artifact_id, "desktop.png")
        self.assertEqual(result, self.root / "images" / self.run_id / self.artifact_id / "desktop.png")
        self.assertFalse(result.parent.exists())

    def test_rejects_user_paths_and_private_files(self):
        for name in ("../desktop.png", "source.patch", "metadata.json", "/desktop.png", "desktop.png!"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                worker.artifact_path(self.run_id, self.artifact_id, name)
        for identifier in ("..", "../../source", self.run_id.upper(), "!", ""):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                worker.artifact_path(identifier, self.artifact_id, "mobile.png")

    def test_rejects_application_data_root(self):
        os.environ["LUIGI_MAINTAINER_ARTIFACT_DIR"] = str(self.root / "production" / "images")
        with self.assertRaises(ValueError):
            worker.artifact_path(self.run_id, self.artifact_id, "desktop.png")

    def test_rejects_checkout_root(self):
        os.environ["LUIGI_MAINTAINER_ARTIFACT_DIR"] = str(Path(__file__).resolve().parent / "images")
        with self.assertRaises(ValueError):
            worker.artifact_path(self.run_id, self.artifact_id, "desktop.png")

    def test_rejects_symlink_ancestors(self):
        with patch.object(Path, "is_symlink", lambda path: path.name == "images"):
            with self.assertRaises(ValueError):
                worker.artifact_path(self.run_id, self.artifact_id, "desktop.png")

    def test_immutable_bounded_bytes(self):
        target = self.root / "source.patch"
        worker._write(target, b"synthetic\r\npatch\n")
        self.assertEqual(worker._read(target, 30), b"synthetic\r\npatch\n")
        with self.assertRaises(FileExistsError):
            worker._write(target, b"replacement")
        with self.assertRaises(ValueError):
            worker._read(target, 2)

    def test_rejects_hardlinked_artifact(self):
        target = self.root / "source.patch"
        target.write_bytes(b"synthetic")
        fake_stat = Mock(st_mode=0o100600, st_nlink=2, st_size=9)
        with patch.object(worker.os, "fstat", return_value=fake_stat), self.assertRaises(ValueError):
            worker._read(target, 30)

    @unittest.skipUnless(os.name == "posix", "Actual POSIX file modes and queue-group ownership require Linux")
    def test_shared_modes_use_queue_group_on_posix(self):
        queue = self.root / "queue"
        queue.mkdir()
        target = worker._directory(self.root / "shared", shared=True) / "desktop.png"
        worker._write(target, b"synthetic", shared=True)
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o750)
        self.assertEqual(target.stat().st_gid, queue.stat().st_gid)


class ReviewWorkerTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("luigi_web.modules.feedback.test_preview.verify_ready", return_value=True))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR"}}
        environment.update({
            "LUIGI_MAINTAINER_QUEUE_DIR": str(self.root / "queue"),
            "LUIGI_MAINTAINER_ARTIFACT_DIR": str(self.root / "images"),
            "LUIGI_WEB_DATA_DIR": str(self.root / "production"),
            "LUIGI_WEB_MAINTAINER_DB": str(self.root / "queue" / "maintainer.sqlite3"),
            "LUIGI_WEB_ENV_FILE": str(self.root / "absent.env"),
            "TEMP": str(self.root), "TMP": str(self.root), "SQLITE_TMPDIR": str(self.root),
            "APPDATA": str(self.root), "LOCALAPPDATA": str(self.root),
        })
        environment_patch = patch.dict(os.environ, environment, clear=True)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        for target in ("subprocess.run", "socket.create_connection", "smtplib.SMTP", "smtplib.SMTP_SSL",
                       "luigi_web.modules.feedback.repository._connect"):
            guard = patch(target, side_effect=AssertionError("External operations are forbidden in worker tests"))
            guard.start()
            self.addCleanup(guard.stop)
        self.publisher = Mock(side_effect=AssertionError("Generation must never publish"))
        publish_patch = patch.object(worker.legacy, "publish_draft_pr", self.publisher)
        publish_patch.start()
        self.addCleanup(publish_patch.stop)
        self.config = worker.WorkerConfig(self.root / "state", "https://github.com/example/project.git")
        self.job_id = maintainer.enqueue_feedback({
            "uuid": str(uuid.uuid4()), "category": maintainer.feedback.CATEGORIES[0], "page_path": "/tasks",
            "message": "Improve the synthetic example layout.",
        }, "Preserve keyboard access and the existing feature scope.")
        self.steps = []
        self.jobs = []
        self.base = "a" * 40
        self.contents = {}
        self.images = {name: synthetic_png(*size) for name, size in worker.IMAGE_SIZES.items()}
        self.availability = Mock(return_value=sandbox.Availability(True, "Synthetic runtime"))

    def prepare(self, config, job):
        self.steps.append("prepare")
        path = config.worktrees_root / job["uuid"]
        path.mkdir(parents=True)
        return worker.legacy.WorktreeContext(path, "automation/feedback-example", self.base)

    def agent(self, path, job, **kwargs):
        self.steps.append("agent")
        self.jobs.append((dict(job), kwargs))
        (path / "templates").mkdir()
        content = f"<p>Synthetic option {len(self.jobs)}</p>\n"
        (path / "templates" / "example.html").write_text(content, encoding="utf-8")
        self.contents[path] = content
        return worker.legacy.AgentOutcome("ready", "Example candidate prepared.")

    def patch_bytes(self, config, context, files):
        self.steps.append("patch")
        return b"diff --git a/templates/example.html b/templates/example.html\n" + self.contents[context.path].encode()

    def sandbox_run(self, worktree, artifact_root, candidate_id, *, preview_kind):
        self.steps.append("sandbox")
        self.assertEqual(preview_kind, "workspace")
        target = artifact_root / candidate_id
        target.mkdir()
        screenshots = []
        for name, size in worker.IMAGE_SIZES.items():
            (target / name).write_bytes(self.images[name])
            screenshots.append(sandbox.ScreenshotEvidence(name, hashlib.sha256(self.images[name]).hexdigest(), *size))
        evidence = sandbox.TestEvidence(
            candidate_id, True, 0, 4, "d" * 64, "localhost/luigi-maintainer@sha256:" + "e" * 64,
            tuple(sandbox.CheckEvidence(name, True, 0, count) for name, count in (
                ("tests", 4), ("templates", 2), ("routes", 3), ("whitespace", 0), ("screenshots", 2))),
            tuple(screenshots),
        )
        (target / "evidence.json").write_text(json.dumps(asdict(evidence)), encoding="ascii")
        return evidence

    def cleanup(self, config, context):
        self.steps.append("cleanup")
        shutil.rmtree(context.path)

    def generate(self, **overrides):
        dependencies = dict(
            availability=self.availability, preparer=self.prepare, agent_runner=self.agent,
            change_reader=lambda *args: ["templates/example.html"],
            policy_checker=lambda *args: self.steps.append("policy"),
            validator=lambda *args: self.steps.append("static"), patch_reader=self.patch_bytes,
            sandbox_runner=self.sandbox_run, cleaner=self.cleanup,
        )
        dependencies.update(overrides)
        return worker.generation_once(self.config, **dependencies)

    def test_unavailable_never_claims_job(self):
        self.availability.return_value = sandbox.Availability(False, "Not configured")
        self.assertEqual(self.generate().status, "Unavailable")
        self.assertEqual(maintainer.get_job(self.job_id)["status"], "Queued")
        self.assertEqual(self.steps, [])

    def test_three_distinct_options_finish_without_publishing(self):
        result = self.generate()
        self.assertEqual(result.status, "awaiting_approval")
        options = review.list_options(result.run_id)
        self.assertEqual([option["label"] for option in options], ["Option 1", "Option 2", "Option 3"])
        self.assertEqual(len({option["diff_sha256"] for option in options}), 3)
        self.assertTrue(all(option["tests_passed"] for option in options))
        self.assertEqual(len({job["uuid"] for job, kwargs in self.jobs}), 3)
        self.assertEqual(len({job["request_text"] for job, kwargs in self.jobs}), 3)
        original = maintainer.get_job(self.job_id)
        self.assertEqual(original["status"], "Needs attention")
        self.assertEqual(original["result_summary"], "Design options are awaiting review")
        for job, kwargs in self.jobs:
            self.assertTrue(job["request_text"].startswith(original["request_text"]))
            self.assertEqual(job["acceptance_criteria"], original["acceptance_criteria"])
            self.assertLessEqual(kwargs["timeout"], self.config.agent_timeout // 3)
        self.assertEqual(self.steps, ["prepare", "agent", "policy", "patch", "static", "sandbox", "patch", "cleanup"] * 3)
        self.assertEqual(len(review.notifications_due()), 1)
        self.assertFalse(list(self.config.worktrees_root.iterdir()))
        self.publisher.assert_not_called()

    def test_same_patch_cannot_become_two_options(self):
        result = self.generate(patch_reader=lambda *args: b"diff --git a/example b/example\n+same\n")
        self.assertEqual(result.status, "needs_attention")
        self.assertEqual(len(review.list_options(result.run_id)), 1)
        self.assertEqual(review.notifications_due(), [])

    def test_sandbox_failure_never_publishes(self):
        result = self.generate(sandbox_runner=Mock(side_effect=RuntimeError("synthetic failure")))
        self.assertEqual(result.status, "needs_attention")
        self.assertEqual(review.list_options(result.run_id), [])
        self.assertEqual(self.steps.count("cleanup"), 3)
        self.publisher.assert_not_called()

    def test_changed_base_stops_generation(self):
        def prepare(config, job):
            context = self.prepare(config, job)
            return replace(context, base_commit="b" * 40) if self.jobs else context
        result = self.generate(preparer=prepare)
        self.assertEqual(result.status, "needs_attention")
        self.assertEqual(len(self.jobs), 1)
        self.assertFalse(list(self.config.worktrees_root.iterdir()))

    def test_empty_or_missing_screenshot_checks_never_pass(self):
        evidence = sandbox.TestEvidence("synthetic", True, 0, 0, "d" * 64, "", (
            sandbox.CheckEvidence("tests", True, 0, 0), sandbox.CheckEvidence("templates", True, 0, 2),
            sandbox.CheckEvidence("routes", True, 0, 2), sandbox.CheckEvidence("screenshots", True, 0, 2),
        ))
        results = {result["command_id"]: result for result in worker._test_results(evidence, "a" * 64)}
        self.assertFalse(results["unittest"]["passed"])
        self.assertFalse(results["design_review"]["passed"])
        self.assertTrue(results["validator"]["passed"])

    def test_binary_source_rejected_before_sandbox(self):
        def agent(path, job, **kwargs):
            outcome = self.agent(path, job, **kwargs)
            (path / "templates" / "example.html").write_bytes(b"\x00\xff")
            return outcome
        result = self.generate(agent_runner=agent)
        self.assertEqual(result.status, "needs_attention")
        self.assertNotIn("sandbox", self.steps)

    def approved(self):
        generated = self.generate()
        run = review.get_run(generated.run_id)
        option = review.list_options(run["id"])[0]
        review.approve_option(run["id"], option["id"], run["revision"], "1.02")
        return review.get_run(run["id"]), option

    def publish(self, **overrides):
        def apply(config, context, candidate):
            self.steps.append("apply")
            (context.path / "templates").mkdir()
            (context.path / "templates" / "example.html").write_text("<p>Synthetic option 1</p>\n", encoding="utf-8")
            self.contents[context.path] = "<p>Synthetic option 1</p>\n"
        def receipt(config, run, option, path):
            self.steps.append("publish")
            return {"head_commit": "b" * 40, "branch": run["branch"],
                    "pr_url": "https://github.com/example/project/pull/1", "pr_number": 1}
        dependencies = dict(
            publisher=receipt, preparer=self.prepare, patch_applier=apply,
            branch_selector=lambda config, context, run: replace(context, branch_name=run["branch"]),
            change_reader=lambda *args: ["templates/example.html"], policy_checker=lambda *args: self.steps.append("policy"),
            validator=lambda *args: self.steps.append("static"), patch_reader=self.patch_bytes,
            exporter=lambda *args: sandbox.SourceSnapshot("d" * 64, 1, 40), cleaner=self.cleanup,
        )
        dependencies.update(overrides)
        return worker.publish_once(self.config, **dependencies)

    def test_publish_requires_admin_approval(self):
        generated = self.generate()
        publisher = Mock()
        self.assertEqual(self.publish(publisher=publisher).status, "Idle")
        publisher.assert_not_called()
        self.assertEqual(review.get_run(generated.run_id)["state"], "awaiting_approval")

    def test_publish_rechecks_candidate_without_claiming_ci_or_preview(self):
        run, option = self.approved()
        self.steps.clear()
        result = self.publish()
        self.assertEqual(result.status, "testing")
        self.assertEqual(self.steps, ["prepare", "apply", "policy", "static", "patch", "publish", "cleanup"])
        updated = review.get_run(run["id"])
        self.assertIsNone(updated["checks_commit"])
        self.assertIsNone(updated["preview_commit"])
        self.assertEqual(updated["version"], "1.02")

    def test_patch_mutation_is_rejected_before_preparing(self):
        run, option = self.approved()
        path = worker.load_candidate(self.config, run, option).patch_path
        path.chmod(0o600)
        path.write_bytes(b"diff --git a/example b/example\n+mutated\n")
        self.steps.clear()
        result = self.publish()
        self.assertEqual(result.status, "needs_attention")
        self.assertEqual(self.steps, [])

    def test_metadata_and_evidence_mutations_are_rejected(self):
        run, option = self.approved()
        root = self.config.state_root / "reviews" / run["id"] / option["artifact_id"]
        for name in ("metadata.json", "evidence.json"):
            path = root / name
            original = path.read_bytes()
            path.chmod(0o600)
            value = json.loads(original)
            if name == "metadata.json":
                value["image"] = "localhost/luigi-maintainer@sha256:" + "f" * 64
            else:
                value["tests_run"] = 0
            path.write_text(json.dumps(value), encoding="ascii")
            with self.subTest(name=name), self.assertRaises(ValueError):
                worker.load_candidate(self.config, run, option)
            path.write_bytes(original)

    def test_shared_screenshot_mutation_blocks_publish(self):
        run, option = self.approved()
        candidate = worker.load_candidate(self.config, run, option)
        image = option["screenshots"][0]
        path = worker.artifact_path(run["id"], image["artifact_id"], candidate.metadata["image_names"][image["artifact_id"]])
        path.chmod(0o600)
        path.write_bytes(b"not a PNG")
        self.assertEqual(self.publish().status, "needs_attention")

    def test_publish_base_drift_never_calls_publisher(self):
        self.approved()
        self.base = "f" * 40
        publisher = Mock()
        self.assertEqual(self.publish(publisher=publisher).status, "needs_attention")
        publisher.assert_not_called()
        self.assertFalse(list(self.config.worktrees_root.iterdir()))

    def test_publish_source_digest_mismatch_never_calls_publisher(self):
        self.approved()
        publisher = Mock()
        result = self.publish(publisher=publisher, exporter=lambda *args: sandbox.SourceSnapshot("f" * 64, 1, 40))
        self.assertEqual(result.status, "needs_attention")
        publisher.assert_not_called()

    def test_refresh_records_ci_only_and_never_releases(self):
        run, option = self.approved()
        self.publish()
        result = worker.refresh_once(self.config, verifier=lambda *args: {"head_commit": "b" * 40, "checks_passed": True})
        self.assertEqual(result.status, "testing")
        current = review.get_run(run["id"])
        self.assertEqual(current["checks_commit"], "b" * 40)
        self.assertIsNone(current["preview_commit"])
        self.assertEqual(worker.preview_once(self.config, run["id"]).status, "Unavailable")
        releaser = Mock()
        self.assertEqual(worker.release_once(self.config, releaser=releaser).status, "Idle")
        releaser.assert_not_called()

    def test_refresh_changed_head_invalidates_checks_and_preview(self):
        run, option = self.approved()
        self.publish()
        review.record_preview(run["id"], "b" * 40, review.preview_path(run["id"]))
        result = worker.refresh_once(self.config, verifier=lambda *args: {"head_commit": "c" * 40, "checks_passed": True})
        self.assertEqual(result.status, "needs_attention")
        current = review.get_run(run["id"])
        self.assertIsNone(current["checks_commit"])
        self.assertIsNone(current["preview_commit"])

    def test_live_preview_and_exact_admin_release_required(self):
        run, option = self.approved()
        self.publish()
        worker.refresh_once(self.config, verifier=lambda *args: {"head_commit": "b" * 40, "checks_passed": True})
        preview = worker.preview_once(self.config, run["id"], preview_runner=lambda *args: {
            "ready": True, "head_commit": "b" * 40, "preview_url": review.preview_path(run["id"]),
        })
        self.assertEqual(preview.status, "testing")
        current = review.get_run(run["id"])
        releaser = Mock(return_value={"merge_commit": "c" * 40, "tag": "v1.02"})
        self.assertEqual(worker.release_once(self.config, releaser=releaser).status, "Idle")
        review.approve_release(run["id"], current["revision"], "b" * 40, "1.02", confirmed=True)
        result = worker.release_once(self.config, releaser=releaser)
        self.assertEqual(result.status, "released")
        releaser.assert_called_once()

    def test_cancelled_run_cannot_publish_or_release(self):
        run, option = self.approved()
        review.cancel_run(run["id"], run["revision"])
        publisher, releaser = Mock(), Mock()
        self.assertEqual(self.publish(publisher=publisher).status, "Idle")
        self.assertEqual(worker.release_once(self.config, releaser=releaser).status, "Idle")
        publisher.assert_not_called()
        releaser.assert_not_called()

    def test_default_missing_credentials_does_not_claim_publication(self):
        run, option = self.approved()
        self.assertEqual(worker.publish_once(self.config).status, "Unavailable")
        self.assertEqual(review.get_run(run["id"])["state"], "publish_queued")

    def test_notifications_retry_without_sending_approved_content(self):
        generated = self.generate()
        os.environ["LUIGI_MAINTAINER_UI_URL"] = "https://example.test"
        notifier = Mock(return_value=False)
        self.assertEqual(worker.notify_once(notifier=notifier).status, "Notification retry")
        job, = notifier.call_args.args
        self.assertEqual(set(job), {"uuid", "feedback_uuid", "notification_id"})
        self.assertEqual(job["uuid"], generated.run_id)
        self.assertNotIn("synthetic example layout", repr(notifier.call_args))
        self.assertIn("/feedback/reviews/" + generated.run_id, notifier.call_args.kwargs["summary"])
        self.assertEqual(worker.notify_once(notifier=notifier).status, "Idle")
        with maintainer._connect() as connection:
            connection.execute("UPDATE review_notifications SET available_at = ?", ("2000-01-01",))
        notifier.return_value = True
        self.assertEqual(worker.notify_once(notifier=notifier).status, "Notified")
        self.assertEqual(worker.notify_once(notifier=notifier).status, "Idle")

    def test_notification_rejects_url_credentials_before_claim(self):
        self.generate()
        notifier = Mock()
        os.environ["LUIGI_MAINTAINER_UI_URL"] = "https://example.test/?token=synthetic"
        self.assertEqual(worker.notify_once(notifier=notifier).status, "Unavailable")
        notifier.assert_not_called()
        self.assertEqual(review.notifications_due()[0]["attempts"], 0)

    def test_one_passing_option_requires_attention_but_two_are_reviewable(self):
        calls = 0
        def fail_after_one(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise ValueError("Synthetic sandbox failure")
            return self.sandbox_run(*args, **kwargs)
        result = self.generate(sandbox_runner=fail_after_one)
        self.assertEqual(result.status, "needs_attention")
        self.assertEqual(len(review.list_options(result.run_id)), 1)
        self.assertEqual(review.notifications_due(), [])

    def test_two_passing_options_survive_third_candidate_failure(self):
        def sandbox_run(*args, **kwargs):
            if len(self.jobs) == 3:
                raise ValueError("Synthetic sandbox failure")
            return self.sandbox_run(*args, **kwargs)
        result = self.generate(sandbox_runner=sandbox_run)
        self.assertEqual(result.status, "awaiting_approval")
        self.assertEqual(len(review.list_options(result.run_id)), 2)

    def test_all_no_change_finishes_job_without_options(self):
        result = self.generate(agent_runner=lambda *args, **kwargs: worker.legacy.AgentOutcome("no_change", "Already satisfied."),
                               change_reader=lambda *args: [])
        self.assertEqual(result.status, "needs_attention")
        self.assertEqual(review.list_options(result.run_id), [])
        self.assertEqual(maintainer.get_job(self.job_id)["status"], "Needs attention")
        self.assertEqual(self.steps.count("cleanup"), 3)

    def test_generation_budget_expiration_stops_without_executing(self):
        result = self.generate(monotonic=Mock(side_effect=[0.0, float(self.config.agent_timeout)]))
        self.assertEqual(result.status, "needs_attention")
        self.assertEqual(self.steps, ["prepare", "cleanup"])

    def test_policy_denial_stops_before_sandbox(self):
        result = self.generate(policy_checker=Mock(side_effect=worker.legacy.AttentionRequired("Synthetic policy boundary")))
        self.assertEqual(result.status, "needs_attention")
        self.assertNotIn("sandbox", self.steps)
        self.assertEqual(self.steps.count("cleanup"), 1)

    def test_unapproved_preview_path_does_not_prepare(self):
        with maintainer._connect() as connection:
            connection.execute("UPDATE maintainer_jobs SET page_path = ? WHERE uuid = ?", ("/tasks/private-record", self.job_id))
        self.assertEqual(self.generate().status, "needs_attention")
        self.assertEqual(self.steps, [])

    def test_option_summary_is_sanitized(self):
        def agent(*args, **kwargs):
            outcome = self.agent(*args, **kwargs)
            return replace(outcome, summary="Example maintainer@example.test token=synthetic-value")
        result = self.generate(agent_runner=agent)
        for option in review.list_options(result.run_id):
            self.assertNotIn("maintainer@example.test", option["summary"])
            self.assertNotIn("synthetic-value", option["summary"])

    def test_live_preview_wrong_sha_does_not_attest(self):
        run, option = self.approved()
        self.publish()
        result = worker.preview_once(self.config, run["id"], preview_runner=lambda *args: {
            "ready": True, "head_commit": "f" * 40, "preview_url": review.preview_path(run["id"]),
        })
        self.assertEqual(result.status, "needs_attention")
        self.assertIsNone(review.get_run(run["id"])["preview_commit"])

    def test_remote_publish_exception_is_not_retried(self):
        run, option = self.approved()
        publisher = Mock(side_effect=RuntimeError("synthetic token=not-for-error-details"))
        self.assertEqual(self.publish(publisher=publisher).status, "needs_attention")
        self.assertEqual(self.publish(publisher=publisher).status, "Idle")
        publisher.assert_called_once()
        self.assertNotIn("not-for-error-details", review.get_run(run["id"])["error_detail"])

    def test_stale_notification_success_cannot_acknowledge_new_attempt(self):
        self.generate()
        def notifier(*args, **kwargs):
            with maintainer._connect() as connection:
                connection.execute("UPDATE review_notifications SET attempts = attempts + 1")
            return True
        self.assertEqual(worker.notify_once(notifier=notifier).status, "Notification lease expired")
        with maintainer._connect() as connection:
            status = connection.execute("SELECT status FROM review_notifications").fetchone()["status"]
        self.assertEqual(status, "sending")

    def test_expired_notification_lease_can_be_reclaimed(self):
        self.generate()
        first = review.claim_notification()
        with maintainer._connect() as connection:
            connection.execute("UPDATE review_notifications SET lease_until = ?", ("2000-01-01",))
        notifier = Mock(return_value=True)
        self.assertEqual(worker.notify_once(notifier=notifier).status, "Notified")
        self.assertEqual(notifier.call_args.args[0]["notification_id"], first["id"])

    def test_capture_patch_uses_intent_staging_and_exact_bytes(self):
        context = self.prepare(self.config, maintainer.get_job(self.job_id))
        self.agent(context.path, maintainer.get_job(self.job_id))
        content = b"diff --git a/templates/example.html b/templates/example.html\r\n+synthetic\r\n"
        git = Mock()
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=content))
        with patch.object(worker.legacy, "_git", git), patch.object(worker.subprocess, "run", runner):
            actual = worker.capture_patch(self.config, context, ["templates/example.html"])
        self.assertEqual(actual, content)
        self.assertEqual(git.call_args.args[1], ["add", "--intent-to-add", "--", "templates/example.html"])
        command = runner.call_args.args[0]
        for flag in ("--no-ext-diff", "--no-textconv", "--binary", "--full-index"):
            self.assertIn(flag, command)
        self.assertNotIn("text", runner.call_args.kwargs)
        self.cleanup(self.config, context)

    def test_apply_patch_checks_before_applying_without_host_execution(self):
        run, option = self.approved()
        candidate = worker.load_candidate(self.config, run, option)
        context = self.prepare(self.config, {**maintainer.get_job(self.job_id), "uuid": str(uuid.uuid4())})
        with patch.object(worker.legacy, "_git") as git:
            worker.apply_candidate(self.config, context, candidate)
        self.assertIn("--check", git.call_args_list[0].args[1])
        self.assertNotIn("--check", git.call_args_list[1].args[1])
        self.cleanup(self.config, context)

    def test_cli_notify_does_not_request_copilot_config(self):
        with patch.object(worker, "notify_once", return_value=worker.WorkerResult("Idle")) as notifier, patch.object(
            worker.WorkerConfig, "from_env", side_effect=AssertionError("No Copilot configuration for notification")
        ), patch("builtins.print"):
            self.assertEqual(worker.main(["--notify"]), 0)
        notifier.assert_called_once()

    def test_ci_pending_remains_refreshable_without_preview_receipt(self):
        run, option = self.approved()
        self.publish()
        pending = Mock(side_effect=ValueError("Required checks not complete"))
        self.assertEqual(worker.refresh_once(self.config, verifier=pending).status, "testing")
        self.assertEqual(review.get_run(run["id"])["state"], "testing")
        ready = lambda *args: {"head_commit": "b" * 40, "checks_passed": True}
        self.assertEqual(worker.refresh_once(self.config, verifier=ready).status, "testing")
        current = review.get_run(run["id"])
        self.assertEqual(current["checks_commit"], "b" * 40)
        self.assertIsNone(current["preview_commit"])

    def test_select_review_branch_renames_only_the_disposable_branch(self):
        run, option = self.approved()
        context = self.prepare(self.config, {**maintainer.get_job(self.job_id), "uuid": str(uuid.uuid4())})
        with patch.object(worker.legacy, "_git") as git:
            selected = worker.select_review_branch(self.config, context, run)
        self.assertEqual(selected.branch_name, run["branch"])
        self.assertEqual(git.call_args.args[1], ["branch", "-m", context.branch_name, run["branch"]])
        self.cleanup(self.config, context)

    def test_partial_publication_receipt_survives_cleanup(self):
        run, option = self.approved()
        error = RuntimeError("Private details must not persist")
        error.head_commit = "b" * 40
        error.branch = run["branch"]
        self.assertEqual(self.publish(publisher=Mock(side_effect=error)).status, "needs_attention")
        path = self.config.state_root / "reviews" / run["id"] / "publication-receipt.json"
        receipt = json.loads(path.read_bytes())
        self.assertEqual(receipt, {"run_id": run["id"], "phase": "publication", "head_commit": "b" * 40, "branch": run["branch"]})
        self.assertFalse(list(self.config.worktrees_root.iterdir()))

    def test_release_credentials_checked_before_claim_and_copilot_is_excluded(self):
        run, option = self.approved()
        self.publish()
        review.record_preview(run["id"], "b" * 40, review.preview_path(run["id"]))
        checked = review.record_test_result(run["id"], "b" * 40, True)
        review.approve_release(run["id"], checked["revision"], "b" * 40, "1.02", confirmed=True)
        self.assertEqual(worker.release_once(self.config).status, "Unavailable")
        self.assertEqual(review.get_run(run["id"])["state"], "release_queued")
        os.environ["LUIGI_RELEASE_GITHUB_TOKEN"] = "synthetic-release-credential"
        os.environ["LUIGI_MAINTAINER_COPILOT_TOKEN"] = "synthetic-copilot-credential"
        self.assertEqual(worker.release_once(self.config).status, "Unavailable")
        self.assertEqual(review.get_run(run["id"])["state"], "release_queued")

    def test_partial_release_never_retries_merge_and_retains_only_identities(self):
        run, option = self.approved()
        self.publish()
        review.record_preview(run["id"], "b" * 40, review.preview_path(run["id"]))
        checked = review.record_test_result(run["id"], "b" * 40, True)
        review.approve_release(run["id"], checked["revision"], "b" * 40, "1.02", confirmed=True)
        error = RuntimeError("Synthetic details must not persist")
        error.merge_commit = "c" * 40
        error.tag = "v1.02"
        releaser = Mock(side_effect=error)
        self.assertEqual(worker.release_once(self.config, releaser=releaser).status, "needs_attention")
        self.assertEqual(worker.release_once(self.config, releaser=releaser).status, "Idle")
        releaser.assert_called_once()
        path = self.config.state_root / "reviews" / run["id"] / "release-receipt.json"
        self.assertEqual(json.loads(path.read_bytes()), {
            "run_id": run["id"], "phase": "release", "merge_commit": "c" * 40, "tag": "v1.02",
        })


if __name__ == "__main__":
    unittest.main()
