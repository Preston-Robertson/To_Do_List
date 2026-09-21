"""Synthetic, offline tests for the durable multi-option review store."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from luigi_web.modules.feedback import maintainer, review


BASE_COMMIT = "a" * 40
HEAD_COMMIT = "b" * 40
NEW_HEAD_COMMIT = "c" * 40
MERGE_COMMIT = "d" * 40


def test_results(passed: bool = True) -> list[dict]:
    return [{
        "command_id": command, "passed": passed, "total": 12,
        "failed": 0 if passed else 1, "skipped": 0, "output_digest": "b" * 64,
    } for command in sorted(review.REQUIRED_CHECKS)]


class FeedbackReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        self.root = Path(self.storage.name)
        environment = patch.dict(os.environ, {
            "LUIGI_WEB_MAINTAINER_DB": str(self.root / "maintainer.db"),
            "LUIGI_WEB_FEEDBACK_DB": str(self.root / "must-not-exist.db"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        guard = patch.object(maintainer.feedback, "get_item", side_effect=AssertionError("raw inbox read"))
        guard.start()
        self.addCleanup(guard.stop)

    def enqueue(self) -> str:
        return maintainer.enqueue_feedback({
            "uuid": str(uuid.uuid4()), "category": "Idea",
            "message": "Synthetic approved improvement", "page_path": "/feedback",
        }, "Synthetic acceptance criteria")

    def new_run(self, options_requested: int = 3) -> dict:
        job_id = self.enqueue()
        self.assertEqual(maintainer.claim_next_job()["uuid"], job_id)
        return review.create_run(job_id, BASE_COMMIT, options_requested)

    def option(self, run: dict, seed: str = "first", passed: bool = True, **overrides) -> dict:
        arguments = {
            "label": "Synthetic option", "summary": "Synthetic approved summary",
            "diff_sha256": hashlib.sha256(seed.encode()).hexdigest(),
            "artifact_id": str(uuid.uuid4()), "test_results": test_results(passed),
        }
        arguments.update(overrides)
        return review.add_option(run["id"], **arguments)

    def ready(self) -> tuple[dict, list[dict]]:
        run = self.new_run()
        options = [self.option(run), self.option(run, "second")]
        return review.finish_generation(run["id"]), options

    def publish_queued(self, version: str = "1.01") -> dict:
        run, options = self.ready()
        return review.approve_option(run["id"], options[0]["id"], run["revision"], version)

    def published(self) -> dict:
        run = self.publish_queued()
        self.assertEqual(review.claim_publish()["id"], run["id"])
        return review.record_published(run["id"], HEAD_COMMIT, run["branch"],
                                       "https://github.com/example/project/pull/7", 7)

    def checked_run(self) -> dict:
        run = self.published()
        review.record_preview(run["id"], HEAD_COMMIT, review.preview_path(run["id"]))
        return review.record_test_result(run["id"], HEAD_COMMIT, True)

    def release_queued(self) -> dict:
        run = self.checked_run()
        return review.approve_release(run["id"], run["revision"], HEAD_COMMIT, "1.01", confirmed=True)

    def test_only_running_approved_job_can_start_once(self) -> None:
        job_id = self.enqueue()
        with self.assertRaisesRegex(ValueError, "Running"):
            review.create_run(job_id, BASE_COMMIT)
        maintainer.claim_next_job()
        run = review.create_run(job_id, BASE_COMMIT)
        self.assertEqual(run["state"], "generating")
        self.assertEqual(run["revision"], 1)
        self.assertEqual(run["repository_id"], "host")
        self.assertEqual(run["branch"], f"automation/review-{uuid.UUID(run['id']).hex}")
        self.assertEqual(review.get_run(run["id"]), run)
        self.assertEqual(review.list_runs(), [run])
        self.assertEqual(review.list_events(run["id"])[0]["actor_role"], "worker")
        self.assertEqual(maintainer.get_job(job_id)["status"], "Running")
        with self.assertRaisesRegex(ValueError, "already"):
            review.create_run(job_id, BASE_COMMIT)
        self.assertFalse((self.root / "must-not-exist.db").exists())

    def test_creation_rejects_invalid_identity_commit_and_count(self) -> None:
        run = self.new_run()
        for commit in ("abc123", "g" * 40, "a" * 41, "", BASE_COMMIT + "\n"):
            with self.subTest(commit=commit), self.assertRaises(ValueError):
                review.create_run(run["job_uuid"], commit)
        for count in (1, 4, True, 2.0):
            with self.subTest(count=count), self.assertRaises(ValueError):
                review.create_run(run["job_uuid"], BASE_COMMIT, count)
        with self.assertRaises(ValueError):
            review.create_run("invalid", BASE_COMMIT)
        with self.assertRaises(ValueError):
            review.create_run(str(uuid.uuid4()), BASE_COMMIT)

    def test_listing_is_bounded(self) -> None:
        self.new_run()
        self.new_run()
        self.assertEqual(len(review.list_runs(limit=1)), 1)
        self.assertEqual(review.list_runs(state="released"), [])
        for limit in (0, 101, True, 2.5):
            with self.assertRaises(ValueError):
                review.list_runs(limit=limit)
        with self.assertRaises(ValueError):
            review.list_runs(state="unknown")

    def test_two_distinct_passing_options_enable_approval(self) -> None:
        run = self.new_run()
        options = [self.option(run), self.option(run, "second"), self.option(run, "third", False)]
        run = review.finish_generation(run["id"])
        self.assertEqual(run["state"], "awaiting_approval")
        self.assertEqual(run["revision"], 5)
        self.assertEqual(review.list_options(run["id"]), options)
        self.assertFalse(options[-1]["tests_passed"])
        with self.assertRaises(ValueError):
            self.option(run, "late")
        with self.assertRaises(ValueError):
            review.finish_generation(run["id"])

    def test_insufficient_passing_options_require_attention(self) -> None:
        for passing in (0, 1):
            run = self.new_run()
            self.option(run, passed=bool(passing))
            self.option(run, "second", False)
            finished = review.finish_generation(run["id"])
            self.assertEqual(finished["state"], "needs_attention")
            self.assertIsNone(finished["selected_option_id"])

    def test_options_are_immutable_deduplicated_and_bounded(self) -> None:
        run = self.new_run(options_requested=2)
        option = self.option(run)
        before = review.get_run(run["id"])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.option(run)
        self.assertEqual(review.get_run(run["id"]), before)
        self.option(run, "second")
        with self.assertRaisesRegex(ValueError, "limit"):
            self.option(run, "third")
        with maintainer._connect() as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                conn.execute("UPDATE review_options SET tests_passed = 0 WHERE id = ?", (option["id"],))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                conn.execute("DELETE FROM review_options WHERE id = ?", (option["id"],))
        self.assertEqual(review.get_option(option["id"]), option)

    def test_metadata_sanitization_and_digest_binding(self) -> None:
        run = self.new_run()
        unsafe = "Example example@example.test token=synthetic-secret\x00"
        screenshot = {"artifact_id": str(uuid.uuid4()), "sha256": "e" * 64, "width": 1440, "height": 900}
        option = self.option(run, label=unsafe, summary=unsafe, notes=unsafe + "x" * 3000,
                             screenshots=[screenshot], tree_sha256="f" * 64)
        metadata = review.candidate_metadata(option["id"])
        self.assertNotIn("artifact_id", metadata)
        self.assertNotIn("tree_sha256", metadata)
        self.assertNotIn("example@example.test", json.dumps(metadata))
        self.assertNotIn("synthetic-secret", json.dumps(metadata))
        self.assertNotIn("\x00", option["summary"])
        self.assertLessEqual(len(option["notes"]), 2000)
        evidence = {field: option[field] for field in (
            "diff_sha256", "tree_sha256", "artifact_id", "test_results", "screenshots",
        )}
        expected = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":"),
                                            ensure_ascii=True).encode("ascii")).hexdigest()
        self.assertEqual(option["validation_digest"], expected)
        self.assertEqual(metadata["screenshots"], [screenshot])

    def test_rejects_paths_urls_logs_and_invalid_digests(self) -> None:
        run = self.new_run()
        for overrides in (
            {"artifact_id": "../private.patch"}, {"artifact_id": "https://example.test/patch"},
            {"diff_sha256": "g" * 64}, {"tree_sha256": "short"},
            {"test_results": [{**test_results()[0], "output": "must not persist"}]},
            {"test_results": [{**test_results()[0], "output_digest": "bad"}]},
            {"test_results": [{**test_results()[0], "command_id": "arbitrary shell"}]},
            {"screenshots": [{"url": "https://example.test/private.png"}]},
        ):
            with self.subTest(fields=list(overrides)), self.assertRaises(ValueError):
                self.option(run, **overrides)
        self.assertEqual(review.list_options(run["id"]), [])
        self.assertEqual(review.get_run(run["id"])["revision"], 1)

    def test_incomplete_or_failed_evidence_is_not_passing(self) -> None:
        run = self.new_run()
        missing = self.option(run, test_results=test_results()[:1])
        empty = self.option(run, "empty", test_results=[])
        failed = self.option(run, "failed", False)
        self.assertTrue(all(not option["tests_passed"] for option in (missing, empty, failed)))
        self.assertEqual(review.finish_generation(run["id"])["state"], "needs_attention")

    def test_inconsistent_counts_and_skipped_passes_rejected(self) -> None:
        run = self.new_run()
        for overrides in ({"failed": 1}, {"skipped": 1}, {"total": 0}, {"total": True},
                          {"total": -1}, {"total": 1_000_001}, {"passed": 1}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.option(run, test_results=[{**test_results()[0], **overrides}])
        with self.assertRaises(ValueError):
            self.option(run, test_results=[test_results()[0], test_results()[0]])

    def test_option_approval_binds_exact_candidate_version_and_revision(self) -> None:
        run, options = self.ready()
        with self.assertRaisesRegex(ValueError, "stale"):
            review.approve_option(run["id"], options[0]["id"], run["revision"] - 1, "1.01")
        approved = review.approve_option(run["id"], options[0]["id"], run["revision"], "1.01")
        self.assertEqual(approved["state"], "publish_queued")
        self.assertEqual(approved["version"], "1.01")
        self.assertEqual(approved["expected_tag"], "v1.01")
        self.assertEqual(approved["approved_diff_sha256"], options[0]["diff_sha256"])
        self.assertEqual(approved["approved_validation_digest"], options[0]["validation_digest"])
        self.assertEqual(review.list_events(run["id"])[0]["actor_role"], "admin")
        with self.assertRaises(ValueError):
            review.approve_option(run["id"], options[1]["id"], approved["revision"], "1.02")

    def test_cross_run_failed_and_missing_options_cannot_be_approved(self) -> None:
        run = self.new_run()
        self.option(run)
        self.option(run, "second")
        failed = self.option(run, "third", False)
        run = review.finish_generation(run["id"])
        other, options = self.ready()
        for option_id in (failed["id"], options[0]["id"], str(uuid.uuid4())):
            with self.assertRaises(ValueError):
                review.approve_option(run["id"], option_id, run["revision"], "1.0")
        self.assertEqual(review.get_run(run["id"]), run)
        self.assertEqual(review.get_run(other["id"]), other)

    def test_version_is_required_numeric_bounded_and_never_normalized(self) -> None:
        run, options = self.ready()
        for version in ("", "1", "1.x", "v1.0", "1.0rc1", "1.0.0.0", "1000.0", "1.0\n", " 1.0", 1.0):
            with self.subTest(version=version), self.assertRaises(ValueError):
                review.approve_option(run["id"], options[0]["id"], run["revision"], version)
        for version in ("1.0", "1.01", "1.0.1", "001.002.003", "999.999"):
            approved = self.publish_queued(version)
            self.assertEqual(approved["version"], version)
            self.assertEqual(approved["expected_tag"], f"v{version}")

    def test_publishing_requires_claim_and_fixed_branch_safe_pr_url(self) -> None:
        run = self.publish_queued()
        with self.assertRaises(ValueError):
            review.record_published(run["id"], HEAD_COMMIT, run["branch"],
                                    "https://github.com/example/project/pull/7", 7)
        claimed = review.claim_publish()
        self.assertEqual(claimed["revision"], run["revision"] + 1)
        self.assertIsNone(review.claim_publish())
        for head, branch, url, number in (
            ("short", run["branch"], "https://github.com/example/project/pull/7", 7),
            (HEAD_COMMIT, "main", "https://github.com/example/project/pull/7", 7),
            (HEAD_COMMIT, run["branch"], "https://github.com/example/project/pull/8", 7),
            (HEAD_COMMIT, run["branch"], "https://github.com/example/project/pull/7?token=example", 7),
            (HEAD_COMMIT, run["branch"], "https://example.test/pull/7", 7),
            (HEAD_COMMIT, run["branch"], "https://github.com/example/project/pull/7", True),
        ):
            with self.assertRaises(ValueError):
                review.record_published(run["id"], head, branch, url, number)
        published = review.record_published(run["id"], HEAD_COMMIT, run["branch"],
                                            "https://github.com/example/project/pull/7", 7)
        self.assertEqual(published["state"], "testing")
        self.assertIsNone(published["checks_commit"])
        self.assertIsNone(published["preview_commit"])

    def test_checks_and_preview_are_distinct_head_bound_worker_receipts(self) -> None:
        run = self.published()
        for url in ("https://example.test/preview", "//example.test/preview", "/other/preview/",
                    review.preview_path(run["id"]) + "?token=example"):
            with self.assertRaises(ValueError):
                review.record_preview(run["id"], HEAD_COMMIT, url)
        for head in (NEW_HEAD_COMMIT, "invalid"):
            with self.assertRaises(ValueError):
                review.record_preview(run["id"], head, review.preview_path(run["id"]))
            with self.assertRaises(ValueError):
                review.record_test_result(run["id"], head, True)
        checked = review.record_test_result(run["id"], HEAD_COMMIT, True)
        self.assertIsNone(checked["preview_commit"])
        with self.assertRaisesRegex(ValueError, "verified preview"):
            review.approve_release(run["id"], checked["revision"], HEAD_COMMIT, "1.01", True)
        previewed = review.record_preview(run["id"], HEAD_COMMIT, review.preview_path(run["id"]))
        self.assertEqual(previewed["preview_commit"], HEAD_COMMIT)
        failed = review.record_test_result(run["id"], HEAD_COMMIT, False)
        self.assertIsNone(failed["checks_commit"])
        with self.assertRaises(ValueError):
            review.approve_release(run["id"], failed["revision"], HEAD_COMMIT, "1.01", True)

    def test_release_requires_current_admin_revision_head_version_and_checkbox(self) -> None:
        run = self.checked_run()
        self.assertIsNone(review.claim_release())
        for revision, head, version, confirmed in (
            (run["revision"] - 1, HEAD_COMMIT, "1.01", True),
            (run["revision"], NEW_HEAD_COMMIT, "1.01", True),
            (run["revision"], HEAD_COMMIT, "1.1", True),
            (run["revision"], HEAD_COMMIT, "1.02", True),
            (run["revision"], HEAD_COMMIT, "1.01", False),
            (run["revision"], HEAD_COMMIT, "1.01", 1),
        ):
            with self.assertRaises(ValueError):
                review.approve_release(run["id"], revision, head, version, confirmed)
        with self.assertRaises(ValueError):
            review.approve_release(run["id"], run["revision"], HEAD_COMMIT, "1.01")
        self.assertEqual(review.get_run(run["id"]), run)
        queued = review.approve_release(run["id"], run["revision"], HEAD_COMMIT, "1.01", True)
        self.assertEqual(queued["state"], "release_queued")
        self.assertEqual(queued["release_approved_head"], HEAD_COMMIT)
        self.assertEqual(queued["release_approved_version"], "1.01")

    def test_release_records_exact_tag_only_after_approved_claim(self) -> None:
        run = self.release_queued()
        with self.assertRaises(ValueError):
            review.record_released(run["id"], MERGE_COMMIT, "v1.01")
        self.assertEqual(review.claim_release()["id"], run["id"])
        self.assertIsNone(review.claim_release())
        for commit, tag in (("short", "v1.01"), (MERGE_COMMIT, "v1.1"), (MERGE_COMMIT, "v1.02")):
            with self.assertRaises(ValueError):
                review.record_released(run["id"], commit, tag)
        released = review.record_released(run["id"], MERGE_COMMIT, "v1.01")
        self.assertEqual(released["state"], "released")
        self.assertEqual(released["merge_commit"], MERGE_COMMIT)
        self.assertEqual(released["tag"], "v1.01")
        with self.assertRaises(ValueError):
            review.record_released(run["id"], MERGE_COMMIT, "v1.01")
        with self.assertRaises(ValueError):
            review.cancel_run(run["id"], released["revision"])

    def test_new_head_revokes_checks_preview_and_release_authorization(self) -> None:
        run = self.release_queued()
        self.assertEqual(review.record_head(run["id"], HEAD_COMMIT), run)
        changed = review.record_head(run["id"], NEW_HEAD_COMMIT)
        self.assertEqual(changed["state"], "testing")
        for field in ("checks_commit", "preview_commit", "preview_url", "release_approved_head",
                      "release_approved_version", "release_approved_at"):
            self.assertIsNone(changed[field])
        self.assertIsNone(review.claim_release())
        with self.assertRaises(ValueError):
            review.record_test_result(run["id"], HEAD_COMMIT, True)
        with self.assertRaises(ValueError):
            review.approve_release(run["id"], changed["revision"], NEW_HEAD_COMMIT, "1.01", True)
        review.record_preview(run["id"], NEW_HEAD_COMMIT, review.preview_path(run["id"]))
        changed = review.record_test_result(run["id"], NEW_HEAD_COMMIT, True)
        queued = review.approve_release(run["id"], changed["revision"], NEW_HEAD_COMMIT, "1.01", True)
        self.assertEqual(queued["release_approved_head"], NEW_HEAD_COMMIT)
        approvals = [event for event in review.list_events(run["id"]) if event["action"] == "release_approved"]
        self.assertEqual([event["metadata"]["head_commit"] for event in approvals], [NEW_HEAD_COMMIT, HEAD_COMMIT])

    def test_checks_failing_after_release_approval_revoke_authorization(self) -> None:
        run = self.release_queued()
        self.assertEqual(review.record_test_result(run["id"], HEAD_COMMIT, True), run)
        failed = review.record_test_result(run["id"], HEAD_COMMIT, False)
        self.assertEqual(failed["state"], "testing")
        self.assertIsNone(failed["checks_commit"])
        self.assertIsNone(failed["release_approved_head"])
        self.assertIsNone(review.claim_release())
        checked = review.record_test_result(run["id"], HEAD_COMMIT, True)
        self.assertIsNone(review.claim_release())
        self.assertIsNone(checked["release_approved_head"])

    def test_checks_failing_during_release_require_attention_without_retry(self) -> None:
        run = self.release_queued()
        review.claim_release()
        failed = review.record_test_result(run["id"], HEAD_COMMIT, False)
        self.assertEqual(failed["state"], "needs_attention")
        self.assertIsNone(failed["release_approved_head"])
        self.assertIsNone(review.claim_release())
        with self.assertRaises(ValueError):
            review.record_released(run["id"], MERGE_COMMIT, "v1.01")

    def test_ambiguous_outcomes_never_automatically_retry(self) -> None:
        publishing = self.publish_queued()
        review.claim_publish()
        attention = review.mark_attention(publishing["id"], "Check external publish outcome")
        self.assertEqual(attention["state"], "needs_attention")
        self.assertIsNone(review.claim_publish())
        releasing = self.release_queued()
        review.claim_release()
        attention = review.record_head(releasing["id"], NEW_HEAD_COMMIT)
        self.assertEqual(attention["state"], "needs_attention")
        self.assertIsNone(attention["release_approved_head"])
        self.assertIsNone(review.claim_release())
        with self.assertRaises(ValueError):
            review.record_released(releasing["id"], MERGE_COMMIT, "v1.01")

    def test_cancel_and_failure_are_bounded_terminal_transitions(self) -> None:
        run = self.publish_queued()
        with self.assertRaisesRegex(ValueError, "stale"):
            review.cancel_run(run["id"], run["revision"] - 1)
        cancelled = review.cancel_run(run["id"], run["revision"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertIsNone(review.claim_publish())
        with self.assertRaises(ValueError):
            review.create_run(run["job_uuid"], BASE_COMMIT)
        with self.assertRaises(ValueError):
            review.fail_run(run["id"])
        generating = self.new_run()
        with self.assertRaises(ValueError):
            review.cancel_run(generating["id"], generating["revision"])
        failed = review.fail_run(generating["id"], "token=synthetic-secret " + "x" * 2000)
        self.assertEqual(failed["state"], "failed")
        self.assertNotIn("synthetic-secret", failed["error_detail"])
        self.assertLessEqual(len(failed["error_detail"]), 1000)
        with self.assertRaises(ValueError):
            review.mark_attention(failed["id"])

    def test_cancel_rejects_inflight_publish_and_release(self) -> None:
        run = self.publish_queued()
        claimed = review.claim_publish()
        with self.assertRaises(ValueError):
            review.cancel_run(run["id"], claimed["revision"])
        review.mark_attention(run["id"])
        run = self.release_queued()
        claimed = review.claim_release()
        with self.assertRaises(ValueError):
            review.cancel_run(run["id"], claimed["revision"])

    def test_simultaneous_publish_and_release_claims_have_one_winner(self) -> None:
        published = self.publish_queued()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda unused: review.claim_publish(), range(8)))
        winners = [result for result in results if result]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["id"], published["id"])
        releasing = self.release_queued()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda unused: review.claim_release(), range(8)))
        winners = [result for result in results if result]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["id"], releasing["id"])
        self.assertEqual(review.get_run(releasing["id"])["revision"], releasing["revision"] + 1)

    def test_concurrent_admin_approvals_reject_loser_as_stale(self) -> None:
        run, options = self.ready()

        def approve(option: dict) -> str:
            try:
                return review.approve_option(run["id"], option["id"], run["revision"], "1.0")["state"]
            except ValueError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(approve, options))
        self.assertCountEqual(results, ["publish_queued", "stale review revision"])
        self.assertEqual(review.get_run(run["id"])["revision"], run["revision"] + 1)

    def test_option_event_and_outbox_errors_roll_back_entire_transition(self) -> None:
        run = self.new_run()
        with patch.object(review, "_event", side_effect=RuntimeError("synthetic audit failure")):
            with self.assertRaises(RuntimeError):
                self.option(run)
        self.assertEqual(review.get_run(run["id"]), run)
        self.assertEqual(review.list_options(run["id"]), [])
        self.option(run)
        self.option(run, "second")
        before = review.get_run(run["id"])
        events = review.list_events(run["id"])
        with patch.object(review.uuid, "uuid4", side_effect=RuntimeError("synthetic outbox failure")):
            with self.assertRaises(RuntimeError):
                review.finish_generation(run["id"])
        self.assertEqual(review.get_run(run["id"]), before)
        self.assertEqual(review.list_events(run["id"]), events)
        self.assertEqual(review.notifications_due(), [])
        finished = review.finish_generation(run["id"])
        self.assertEqual(finished["state"], "awaiting_approval")

    def test_job_status_is_rechecked_inside_transaction(self) -> None:
        job_id = self.enqueue()
        maintainer.claim_next_job()
        original = maintainer.get_job

        def changed_job(row_uuid: str) -> dict:
            job = original(row_uuid)
            maintainer.finish_job(row_uuid, status="Cancelled", summary="Synthetic cancellation")
            return job

        with patch.object(maintainer, "get_job", side_effect=changed_job):
            with self.assertRaisesRegex(ValueError, "Running"):
                review.create_run(job_id, BASE_COMMIT)
        self.assertEqual(review.list_runs(), [])

    def test_approval_digest_binding_is_rechecked_before_claim(self) -> None:
        run = self.publish_queued()
        with maintainer._connect() as conn:
            conn.execute("UPDATE review_runs SET approved_diff_sha256 = ? WHERE id = ?", ("0" * 64, run["id"]))
        with self.assertRaisesRegex(ValueError, "binding"):
            review.claim_publish()
        self.assertEqual(review.get_run(run["id"])["state"], "publish_queued")

    def test_outbox_tracks_transitions_and_survives_module_reload(self) -> None:
        run = self.release_queued()
        review.claim_release()
        released = review.record_released(run["id"], MERGE_COMMIT, "v1.01")
        importlib.reload(review)
        self.assertEqual(review.get_run(run["id"]), released)
        due = review.notifications_due()
        self.assertEqual([item["kind"] for item in due], ["awaiting_approval", "testing", "released"])
        self.assertEqual(len({item["revision"] for item in due}), 3)
        self.assertTrue(all(item["run_id"] == run["id"] for item in due))
        self.assertNotIn("request_text", json.dumps(due))
        self.assertEqual(len(review.notifications_due(limit=1)), 1)
        with self.assertRaises(ValueError):
            review.notification_sent(due[0]["id"])
        first = review.claim_notification()
        self.assertEqual(first["id"], due[0]["id"])
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(first["status"], "sending")
        sent = review.notification_sent(first["id"])
        self.assertEqual(review.notification_sent(first["id"]), sent)
        self.assertEqual(sent["status"], "sent")
        self.assertEqual(len(review.notifications_due()), 2)
        self.assertEqual(review.get_run(run["id"]), released)

    def test_notification_claim_is_atomic(self) -> None:
        self.ready()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda unused: review.claim_notification(), range(8)))
        winners = [result for result in results if result]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["attempts"], 1)
        self.assertEqual(review.notifications_due(), [])

    def test_notification_failures_back_off_and_exhaust_five_attempts(self) -> None:
        self.ready()
        now = review.clock.local_now()
        with patch.object(review.clock, "local_now") as clock:
            for attempt in range(1, review.MAX_NOTIFICATION_ATTEMPTS + 1):
                clock.return_value = now + timedelta(hours=attempt)
                claimed = review.claim_notification()
                self.assertEqual(claimed["attempts"], attempt)
                with self.assertRaisesRegex(ValueError, "stale"):
                    review.notification_failed(claimed["id"], attempt - 1)
                failed = review.notification_failed(claimed["id"], attempt)
                self.assertIsNone(review.claim_notification())
                self.assertEqual(review.notifications_due(), [])
            self.assertEqual(failed["status"], "exhausted")
            clock.return_value = now + timedelta(days=10)
            self.assertIsNone(review.claim_notification())

    def test_notification_crash_lease_is_bounded_and_old_failure_cannot_reset_new_attempt(self) -> None:
        self.ready()
        now = review.clock.local_now()
        with patch.object(review.clock, "local_now") as clock:
            for attempt in range(1, review.MAX_NOTIFICATION_ATTEMPTS + 1):
                clock.return_value = now + timedelta(hours=attempt)
                claimed = review.claim_notification()
                self.assertEqual(claimed["attempts"], attempt)
                self.assertIsNone(review.claim_notification())
                if attempt > 1:
                    with self.assertRaises(ValueError):
                        review.notification_failed(claimed["id"], attempt - 1)
            clock.return_value = now + timedelta(days=2)
            self.assertEqual(review.notifications_due(), [])
            self.assertIsNone(review.claim_notification())
        with maintainer._connect() as conn:
            status = conn.execute("SELECT status FROM review_notifications WHERE id = ?", (claimed["id"],)).fetchone()[0]
        self.assertEqual(status, "exhausted")

    def test_event_revisions_are_complete_and_roles_are_generic(self) -> None:
        run = self.release_queued()
        review.claim_release()
        released = review.record_released(run["id"], MERGE_COMMIT, "v1.01")
        events = review.list_events(run["id"])
        self.assertEqual([event["revision"] for event in events], list(range(released["revision"], 0, -1)))
        self.assertEqual({event["actor_role"] for event in events}, {"admin", "worker"})
        self.assertEqual(len(review.list_events(run["id"], limit=2)), 2)
        with self.assertRaises(ValueError):
            review.list_events(run["id"], limit=101)
        with self.assertRaises(ValueError):
            review.list_options(run["id"], limit=101)
        with self.assertRaises(ValueError):
            review.notifications_due(limit=101)


if __name__ == "__main__":
    unittest.main()