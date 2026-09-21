"""Review HTTP boundary tests using only synthetic jobs and disposable storage."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid
import zlib

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from luigi_web.paths import STATIC_DIR
from luigi_web.modules.feedback import maintainer, review, review_routes

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


def synthetic_png(width=1440, height=900):
    def chunk(kind, content):
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))

    pixels = (b"\x00" + b"\x80\xa0\xb0" * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


class FeedbackReviewRouteTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        self.root = Path(storage.name)
        self.enterContext(patch("luigi_web.modules.feedback.test_preview.verify_ready", return_value=True))
        environment = {key: os.environ[key] for key in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH")
                       if key in os.environ}
        browser_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        if not browser_path and os.environ.get("LOCALAPPDATA"):
            browser_path = str(Path(os.environ["LOCALAPPDATA"]) / "ms-playwright")
        if browser_path:
            environment["PLAYWRIGHT_BROWSERS_PATH"] = browser_path
        environment.update({
            "LUIGI_WEB_UI_TOKEN": "synthetic-review-session",
            "LUIGI_WEB_MAINTAINER_DB": str(self.root / "queue.sqlite3"),
            "LUIGI_WEB_FEEDBACK_DB": str(self.root / "never-open.sqlite3"),
            "LUIGI_MAINTAINER_ARTIFACT_DIR": str(self.root / "artifacts"),
            "LUIGI_WEB_MAINTAINER_REVIEW_ENABLED": "1",
            "LUIGI_WEB_RELEASE_ENABLED": "1",
            **{key: str(self.root) for key in ("TEMP", "TMP", "SQLITE_TMPDIR", "APPDATA", "LOCALAPPDATA")},
        })
        env_patch = patch.dict(os.environ, environment, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        inbox_guard = patch.object(maintainer.feedback, "get_item", side_effect=AssertionError("raw inbox read"))
        inbox_guard.start()
        self.addCleanup(inbox_guard.stop)
        self.app = FastAPI()
        self.app.include_router(review_routes.router)
        self.app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
        self.app.mount("/module-assets/feedback", StaticFiles(
            directory=str(Path(review_routes.__file__).parent / "static")), name="feedback-assets")
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.client.cookies.set("luigi_session", "synthetic-review-session")
        self.client.cookies.set("luigi_csrf", "synthetic-csrf")
        self.headers = {"x-csrf-token": "synthetic-csrf", "origin": "http://testserver"}

    def ready(self, label_override=None, screenshot_content=None, option_count=2):
        job_id = maintainer.enqueue_feedback({
            "uuid": str(uuid.uuid4()), "category": "Idea", "message": "Synthetic improvement",
            "page_path": "/feedback",
        }, "Synthetic acceptance criteria")
        maintainer.claim_next_job()
        run = review.create_run(job_id, "a" * 40)
        options = []
        for label in ("Compact layout", "Expanded layout", "Balanced layout")[:option_count]:
            content = screenshot_content or synthetic_png()
            mobile_content = synthetic_png(390, 844)
            patch_content = f"--- a/synthetic.txt\n+++ b/synthetic.txt\n@@ -1 +1 @@\n-before\n+{label}\n".encode()
            artifact_id = str(uuid.uuid4())
            options.append(review.add_option(
                run["id"], label=label_override or label, summary="Synthetic reviewed summary",
                diff_sha256=hashlib.sha256(patch_content).hexdigest(), artifact_id=artifact_id,
                test_results=[{"command_id": command, "passed": True, "total": 12,
                               "failed": 0, "skipped": 0, "output_digest": "d" * 64}
                              for command in sorted(review.REQUIRED_CHECKS)],
                screenshots=[{"artifact_id": str(uuid.uuid4()), "sha256": hashlib.sha256(content).hexdigest(),
                              "width": 1440, "height": 900},
                             {"artifact_id": str(uuid.uuid4()), "sha256": hashlib.sha256(mobile_content).hexdigest(),
                              "width": 390, "height": 844}],
            ))
            directory = self.root / "artifacts" / run["id"] / artifact_id
            directory.mkdir(parents=True)
            (directory / "candidate.patch").write_bytes(patch_content)
            for image in options[-1]["screenshots"]:
                filename, image_content = ("desktop.png", content) if image["width"] == 1440 else ("mobile.png", mobile_content)
                image_directory = directory.parent / image["artifact_id"]
                image_directory.mkdir()
                (image_directory / filename).write_bytes(image_content)
        return review.finish_generation(run["id"]), options

    def approval(self, run, option):
        return {"option_id": option["id"], "expected_revision": str(run["revision"]),
                "version": "1.01", "confirm": "on"}

    def post(self, run, action, form, **kwargs):
        return self.client.post(f"/feedback/reviews/{run['id']}/{action}", data=form,
                                headers=kwargs.pop("headers", self.headers), follow_redirects=False, **kwargs)

    def test_approval_commits_exact_choice_and_literal_version(self):
        run, options = self.ready()
        response = self.post(run, "approve", self.approval(run, options[1]))
        self.assertEqual(response.status_code, 303)
        saved = review.get_run(run["id"])
        self.assertEqual(saved["state"], "publish_queued")
        self.assertEqual(saved["selected_option_id"], options[1]["id"])
        self.assertEqual(saved["version"], "1.01")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_auth_csrf_origin_and_flags_fail_closed(self):
        run, options = self.ready()
        form = self.approval(run, options[0])
        for headers in ({}, {**self.headers, "origin": "https://elsewhere.test"},
                        {**self.headers, "sec-fetch-site": "cross-site"},
                        {"x-csrf-token": "synthetic-csrf", "referer": "http://[invalid"}):
            self.assertEqual(self.post(run, "approve", form, headers=headers).status_code, 403)
        with patch.dict(os.environ, {"LUIGI_WEB_MAINTAINER_REVIEW_ENABLED": "0"}):
            self.assertEqual(self.post(run, "approve", form).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.post(run, "approve", form).status_code, 401)
        self.assertEqual(review.get_run(run["id"]), run)

    def test_confirmation_unknown_fields_and_staleness_do_not_mutate(self):
        run, options = self.ready()
        form = self.approval(run, options[0])
        for invalid in ({**form, "confirm": ""}, {**form, "worker": "ready"}, {**form, "version": "v1.01"},
                {**form, "version": "123.123.123"}):
            self.assertEqual(self.post(run, "approve", invalid).status_code, 400)
        self.assertEqual(self.post(run, "approve", {**form, "expected_revision": "1"}).status_code, 412)
        self.assertEqual(review.get_run(run["id"]), run)

    def test_form_size_duplicates_and_files_are_rejected(self):
        run, options = self.ready()
        path = f"/feedback/reviews/{run['id']}/approve"
        headers = {**self.headers, "content-type": "application/x-www-form-urlencoded"}
        for content, status in (("confirm=on&confirm=on", 400), ("field=" + "x" * 16384, 413),
                                ("&".join(f"field{index}=x" for index in range(11)), 400)):
            self.assertEqual(self.client.post(path, content=content, headers=headers).status_code, status)
        response = self.client.post(path, files={"file": ("synthetic.txt", b"synthetic")}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(review.get_run(run["id"]), run)

    def test_bearer_clients_work_without_browser_csrf(self):
        run, options = self.ready()
        self.client.cookies.clear()
        response = self.post(run, "approve", self.approval(run, options[0]),
                             headers={"authorization": "Bearer synthetic-review-session"})
        self.assertEqual(response.status_code, 303)

    def test_worker_receipts_and_get_approvals_are_not_routes(self):
        run, options = self.ready()
        for action in ("record_preview", "record_test_result", "record_head", "claim_release", "_refresh"):
            self.assertEqual(self.post(run, action, {}).status_code, 404)
        for action in ("approve", "release", "cancel"):
            self.assertEqual(self.client.get(f"/feedback/reviews/{run['id']}/{action}").status_code, 405)
        self.assertEqual(review.get_run(run["id"]), run)

    def _testing_run(self, ready=True):
        run, options = self.ready()
        review.approve_option(run["id"], options[0]["id"], run["revision"], "1.01")
        review.claim_publish()
        run = review.record_published(run["id"], "b" * 40, run["branch"],
                                      "https://github.com/example/project/pull/7", 7)
        if ready:
            review.record_preview(run["id"], "b" * 40, review.preview_path(run["id"]))
            run = review.record_test_result(run["id"], "b" * 40, True)
        return run

    def release_form(self, run):
        return {"expected_revision": str(run["revision"]), "head_commit": run["head_commit"],
                "version": run["version"], "confirmed": "on"}

    def test_release_requires_exact_head_version_confirmation_and_both_flags(self):
        run = self._testing_run()
        form = self.release_form(run)
        for values, status in (({"head_commit": "c" * 40}, 409), ({"version": "1.02"}, 409),
                               ({"confirmed": ""}, 400), ({"expected_revision": "1"}, 412)):
            self.assertEqual(self.post(run, "release", {**form, **values}).status_code, status)
        for flag in ("LUIGI_WEB_MAINTAINER_REVIEW_ENABLED", "LUIGI_WEB_RELEASE_ENABLED"):
            with patch.dict(os.environ, {flag: "0"}):
                self.assertEqual(self.post(run, "release", form).status_code, 403)
        self.assertEqual(review.get_run(run["id"]), run)
        self.assertEqual(self.post(run, "release", form).status_code, 303)
        saved = review.get_run(run["id"])
        self.assertEqual(saved["state"], "release_queued")
        self.assertEqual(saved["release_approved_head"], "b" * 40)
        self.assertEqual(saved["release_approved_version"], "1.01")

    def test_release_cannot_invent_preview_or_checks_readiness(self):
        run = self._testing_run(ready=False)
        self.assertEqual(self.post(run, "release", self.release_form(run)).status_code, 409)
        page = self.client.get(f"/feedback/reviews/{run['id']}")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Open test application", page.text)
        self.assertNotIn('name="head_commit"', page.text)
        review.record_preview(run["id"], "b" * 40, review.preview_path(run["id"]))
        run = review.get_run(run["id"])
        self.assertEqual(self.post(run, "release", self.release_form(run)).status_code, 409)

    def test_expired_preview_rejects_release_without_changing_state(self):
        run = self._testing_run()
        with patch("luigi_web.modules.feedback.test_preview.verify_ready", side_effect=ValueError("Synthetic expired preview")):
            self.assertEqual(self.post(run, "release", self.release_form(run)).status_code, 409)
        self.assertEqual(review.get_run(run["id"]), run)

    def test_changed_head_revokes_displayed_release_and_old_submission(self):
        run = self._testing_run()
        page = self.client.get(f"/feedback/reviews/{run['id']}")
        self.assertIn(f'href="{review.preview_path(run["id"])}"', page.text)
        self.assertIn(f'name="head_commit" value="{"b" * 40}"', page.text)
        review.record_head(run["id"], "c" * 40)
        self.assertEqual(self.post(run, "release", self.release_form(run)).status_code, 412)
        changed = self.client.get(f"/feedback/reviews/{run['id']}")
        self.assertNotIn('name="head_commit"', changed.text)
        self.assertNotIn("Open test application", changed.text)

    def test_cancel_requires_fresh_confirmation_and_is_not_replayed(self):
        run, options = self.ready()
        form = {"expected_revision": str(run["revision"]), "confirm": "on"}
        self.assertEqual(self.post(run, "cancel", {**form, "confirm": ""}).status_code, 400)
        self.assertEqual(self.post(run, "cancel", form).status_code, 303)
        self.assertEqual(review.get_run(run["id"])["state"], "cancelled")
        self.assertEqual(self.post(run, "cancel", form).status_code, 412)

    def test_foreign_option_and_conflicting_store_receipt_are_generic(self):
        run, options = self.ready()
        other, foreign = self.ready()
        self.assertEqual(self.post(run, "approve", self.approval(run, foreign[0])).status_code, 409)
        with patch.object(review, "approve_option", side_effect=ValueError("internal detail must not appear")):
            response = self.post(run, "approve", self.approval(run, options[0]))
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("internal detail", response.text)
        self.assertEqual(review.get_run(run["id"]), run)

    def test_authenticated_reads_are_nonmutating_and_disabled_pages_have_no_actions(self):
        run, options = self.ready()
        with patch.dict(os.environ, {"LUIGI_WEB_MAINTAINER_REVIEW_ENABLED": "0"}):
            for path in ("/feedback/reviews", f"/feedback/reviews/{run['id']}"):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("disabled by deployment configuration", response.text)
                self.assertNotIn('data-review-form', response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(review.get_run(run["id"]), run)
        self.assertFalse((self.root / "never-open.sqlite3").exists())

    def test_safe_metadata_escaped_no_private_artifact_or_raw_state(self):
        run, options = self.ready(label_override='<img src=x onerror="alert(1)">')
        response = self.client.get(f"/feedback/reviews/{run['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("&lt;img", response.text)
        self.assertNotIn('<img src=x', response.text)
        for option in options:
            self.assertNotIn(option["artifact_id"], response.text)
        self.assertNotIn(run["job_uuid"], response.text)
        self.assertIn("12 total, 0 failed, 0 skipped", response.text)
        self.assertNotIn('value="1.0"', response.text)
        self.assertNotIn(str(self.root), response.text)

    def test_reads_artifacts_and_email_links_require_auth_not_query_capability(self):
        run, options = self.ready()
        self.client.cookies.clear()
        for path in ("/feedback/reviews", f"/feedback/reviews/{run['id']}",
                     f"/feedback/reviews/{run['id']}/options/{options[0]['id']}/diff",
                     f"/feedback/reviews/{run['id']}/options/{options[0]['id']}/artifacts/desktop.png"):
            self.assertEqual(self.client.get(path).status_code, 401)
        response = self.client.get(f"/feedback/reviews/{run['id']}?token=synthetic-review-session")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(review.get_run(run["id"]), run)

    def test_unknown_ids_and_storage_errors_do_not_leak_details(self):
        for identifier in ("invalid", str(uuid.uuid4())):
            self.assertEqual(self.client.get(f"/feedback/reviews/{identifier}").status_code, 404)
        with patch.object(review, "list_runs", side_effect=sqlite3.OperationalError("private storage location")):
            response = self.client.get("/feedback/reviews")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private storage", response.text)

    def artifact_url(self, run, option, suffix="artifacts/desktop.png"):
        return f"/feedback/reviews/{run['id']}/options/{option['id']}/{suffix}"

    def artifact_file(self, run, option, filename):
        image = review_routes._image_metadata(option, filename)
        identifier = image["artifact_id"] if image else option["artifact_id"]
        return self.root / "artifacts" / run["id"] / identifier / filename

    def test_png_and_patch_are_digest_checked_and_sent_without_cache(self):
        run, options = self.ready()
        for suffix, media in (("artifacts/desktop.png", "image/png"), ("artifacts/mobile.png", "image/png"),
                      ("diff", "text/plain")):
            response = self.client.get(self.artifact_url(run, options[0], suffix))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith(media))
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertIn("sandbox", response.headers["content-security-policy"])
        for filename, suffix in (("desktop.png", "artifacts/desktop.png"), ("candidate.patch", "diff")):
            self.artifact_file(run, options[0], filename).write_bytes(b"changed")
            self.assertEqual(self.client.get(self.artifact_url(run, options[0], suffix)).status_code, 404)

    def test_invalid_png_header_dimensions_trailing_data_and_crc_are_rejected(self):
        for content in (b"not an image", synthetic_png(390, 844), synthetic_png() + b"<script>bad</script>",
                        synthetic_png()[:-4] + b"xxxx"):
            run, options = self.ready(screenshot_content=content)
            self.assertEqual(self.client.get(self.artifact_url(run, options[0])).status_code, 404)

    def test_artifact_ownership_allowlist_symlinks_and_size_limits(self):
        run, options = self.ready()
        other, foreign = self.ready()
        self.assertEqual(self.client.get(self.artifact_url(run, foreign[0])).status_code, 404)
        for name in ("candidate.patch", "private.png", "..%5cdesktop.png"):
            self.assertEqual(self.client.get(self.artifact_url(run, options[0], f"artifacts/{name}")).status_code, 404)
        with patch.object(Path, "is_symlink", return_value=True):
            self.assertEqual(self.client.get(self.artifact_url(run, options[0])).status_code, 404)
        for filename, size, suffix in (("desktop.png", 8 * 1024 * 1024 + 1, "artifacts/desktop.png"),
                                       ("candidate.patch", 1_200_001, "diff")):
            self.artifact_file(run, options[0], filename).write_bytes(b"x" * size)
            self.assertEqual(self.client.get(self.artifact_url(run, options[0], suffix)).status_code, 404)

    def _browser_page(self):
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch()
        self.addCleanup(browser.close)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        self.addCleanup(context.close)
        context.add_cookies([
            {"name": "luigi_session", "value": "synthetic-review-session", "url": "http://testserver"},
            {"name": "luigi_csrf", "value": "synthetic-csrf", "url": "http://testserver"},
        ])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))

        def respond(route):
            request = route.request
            if urlsplit(request.url).netloc != "testserver":
                route.abort()
                return
            response = self.client.request(request.method, request.url, headers=request.all_headers(),
                                           content=request.post_data_buffer, follow_redirects=False)
            headers = dict(response.headers)
            for name in ("content-length", "content-encoding", "transfer-encoding"):
                headers.pop(name, None)
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("**/*", respond)
        return page

    @unittest.skipIf(sync_playwright is None, "Playwright is not installed")
    def test_browser_desktop_mobile_render_and_synthetic_screenshots(self):
        run, options = self.ready(option_count=3)
        testing = self._testing_run()
        page = self._browser_page()
        self.assertEqual(self.client.get("/static/icons/lucide/rotate-ccw.svg").status_code, 200)
        for width, height in ((1440, 900), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            for name, path in (("queue", "/feedback/reviews"),
                               ("options", f"/feedback/reviews/{run['id']}"),
                               ("release", f"/feedback/reviews/{testing['id']}")):
                with self.subTest(width=width, view=name):
                    page.goto("http://testserver" + path)
                    page.locator(".maintenance-review-workspace").wait_for()
                    for image in page.locator(".review-screenshots img").all():
                        image.scroll_into_view_if_needed()
                    page.wait_for_function("Array.from(document.querySelectorAll('.review-screenshots img')).every(image => image.complete && image.naturalWidth > 0)")
                    page.evaluate("window.scrollTo(0, 0)")
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                    self.assertTrue(page.locator(".maintenance-review-workspace .btn").evaluate_all(
                        "buttons => buttons.every(button => button.scrollWidth <= button.clientWidth + 2)"))
                    screenshot = page.screenshot(path=str(self.root / f"{name}-{width}.png"), full_page=True)
                    self.assertGreater(len(screenshot), 4000)
                    if name == "options":
                        self.assertEqual(page.locator(".review-option").count(), 3)
                        bounds = page.locator(".review-option").evaluate_all(
                            "items => items.map(item => { const rect = item.getBoundingClientRect(); return {x: rect.x, y: rect.y, right: rect.right, bottom: rect.bottom}; })")
                        header_bottom = page.locator(".maintenance-review-workspace > header").evaluate(
                            "header => header.getBoundingClientRect().bottom")
                        self.assertGreater(bounds[0]["y"], header_bottom)
                        if width == 1440:
                            self.assertEqual(len({item["y"] for item in bounds}), 1)
                        for first, second in zip(bounds, bounds[1:]):
                            self.assertTrue(first["right"] <= second["x"] or first["bottom"] <= second["y"])

    @unittest.skipIf(sync_playwright is None, "Playwright is not installed")
    def test_browser_submission_and_stale_warning_never_auto_retry(self):
        run, options = self.ready()
        page = self._browser_page()
        page.goto(f"http://testserver/feedback/reviews/{run['id']}")
        form = page.locator('[data-review-form]').first
        form.get_by_label("Release version").fill("2.07")
        form.get_by_role("checkbox").check()
        form.get_by_role("button", name="Approve test branch").click()
        page.get_by_text("Publication queued", exact=True).wait_for()
        self.assertEqual(review.get_run(run["id"])["version"], "2.07")
        stale, options = self.ready()
        page.goto(f"http://testserver/feedback/reviews/{stale['id']}")
        review.cancel_run(stale["id"], stale["revision"])
        form = page.locator('[data-review-form]').first
        form.get_by_label("Release version").fill("2.08")
        form.get_by_role("checkbox").check()
        form.get_by_role("button", name="Approve test branch").click()
        page.locator('[data-review-error]:not([hidden])').wait_for()
        self.assertIn("nothing was queued", page.locator('[data-review-error]').inner_text())
        self.assertTrue(form.get_by_role("button").is_disabled())
        self.assertEqual(review.get_run(stale["id"])["state"], "cancelled")

    @unittest.skipIf(sync_playwright is None, "Playwright is not installed")
    def test_browser_release_submission_queues_exact_commit(self):
        testing = self._testing_run()
        page = self._browser_page()
        page.goto(f"http://testserver/feedback/reviews/{testing['id']}")
        release_form = page.locator(".review-release-form")
        release_form.get_by_role("checkbox").check()
        release_form.get_by_role("button", name="Approve merge and tag v1.01").click()
        page.get_by_text("Release queued", exact=True).wait_for()
        self.assertEqual(review.get_run(testing["id"])["release_approved_head"], "b" * 40)
