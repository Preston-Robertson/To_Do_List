"""Offline review lifecycle integration through real HTTP routes and queue storage."""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import importlib.util
import os
from pathlib import Path
import socket
import sys
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
import uuid

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from luigi_web.paths import STATIC_DIR
from luigi_web.modules.feedback import maintainer, review, review_routes, routes, review_worker as worker
from luigi_web.modules.feedback import sandbox, test_preview
import test_feedback_review_worker as worker_fixtures


class Elements(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def select(self, tag, **attributes):
        return [attrs for element, attrs in self.elements if element == tag
                and all(attrs.get(key) == value for key, value in attributes.items())]


class FeedbackReviewIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.feedback_connect = routes.feedback._connect
        self.fixture = worker_fixtures.ReviewWorkerTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.root = self.fixture.root
        self.config = self.fixture.config
        self.enterContext(patch.dict(os.environ, {
            "LUIGI_WEB_UI_TOKEN": "synthetic-review-session",
            "LUIGI_WEB_MAINTAINER_REVIEW_ENABLED": "1",
            "LUIGI_WEB_RELEASE_ENABLED": "1",
            "LUIGI_MAINTAINER_UI_URL": "https://example.test",
            "LUIGI_WEB_FEEDBACK_DB": str(self.root / "production" / "feedback.sqlite3"),
            "LUIGI_WEB_MODULES": "feedback",
        }))
        original_connect = socket.socket.connect

        def local_socketpair_only(connection, address):
            frame = sys._getframe()
            while frame is not None:
                if (frame.f_globals.get("__name__") == "socket"
                        and frame.f_code.co_name in {"socketpair", "_socketpair", "_fallback_socketpair"}
                        and isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}):
                    return original_connect(connection, address)
                frame = frame.f_back
            raise AssertionError("External network access is forbidden")

        self.enterContext(patch.object(socket.socket, "connect", local_socketpair_only))
        for target in ("socket.getaddrinfo", "subprocess.Popen", "os.system"):
            self.enterContext(patch(target, side_effect=AssertionError("External operations are forbidden")))
        self.app = FastAPI()
        self.app.include_router(routes.router)
        self.app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
        self.app.mount("/module-assets/feedback", StaticFiles(
            directory=str(Path(review_routes.__file__).parent / "static")), name="feedback-assets")
        self.client = self.enterContext(TestClient(self.app, raise_server_exceptions=False))
        self.client.cookies.set("luigi_session", "synthetic-review-session")
        self.client.cookies.set("luigi_csrf", "synthetic-review-csrf")
        self.headers = {"x-csrf-token": "synthetic-review-csrf", "origin": "http://testserver"}

    def generate(self):
        result = self.fixture.generate()
        self.assertEqual(result.status, "awaiting_approval")
        run = review.get_run(result.run_id)
        options = review.list_options(result.run_id)
        self.assertEqual(len(options), 3)
        return run, options

    def detail_path(self, run):
        return f"/feedback/reviews/{run['id']}"

    def approval_form(self, run, option):
        return {"option_id": option["id"], "expected_revision": str(run["revision"]),
                "version": "1.01", "confirm": "on"}

    def release_form(self, run):
        return {"expected_revision": str(run["revision"]), "head_commit": run["head_commit"],
                "version": run["version"], "confirmed": "on"}

    def post(self, run, action, form, headers=None):
        return self.client.post(self.detail_path(run) + "/" + action, data=form,
                                headers=self.headers if headers is None else headers, follow_redirects=False)

    def approve(self, run, option):
        response = self.post(run, "approve", self.approval_form(run, option))
        self.assertEqual(response.status_code, 303)
        saved = review.get_run(run["id"])
        self.assertEqual(saved["state"], "publish_queued")
        self.assertEqual(saved["selected_option_id"], option["id"])
        self.assertEqual(saved["version"], "1.01")
        return saved

    def publish(self, run, option, events):
        def receipt(config, claimed, selected, path):
            self.assertEqual(config, self.config)
            self.assertTrue(path.is_dir())
            self.assertEqual(review.get_run(run["id"]), claimed)
            self.assertEqual(claimed["state"], "publishing")
            self.assertEqual(claimed["selected_option_id"], option["id"])
            self.assertEqual(claimed["version"], "1.01")
            self.assertEqual(claimed["base_commit"], self.fixture.base)
            self.assertEqual(selected, review.get_option(option["id"]))
            candidate = worker.load_candidate(config, claimed, selected)
            self.assertEqual(hashlib.sha256(candidate.patch).hexdigest(), selected["diff_sha256"])
            self.assertIsNone(claimed["head_commit"])
            self.assertIsNone(claimed["release_approved_head"])
            events.append("publish")
            return {"head_commit": "b" * 40, "branch": claimed["branch"],
                    "pr_url": "https://github.com/example/project/pull/1", "pr_number": 1}

        publisher = Mock(side_effect=receipt)
        self.assertEqual(self.fixture.publish(publisher=publisher).status, "testing")
        publisher.assert_called_once()
        saved = review.get_run(run["id"])
        self.assertEqual(saved["head_commit"], "b" * 40)
        self.assertEqual(saved["pr_url"], "https://github.com/example/project/pull/1")
        self.assertIsNone(saved["checks_commit"])
        self.assertIsNone(saved["preview_commit"])
        return saved

    def verify_ci(self, run, events):
        def verify(config, current):
            self.assertEqual(config, self.config)
            self.assertEqual(current, review.get_run(run["id"]))
            events.append("ci")
            return {"head_commit": current["head_commit"], "checks_passed": True}

        verifier = Mock(side_effect=verify)
        self.assertEqual(worker.refresh_once(self.config, verifier=verifier).status, "testing")
        verifier.assert_called_once()
        return review.get_run(run["id"])

    def verify_preview(self, run, events):
        def preview(config, current, selected):
            self.assertEqual(config, self.config)
            self.assertEqual(current, review.get_run(run["id"]))
            self.assertEqual(selected["id"], current["selected_option_id"])
            events.append("preview")
            return {"ready": True, "head_commit": current["head_commit"],
                    "preview_url": review.preview_path(run["id"])}

        gateway = Mock(side_effect=preview)
        self.assertEqual(worker.preview_once(self.config, run["id"], preview_runner=gateway).status, "testing")
        gateway.assert_called_once()
        return review.get_run(run["id"])

    def _testing_run(self):
        run, options = self.generate()
        approved = self.approve(run, options[0])
        return self.publish(approved, options[0], [])

    def host_app(self):
        name = "luigi_web._feedback_review_integration_host"
        path = Path(__file__).resolve().parents[1] / "luigi_web" / "application.py"
        specification = importlib.util.spec_from_file_location(name, path)
        if specification is None or specification.loader is None:
            raise AssertionError("The host source must have an executable module specification")
        host = importlib.util.module_from_spec(specification)
        self.enterContext(patch.dict(sys.modules, {name: host}))
        specification.loader.exec_module(host)
        return host

    def test_generation_serves_three_candidates_with_matching_artifact_digests(self):
        run, options = self.generate()
        response = self.client.get(self.detail_path(run))
        self.assertEqual(response.status_code, 200)
        elements = Elements(response.text)
        self.assertEqual({field["value"] for field in elements.select("input", name="option_id")},
                         {option["id"] for option in options})
        self.assertEqual(len({option["diff_sha256"] for option in options}), 3)
        image_ids = [image["artifact_id"] for option in options for image in option["screenshots"]]
        self.assertEqual(len(image_ids), 6)
        self.assertEqual(len(set(image_ids)), 6)
        self.assertTrue(set(image_ids).isdisjoint(option["artifact_id"] for option in options))
        image_sources = {image["src"] for image in elements.select("img")}
        for option in options:
            base = f"{self.detail_path(run)}/options/{option['id']}"
            diff = self.client.get(base + "/diff")
            self.assertEqual(diff.status_code, 200)
            self.assertEqual(hashlib.sha256(diff.content).hexdigest(), option["diff_sha256"])
            private = worker.load_candidate(self.config, run, option)
            self.assertEqual(private.patch_path.read_bytes(), diff.content)
            for filename, dimensions in worker.IMAGE_SIZES.items():
                with self.subTest(option=option["label"], image=filename):
                    image, = [image for image in option["screenshots"]
                              if (image["width"], image["height"]) == dimensions]
                    url = base + "/artifacts/" + filename
                    self.assertIn(url, image_sources)
                    served = self.client.get(url)
                    self.assertEqual(served.status_code, 200)
                    self.assertEqual(served.headers["content-type"], "image/png")
                    self.assertEqual(served.headers["cache-control"], "no-store")
                    self.assertEqual(hashlib.sha256(served.content).hexdigest(), image["sha256"])
                    self.assertEqual(worker.artifact_path(run["id"], image["artifact_id"], filename).read_bytes(),
                                     served.content)
        self.assertEqual(review.get_run(run["id"]), run)
        self.fixture.publisher.assert_not_called()

    def test_browser_approvals_are_required_before_publication_and_exact_release(self):
        run, options = self.generate()
        events = []
        publisher = Mock(side_effect=AssertionError("Unapproved publication"))

        def merge(config, current):
            self.assertEqual(config, self.config)
            self.assertEqual(current, review.get_run(run["id"]))
            self.assertEqual(current["state"], "releasing")
            self.assertEqual(current["head_commit"], "b" * 40)
            self.assertEqual(current["release_approved_head"], current["head_commit"])
            self.assertEqual(current["release_approved_version"], "1.01")
            self.assertEqual(current["expected_tag"], "v1.01")
            events.append("merge_tag")
            return {"merge_commit": "c" * 40, "tag": "v1.01"}

        releaser = Mock(side_effect=merge)
        for path in ("/feedback/reviews", self.detail_path(run)):
            self.assertEqual(self.client.get(path).status_code, 200)
        for action in ("approve", "release"):
            self.assertEqual(self.client.get(self.detail_path(run) + "/" + action).status_code, 405)
        self.assertEqual(review.get_run(run["id"]), run)
        self.assertEqual(self.fixture.publish(publisher=publisher).status, "Idle")
        self.assertEqual(worker.release_once(self.config, releaser=releaser).status, "Idle")
        publisher.assert_not_called()
        releaser.assert_not_called()
        self.assertEqual(events, [])

        run = self.approve(run, options[0])
        events.append("approve_post")
        run = self.publish(run, options[0], events)
        self.assertEqual(self.post(run, "release", self.release_form(run)).status_code, 409)
        self.assertEqual(review.get_run(run["id"]), run)
        run = self.verify_ci(run, events)
        self.assertEqual(run["checks_commit"], run["head_commit"])
        self.assertIsNone(run["preview_commit"])
        self.assertEqual(self.post(run, "release", self.release_form(run)).status_code, 409)
        run = self.verify_preview(run, events)
        self.assertEqual(run["preview_commit"], run["head_commit"])
        self.assertEqual(run["preview_url"], review.preview_path(run["id"]))
        self.assertEqual(self.client.get(self.detail_path(run)).status_code, 200)
        self.assertEqual(review.get_run(run["id"]), run)
        self.assertEqual(worker.release_once(self.config, releaser=releaser).status, "Idle")
        releaser.assert_not_called()
        for override, status in (({"confirmed": ""}, 400), ({"version": "1.02"}, 409),
                                 ({"head_commit": "f" * 40}, 409)):
            self.assertEqual(self.post(run, "release", {**self.release_form(run), **override}).status_code, status)
            self.assertEqual(review.get_run(run["id"]), run)
        self.assertEqual(self.post(run, "release", self.release_form(run)).status_code, 303)
        events.append("release_post")
        queued = review.get_run(run["id"])
        self.assertEqual(queued["state"], "release_queued")
        self.assertEqual(queued["release_approved_version"], "1.01")
        self.assertEqual(worker.release_once(self.config, releaser=releaser).status, "released")
        releaser.assert_called_once()
        released = review.get_run(run["id"])
        self.assertEqual((released["state"], released["version"], released["tag"], released["merge_commit"]),
                         ("released", "1.01", "v1.01", "c" * 40))
        self.assertEqual(worker.release_once(self.config, releaser=releaser).status, "Idle")
        self.assertEqual(self.fixture.publish(publisher=publisher).status, "Idle")
        self.assertEqual(events, ["approve_post", "publish", "ci", "preview", "release_post", "merge_tag"])
        publisher.assert_not_called()
        self.fixture.publisher.assert_not_called()

    def test_notification_contains_only_opaque_ids_and_fixed_copy_and_is_persisted(self):
        run, options = self.generate()
        notification, = review.notifications_due()
        notifier = Mock(return_value=True)
        result = worker.notify_once(notifier=notifier)
        self.assertEqual(result.status, "Notified")
        self.assertTrue(result.notification_sent)
        notifier.assert_called_once_with(
            {"uuid": run["id"], "feedback_uuid": run["id"], "notification_id": notification["id"]},
            status="awaiting_approval",
            summary="Options ready. Open Maintenance review in Feedback.\nhttps://example.test" + self.detail_path(run),
        )
        for value in notifier.call_args.args[0].values():
            self.assertEqual(str(uuid.UUID(value)), value)
        job = maintainer.get_job(run["job_uuid"])
        for private in (job["request_text"], job["acceptance_criteria"], job["uuid"], job["feedback_uuid"],
                        str(self.root), *(option["artifact_id"] for option in options)):
            self.assertNotIn(private, repr(notifier.call_args))
        self.assertEqual(review.notifications_due(), [])
        with maintainer._connect() as connection:
            saved = dict(connection.execute("SELECT * FROM review_notifications WHERE id = ?",
                                            (notification["id"],)).fetchone())
        self.assertEqual((saved["status"], saved["attempts"], saved["lease_until"]), ("sent", 1, None))
        self.assertEqual(review.notification_sent(notification["id"]), saved)
        self.assertEqual(worker.notify_once(notifier=notifier).status, "Idle")
        notifier.assert_called_once()
        self.assertEqual(review.get_run(run["id"]), run)

    def test_missing_fields_csrf_and_stale_approval_never_queue_publication(self):
        run, options = self.generate()
        form = self.approval_form(run, options[0])
        for missing in form:
            with self.subTest(missing=missing):
                self.assertEqual(self.post(run, "approve", {key: value for key, value in form.items()
                                                          if key != missing}).status_code, 400)
                self.assertEqual(review.get_run(run["id"]), run)
        for headers in ({}, {**self.headers, "origin": "https://elsewhere.test"},
                        {**self.headers, "x-csrf-token": "synthetic-wrong-csrf"}):
            self.assertEqual(self.post(run, "approve", form, headers=headers).status_code, 403)
            self.assertEqual(review.get_run(run["id"]), run)
        self.assertEqual(self.post(run, "approve", {**form, "expected_revision": str(run["revision"] + 1)}).status_code, 412)
        publisher = Mock()
        self.assertEqual(self.fixture.publish(publisher=publisher).status, "Idle")
        publisher.assert_not_called()
        approved = self.approve(run, options[0])
        self.assertEqual(self.post(run, "approve", form).status_code, 412)
        self.assertEqual(review.get_run(run["id"]), approved)
        publisher.assert_not_called()

    def test_changed_head_revokes_preview_and_queued_release_and_rejects_stale_form(self):
        run = self._testing_run()
        run = self.verify_ci(run, [])
        run = self.verify_preview(run, [])
        stale_form = self.release_form(run)
        self.assertEqual(self.post(run, "release", stale_form).status_code, 303)
        self.assertEqual(review.get_run(run["id"])["release_approved_head"], "b" * 40)
        changed = review.record_head(run["id"], "c" * 40)
        for field in ("checks_commit", "preview_commit", "preview_url", "release_approved_head",
                      "release_approved_version", "release_approved_at"):
            self.assertIsNone(changed[field], field)
        self.assertEqual(self.post(changed, "release", stale_form).status_code, 412)
        self.assertEqual(review.get_run(run["id"]), changed)
        self.assertEqual(self.client.get(review.preview_path(run["id"])).status_code, 409)
        page = self.client.get(self.detail_path(changed))
        self.assertEqual(page.status_code, 200)
        self.assertFalse(Elements(page.text).select("input", name="head_commit"))
        releaser = Mock()
        self.assertEqual(worker.release_once(self.config, releaser=releaser).status, "Idle")
        releaser.assert_not_called()
        verifier = Mock(return_value={"head_commit": "d" * 40, "checks_passed": True})
        self.assertEqual(worker.refresh_once(self.config, verifier=verifier).status, "needs_attention")
        verifier.assert_called_once()
        saved = review.get_run(run["id"])
        self.assertEqual(saved["head_commit"], "d" * 40)
        self.assertIsNone(saved["checks_commit"])
        self.assertIsNone(saved["preview_commit"])
        self.assertEqual(worker.release_once(self.config, releaser=releaser).status, "Idle")
        releaser.assert_not_called()

    def test_preview_entry_posts_single_use_ticket_to_dedicated_origin(self):
        run = self._testing_run()
        run = self.verify_preview(run, [])
        instance = str(uuid.uuid4())
        state = {"run_id": run["id"], "head_commit": run["head_commit"], "instance": instance,
                 "source_digest": review.get_option(run["selected_option_id"])["tree_sha256"],
                 "container_name": "luigi-preview-" + instance.replace("-", ""),
                 "expires": int(time.time()) + 3500}
        key = "ab" * 32
        self.enterContext(patch.dict(os.environ, {
            "LUIGI_MAINTAINER_PREVIEW_URL": "https://preview.example.test",
            "LUIGI_MAINTAINER_PREVIEW_GATEWAY_KEY": key,
        }))
        self.enterContext(patch.object(test_preview, "_active", return_value=state))
        with patch.object(test_preview, "create_ticket", wraps=test_preview.create_ticket) as creator:
            response = self.client.get(review.preview_path(run["id"]))
        self.assertEqual(response.status_code, 200)
        creator.assert_called_once_with(run)
        elements = Elements(response.text)
        form, = elements.select("form", action="https://preview.example.test/session")
        self.assertEqual(form["method"].lower(), "post")
        self.assertEqual(urlsplit(form["action"]).query, "")
        ticket_field, = elements.select("input", name="ticket")
        ticket = ticket_field["value"]
        self.assertEqual(ticket_field["type"], "hidden")
        self.assertEqual(elements.select("input", name="run_id")[0]["value"], run["id"])
        for attribute in elements.elements:
            for field in ("href", "src", "action"):
                self.assertNotIn(ticket, attribute[1].get(field, ""))
        self.assertNotIn(key, response.text)
        self.assertNotIn("synthetic-review-session", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("form-action https://preview.example.test", response.headers["content-security-policy"])
        with maintainer._connect() as connection:
            record, = connection.execute("SELECT * FROM preview_tickets").fetchall()
        self.assertEqual(record["digest"], hashlib.sha256(ticket.encode()).hexdigest())
        self.assertNotIn(ticket, repr(dict(record)))
        with patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": ""}):
            gateway_app = test_preview.gateway_app()
        gateway = self.enterContext(TestClient(gateway_app, base_url="https://preview.example.test"))
        body = {"run_id": run["id"], "ticket": ticket}
        wrong_origin = gateway.post(form["action"], data=body, headers={"origin": "https://elsewhere.test"})
        self.assertEqual(wrong_origin.status_code, 403)
        result = gateway.post(form["action"], data=body, headers={"origin": "https://example.test"})
        self.assertEqual(result.status_code, 200)
        for private in (ticket, key, "synthetic-review-session"):
            self.assertNotIn(private, result.text)
        self.assertNotIn("location", result.headers)
        for flag in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
            self.assertIn(flag, result.headers["set-cookie"])
        self.assertNotIn("Domain=", result.headers["set-cookie"])
        self.assertIsNone(gateway.cookies.get("luigi_session"))
        self.assertEqual(self.client.cookies.get("luigi_session"), "synthetic-review-session")
        self.assertEqual(gateway.post(form["action"], data=body,
                                     headers={"origin": "https://example.test"}).status_code, 403)
        with maintainer._connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM preview_tickets").fetchone()[0], 0)
        self.assertEqual(review.get_run(run["id"]), run)
        review.record_head(run["id"], "e" * 40)
        with patch.object(test_preview, "create_ticket") as creator:
            self.assertEqual(self.client.get(review.preview_path(run["id"])).status_code, 409)
        creator.assert_not_called()

    def test_anonymous_requests_cannot_read_inbox_reviews_or_generated_artifacts(self):
        run, options = self.generate()
        base = self.detail_path(run) + f"/options/{options[0]['id']}"
        self.client.cookies.clear()
        with patch.object(review_routes, "_get_run", side_effect=AssertionError("Auth must precede storage")), patch.object(
            review_routes, "_artifact_bytes", side_effect=AssertionError("Auth must precede artifact reads")
        ), patch.object(test_preview, "create_ticket") as ticket:
            for path in ("/feedback", "/feedback/new", "/feedback/reviews", self.detail_path(run),
                         review.preview_path(run["id"]), base + "/diff",
                         base + "/artifacts/desktop.png", base + "/artifacts/mobile.png"):
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 401)
            self.assertEqual(self.post(run, "approve", self.approval_form(run, options[0])).status_code, 401)
            ticket.assert_not_called()
        self.assertEqual(review.get_run(run["id"]), run)
        self.fixture.publisher.assert_not_called()

    def test_feedback_creation_requires_privacy_confirmation_before_worker_consumption(self):
        host = self.host_app()
        client = TestClient(host.app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        client.cookies.set("luigi_session", "synthetic-review-session")
        client.cookies.set("luigi_csrf", "synthetic-review-csrf")
        with patch.object(routes.feedback, "_connect", self.feedback_connect):
            payload = {"category": "Idea", "message": "Improve synthetic keyboard navigation.", "page_path": "/tasks"}
            self.assertEqual(client.post("/feedback", data=payload).status_code, 403)
            self.assertEqual(client.post("/feedback", data=payload, headers=self.headers).status_code, 204)
            item, = routes.feedback.list_items()
            self.assertEqual(item["status"], "New")
            before = maintainer.jobs_by_feedback()
            path = f"/feedback/{item['uuid']}/approve"
            criteria = "Keep the synthetic workflow keyboard accessible."
            self.assertEqual(client.post(path, data={"acceptance_criteria": criteria},
                                         headers=self.headers).status_code, 422)
            self.assertEqual(maintainer.jobs_by_feedback(), before)
            self.assertEqual(routes.feedback.get_item(item["uuid"])["status"], "New")
            form = {"privacy_confirmed": "on", "acceptance_criteria": criteria}
            self.assertEqual(client.post(path, data=form, headers=self.headers).status_code, 204)
            jobs = maintainer.jobs_by_feedback()
            self.assertEqual(set(jobs), {*before, item["uuid"]})
            queued = jobs[item["uuid"]]
            self.assertEqual(queued["status"], "Queued")
            self.assertEqual(queued["request_text"], payload["message"])
            self.assertEqual(queued["acceptance_criteria"], criteria)
            self.assertEqual(routes.feedback.get_item(item["uuid"])["status"], "Planned")
            self.assertEqual(client.post(path, data=form, headers=self.headers).status_code, 409)
        initial = maintainer.claim_next_job()
        self.assertEqual(initial["uuid"], self.fixture.job_id)
        self.fixture.job_id = queued["uuid"]
        run, options = self.generate()
        self.assertEqual(run["job_uuid"], queued["uuid"])
        self.assertEqual(self.client.get(self.detail_path(run)).status_code, 200)
        self.assertEqual(len(options), 3)
        self.fixture.publisher.assert_not_called()

    def test_private_public_and_applied_patch_tampering_block_publication(self):
        for index, kind in enumerate(("private", "public", "applied")):
            with self.subTest(kind=kind):
                if index:
                    self.fixture.job_id = maintainer.enqueue_feedback({
                        "uuid": str(uuid.uuid4()), "category": "Idea", "page_path": "/tasks",
                        "message": "Improve the synthetic example layout.",
                    }, "Preserve synthetic keyboard access.")
                    self.fixture.jobs.clear()
                run, options = self.generate()
                run = self.approve(run, options[0])
                candidate = worker.load_candidate(self.config, run, options[0])
                public_path = worker.artifact_path(run["id"], options[0]["artifact_id"], "candidate.patch")
                bad_patch = b"diff --git a/templates/example.html b/templates/example.html\n+synthetic tampering\n"
                self.assertNotEqual(hashlib.sha256(bad_patch).hexdigest(), options[0]["diff_sha256"])
                publisher = Mock()
                self.fixture.steps.clear()
                overrides = {}
                if kind == "applied":
                    overrides["patch_reader"] = Mock(return_value=bad_patch)
                else:
                    target = candidate.patch_path if kind == "private" else public_path
                    target.chmod(0o600)
                    target.write_bytes(bad_patch)
                if kind == "public":
                    self.assertEqual(self.client.get(self.detail_path(run) +
                                     f"/options/{options[0]['id']}/diff").status_code, 404)
                self.assertEqual(self.fixture.publish(publisher=publisher, **overrides).status, "needs_attention")
                publisher.assert_not_called()
                saved = review.get_run(run["id"])
                self.assertEqual(saved["state"], "needs_attention")
                self.assertIsNone(saved["head_commit"])
                self.assertIsNone(saved["pr_url"])
                if kind != "applied":
                    self.assertEqual(self.fixture.steps, [])
                else:
                    self.assertEqual(self.fixture.steps[-1], "cleanup")

    def test_legacy_cli_dispatches_review_worker_and_preview_roles_use_pending_store(self):
        with patch.object(worker, "main", wraps=worker.main) as dispatch, patch.object(
            worker, "generation_once", return_value=worker.WorkerResult("Idle")
        ) as generation, patch.object(worker, "_role_config", return_value=self.config), patch.object(
            worker.legacy, "run_once", side_effect=AssertionError("Legacy worker must not run")
        ) as legacy, patch.object(sys, "argv", ["luigi-maintainer", "--generate"]), patch("builtins.print"):
            self.assertEqual(worker.legacy.main(), 0)
        dispatch.assert_called_once_with()
        generation.assert_called_once_with(self.config)
        legacy.assert_not_called()
        run = self._testing_run()
        receipt = {"ready": True, "head_commit": run["head_commit"], "preview_url": review.preview_path(run["id"])}
        with patch.object(test_preview, "start_preview", return_value=receipt) as start, patch.object(
            test_preview.sandbox, "available", return_value=sandbox.Availability(True, "Synthetic runtime")
        ) as available, patch.object(worker, "_role_config", return_value=self.config) as config, patch("builtins.print") as output:
            self.assertEqual(worker.main(["--preview-pending"]), 0)
            start.assert_called_once()
            self.assertEqual(start.call_args.args, (self.config, run, review.get_option(run["selected_option_id"])))
            self.assertEqual(review.get_run(run["id"])["preview_commit"], run["head_commit"])
            self.assertEqual(worker.main(["--preview", run["id"]]), 0)
            self.assertEqual(start.call_count, 2)
            self.assertEqual(worker.main(["--preview-pending"]), 0)
            self.assertEqual(start.call_count, 2)
            output.assert_called_with("Review worker status: Idle")
            self.assertEqual(config.call_count, 2)
            config.assert_called_with("preview")
            available.return_value = sandbox.Availability(False, "Synthetic unavailable runtime")
            self.assertEqual(worker.main(["--preview", run["id"]]), 2)
            self.assertEqual(start.call_count, 2)

    def test_host_mounts_manifest_review_routes_without_startup_and_flags_stay_protected(self):
        from luigi_web.modules.admin import environment
        from luigi_web.modules.feedback.manifest import module

        flags = {"LUIGI_WEB_MAINTAINER_REVIEW_ENABLED", "LUIGI_WEB_RELEASE_ENABLED"}
        self.assertTrue(flags.isdisjoint(key.name for key in environment.KNOWN_KEYS))
        with patch.dict(os.environ, {flag: "0" for flag in flags}):
            self.assertFalse(review_routes.review_enabled())
            self.assertFalse(review_routes.release_enabled())
        self.assertTrue(review_routes.review_enabled())
        self.assertTrue(review_routes.release_enabled())
        host = self.host_app()
        self.assertEqual([item.id for item in host.app.state.modules.enabled], ["feedback"])
        self.assertEqual(module.router, "luigi_web.modules.feedback.routes:router")
        expected = {(route.path, method) for route in review_routes.router.routes for method in route.methods}
        mounted = [(route.path, method) for route in host.app.routes if hasattr(route, "methods")
                   for method in route.methods]
        for declaration in expected:
            self.assertEqual(mounted.count(declaration), 1, declaration)
        client = TestClient(host.app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        with patch.object(host, "start_modules", side_effect=AssertionError("No startup hooks")) as startup:
            self.assertEqual(client.get("/feedback/reviews").status_code, 401)
            client.cookies.set("luigi_session", "synthetic-review-session")
            client.cookies.set("luigi_csrf", "synthetic-review-csrf")
            self.assertEqual(client.get("/feedback/reviews").status_code, 200)
            self.assertEqual(client.post("/feedback", data={}).status_code, 403)
            startup.assert_not_called()

    def test_source_export_keeps_reviewed_code_and_workflows_but_excludes_internal_docs(self):
        source = self.root / "synthetic-source"
        source.mkdir()
        allowed = {
            ".github/workflows/tests.yml", "module-repos/feedback/.github/workflows/tests.yaml",
            "app.py", "pyproject.toml", "luigi_web/core/example.py",
            "module-repos/feedback/src/luigi_web/modules/feedback/example.py", "tests/test_example.py",
        }
        denied = {"docs/private/internal.md", "LOCAL_DEPLOYMENT.md", "AGENTS.md", ".github/agents/reviewer.md",
                  "module-repos/feedback/.github/agents/reviewer.md", "data/synthetic.txt", ".env.example"}
        for relative in allowed | denied:
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("Synthetic fixture: " + relative + "\n", encoding="ascii")
        destination = self.root / "exported-source"
        with patch.object(sandbox, "_listed_files", return_value=sorted(allowed | denied)) as files:
            snapshot = sandbox.export_candidate(source, destination)
        files.assert_called_once_with(source)
        exported = {path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()}
        self.assertEqual(exported, allowed)
        expected_digest = hashlib.sha256()
        for relative in sorted(allowed):
            content = (source / relative).read_bytes()
            self.assertEqual((destination / relative).read_bytes(), content)
            expected_digest.update(relative.encode("ascii") + b"\0" + hashlib.sha256(content).digest())
        self.assertEqual(snapshot.source_digest, expected_digest.hexdigest())
        self.assertEqual(snapshot.files, len(allowed))


if __name__ == "__main__":
    unittest.main()
