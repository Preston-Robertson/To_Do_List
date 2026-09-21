"""Offline contracts using synthetic configuration, never live application data."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient

from luigi_web.modules.feedback import test_preview as preview
from luigi_web.modules.feedback import maintainer, review
from scripts import maintainer_preview_app as runner


class OriginTests(unittest.TestCase):
    def test_dedicated_origin(self):
        with patch.dict(os.environ, {"LUIGI_MAINTAINER_UI_URL": "https://ui.example.test",
                                     "LUIGI_MAINTAINER_PREVIEW_URL": "https://preview.example.test/"}):
            self.assertEqual(preview.preview_origin(), "https://preview.example.test")

    def test_reject_unsafe_origins(self):
        invalid = ("https://ui.example.test:58120", "https://UI.example.test",
                   "https://ui.example.test./", "http://preview.example.test",
                   "https://preview.example.test/app", "https://preview.example.test/?ticket=bad",
                   "https://preview.example.test/#fragment", "https://user@preview.example.test",
                   "https://preview.example.test/?", "https://preview.example.test/%2f",
                   "https://preview.example.test:bad", "https://preview.example.test\\evil")
        for origin in invalid:
            with self.subTest(origin=origin), patch.dict(os.environ, {
                "LUIGI_MAINTAINER_UI_URL": "https://ui.example.test",
                "LUIGI_MAINTAINER_PREVIEW_URL": origin,
            }), self.assertRaises(ValueError):
                preview.preview_origin()


class GatewayTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        self.root = Path(storage.name)
        environment = {name: os.environ[name] for name in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR") if name in os.environ}
        environment.update({
            "LUIGI_WEB_MAINTAINER_DB": str(self.root / "queue.sqlite3"),
            "LUIGI_MAINTAINER_PREVIEW_STATE_DIR": str(self.root / "preview"),
            "LUIGI_MAINTAINER_UI_URL": "https://ui.example.test",
            "LUIGI_MAINTAINER_PREVIEW_URL": "https://preview.example.test",
            "LUIGI_MAINTAINER_PREVIEW_GATEWAY_KEY": "ab" * 32,
            "TEMP": str(self.root), "TMP": str(self.root),
            "APPDATA": str(self.root), "LOCALAPPDATA": str(self.root),
        })
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        self.enterContext(patch.object(maintainer.feedback, "get_item", side_effect=AssertionError("raw inbox read")))
        job = maintainer.enqueue_feedback({"uuid": str(uuid.uuid4()), "category": "Idea",
                                           "message": "Synthetic improvement", "page_path": "/home"}, "Synthetic criteria")
        maintainer.claim_next_job()
        run = review.create_run(job, "a" * 40)
        checks = [{"command_id": name, "passed": True, "total": 1, "failed": 0,
                   "skipped": 0, "output_digest": "b" * 64} for name in sorted(review.REQUIRED_CHECKS)]
        options = [review.add_option(run["id"], label="Synthetic option", summary="Synthetic summary",
                                    diff_sha256=hashlib.sha256(str(index).encode()).hexdigest(),
                                    artifact_id=str(uuid.uuid4()), tree_sha256="d" * 64, test_results=checks)
                   for index in range(2)]
        run = review.finish_generation(run["id"])
        run = review.approve_option(run["id"], options[0]["id"], run["revision"], "1.01")
        review.claim_publish()
        run = review.record_published(run["id"], "c" * 40, run["branch"], "https://github.com/example/project/pull/7", 7)
        self.run_record = review.record_preview(run["id"], run["head_commit"], review.preview_path(run["id"]))
        instance = str(uuid.uuid4())
        self.state = {"run_id": run["id"], "head_commit": run["head_commit"], "instance": instance,
                      "source_digest": "d" * 64, "container_name": "luigi-preview-" + instance.replace("-", ""),
                      "expires": int(time.time()) + 3500}
        (self.root / "preview").mkdir()
        self.save_state()
        self.client = self.enterContext(TestClient(preview.gateway_app(), base_url="https://preview.example.test"))

    def save_state(self):
        (self.root / "preview" / "active.json").write_text(json.dumps(self.state), encoding="ascii")

    def login(self, ticket=None, **kwargs):
        return self.client.post("/session", data={"run_id": self.run_record["id"],
                                                 "ticket": ticket or preview.create_ticket(self.run_record)},
                                headers={"Origin": "https://ui.example.test"}, **kwargs)

    def test_ticket_one_use_body_only_cookie_security(self):
        ticket = preview.create_ticket(self.run_record)
        result = self.login(ticket)
        self.assertEqual(result.status_code, 200)
        self.assertNotIn(ticket, result.text)
        self.assertNotIn("location", result.headers)
        cookie = result.headers["set-cookie"]
        for flag in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Domain=", cookie)
        self.assertEqual(self.login(ticket).status_code, 403)
        self.assertEqual(self.client.get("/session?ticket=" + ticket).status_code, 403)

    def test_expired_tampered_future_and_stale_tickets(self):
        ticket = preview.create_ticket(self.run_record)
        claims = preview._verify(ticket, "ticket", 60)
        with self.assertRaises(ValueError):
            preview._verify(ticket[:-1] + ("0" if ticket[-1] != "0" else "1"), "ticket", 60)
        with patch.object(preview.time, "time", return_value=claims["exp"]), self.assertRaises(ValueError):
            preview._verify(ticket, "ticket", 60)
        claims.update(iat=int(time.time()) + 10, exp=int(time.time()) + 60)
        with self.assertRaises(ValueError):
            preview._verify(preview._sign(claims, "ticket"), "ticket", 60)
        review.record_head(self.run_record["id"], "e" * 40)
        self.assertEqual(self.login(ticket).status_code, 403)

    def test_replaced_active_manifest_invalidates_session(self):
        self.assertEqual(self.login().status_code, 200)
        self.state["instance"] = str(uuid.uuid4())
        self.state["container_name"] = "luigi-preview-" + self.state["instance"].replace("-", "")
        self.save_state()
        with patch.object(preview, "_proxy", new_callable=AsyncMock) as proxy:
            self.assertEqual(self.client.get("/").status_code, 403)
            proxy.assert_not_called()

    def test_unauthorized_origin_and_mutation_do_not_reach_backend(self):
        ticket = preview.create_ticket(self.run_record)
        self.assertEqual(self.client.post("/session", data={"run_id": self.run_record["id"], "ticket": ticket},
                                          headers={"Origin": "https://other.example.test"}).status_code, 403)
        with patch.object(preview, "_proxy", new_callable=AsyncMock) as proxy:
            self.assertEqual(self.client.get("/").status_code, 403)
            self.assertEqual(self.login(ticket).status_code, 200)
            self.assertEqual(self.client.post("/tasks").status_code, 403)
            self.assertEqual(self.client.post("/tasks", headers={"Origin": "https://ui.example.test"}).status_code, 403)
            proxy.assert_not_called()

    def upstream(self, handler):
        self.enterContext(patch.object(preview, "_socket_address", self.socket_address))
        return self.enterContext(patch.object(preview.httpx, "AsyncHTTPTransport", return_value=httpx.MockTransport(handler)))

    @asynccontextmanager
    async def socket_address(self, state):
        yield str(self.root / "app.sock")

    def test_proxy_strips_secrets_and_normalizes_synthetic_cookies(self):
        self.login()
        self.client.cookies.set("luigi_session", "synthetic-production-sentinel")
        self.client.cookies.set("luigi_csrf", "synthetic-csrf")
        self.client.cookies.set("luigi_preview_session_58120", "synthetic-fixture-session")

        def handler(request):
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("forwarded", request.headers)
            self.assertNotIn(preview.COOKIE, request.headers["cookie"])
            self.assertNotIn("production-sentinel", request.headers["cookie"])
            self.assertIn("luigi_csrf", request.headers["cookie"])
            self.assertEqual(request.headers["origin"], preview.BACKEND)
            return httpx.Response(200, headers=[("content-type", "text/html"),
                ("set-cookie", "luigi_csrf=synthetic-next; Domain=ui.example.test; Path=/evil"),
                ("set-cookie", preview.COOKIE + "=evil"), ("set-cookie", "luigi_session=evil")], stream=httpx.ByteStream(b"synthetic"))

        self.upstream(handler)
        result = self.client.post("/tasks", headers={"Origin": "https://preview.example.test",
                                  "Authorization": "Bearer synthetic-sentinel", "Forwarded": "host=other.example.test"})
        self.assertEqual(result.status_code, 200)
        cookie = result.headers["set-cookie"]
        self.assertNotIn("Domain", cookie)
        self.assertNotIn("evil", cookie)
        self.assertIn("Secure", cookie)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertIn("worker-src 'none'", result.headers["content-security-policy"])

    def test_htmx_edit_headers_survive_without_production_context(self):
        self.assertEqual(self.login().status_code, 200)
        expected = {"hx-request": "true", "hx-target": "task-list", "hx-trigger": "task-save",
                    "hx-trigger-name": "save", "hx-prompt": "Synthetic answer"}

        def handler(request):
            for name, value in expected.items():
                self.assertEqual(request.headers.get(name), value)
            self.assertNotIn("hx-current-url", request.headers)
            self.assertNotIn("referer", request.headers)
            self.assertEqual(request.headers["origin"], preview.BACKEND)
            return httpx.Response(200, headers={"HX-Refresh": "true", "HX-Trigger": '{"tasksChanged":true}',
                                               "HX-Redirect": preview.BACKEND + "/tasks"},
                                  stream=httpx.ByteStream(b"<div>synthetic edit</div>"))

        self.upstream(handler)
        response = self.client.post("/tasks", headers={**expected, "Origin": "https://preview.example.test",
                                    "HX-Current-URL": "https://ui.example.test/tasks?token=synthetic",
                                    "Referer": "https://ui.example.test/"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("hx-refresh"), "true")
        self.assertEqual(response.headers.get("hx-trigger"), '{"tasksChanged":true}')
        self.assertEqual(response.headers.get("hx-redirect"), "/tasks")

    def test_htmx_local_location_and_declarative_events_are_preserved(self):
        self.login()
        headers = {"HX-Location": json.dumps({"path": preview.BACKEND + "/tasks", "target": "#task-list",
                                             "swap": "innerHTML", "select": "#task-list"}),
                   "HX-Trigger": "tasksChanged, taskSaved", "HX-Trigger-After-Swap": '{"taskSaved":{"value":1}}',
                   "HX-Trigger-After-Settle": "taskSettled", "HX-Push-Url": "https://ui.example.test/",
                   "Authorization": "synthetic", "Access-Control-Allow-Origin": "*", "X-Untrusted": "synthetic"}
        self.upstream(lambda request: httpx.Response(200, headers=headers, stream=httpx.ByteStream(b"<div>synthetic</div>")))
        result = self.client.get("/")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(json.loads(result.headers["hx-location"]),
                         {"path": "/tasks", "target": "#task-list", "swap": "innerHTML", "select": "#task-list"})
        self.assertEqual(result.headers["hx-trigger"], "tasksChanged,taskSaved")
        self.assertEqual(json.loads(result.headers["hx-trigger-after-swap"]), {"taskSaved": {"value": 1}})
        self.assertEqual(result.headers["hx-trigger-after-settle"], "taskSettled")
        for name in ("hx-push-url", "authorization", "access-control-allow-origin", "x-untrusted"):
            self.assertNotIn(name, result.headers)
        for value in ("/tasks", preview.BACKEND + "/tasks"):
            self.assertEqual(preview._htmx_location(value), "/tasks")

    def test_htmx_redirects_reject_external_ambiguous_and_injected_locations(self):
        self.login()
        locations = ("https://ui.example.test/tasks", "//other.example.test/", "javascript:alert(1)",
                     "http://127.0.0.1:57300/", "/tasks?token=synthetic", "/tasks#fragment", "/%2foutside",
                     "/%252foutside", "/tasks/../data", "/tasks\\other", "/tasks\r\nX-Injected: value")
        for header in ("HX-Redirect", "HX-Location"):
            for location in locations:
                with self.subTest(header=header, location=location), patch.object(preview, "_socket_address", self.socket_address), patch.object(
                    preview.httpx, "AsyncHTTPTransport", return_value=httpx.MockTransport(
                        lambda request: httpx.Response(200, headers={header: location}, stream=httpx.ByteStream(b"synthetic")))):
                    self.assertEqual(self.client.get("/").status_code, 403)
        for value in ({"path": "https://other.example.test/"}, {"path": "/tasks", "headers": {"Authorization": "synthetic"}},
                      {"path": "/tasks", "values": {"token": "synthetic"}}, {"path": "/tasks", "handler": "alert(1)"},
                      {"path": False}, {"target": "#tasks"}, {"path": "/tasks", "target": "\r\nInjected"}):
            with self.subTest(location=value), self.assertRaises(ValueError):
                preview._htmx_location(json.dumps(value))

    def test_htmx_header_limits_and_invalid_events_fail_closed(self):
        self.login()
        self.upstream(lambda request: httpx.Response(200, stream=httpx.ByteStream(b"synthetic")))
        for value in ("x" * (preview.MAX_HEADER + 1), "synthetic\r\nX-Injected: value", "synthetic\x00value"):
            with self.subTest(prompt=value[:20]):
                self.assertEqual(self.client.post("/tasks", headers={"Origin": "https://preview.example.test", "HX-Prompt": value}).status_code, 403)
        for value in ("x" * (preview.MAX_HEADER + 1), '{"event":', '[]', '{}', '{"event":NaN}',
                      '{"__proto__":{}}', "event,", "javascript:alert(1)", "event\r\nInjected", "constructor",
                      ",".join("event" + str(index) for index in range(65))):
            with self.subTest(trigger=value[:20]), self.assertRaises(ValueError):
                preview._htmx_trigger(value)
        for header, value in (("HX-Refresh", "false"), ("HX-Trigger", "alert(1)"),
                              ("HX-Trigger-After-Swap", "x" * (preview.MAX_HEADER + 1)),
                              ("HX-Trigger-After-Settle", '{"event":'), ("HX-Location", '{"path":false}')):
            with self.subTest(header=header), patch.object(preview.httpx, "AsyncHTTPTransport", return_value=httpx.MockTransport(
                lambda request: httpx.Response(200, headers={header: value}, stream=httpx.ByteStream(b"synthetic")))):
                self.assertEqual(self.client.get("/").status_code, 403)

    def test_fixture_cookie_allowlist_is_fixed_port_and_normalized_both_ways(self):
        self.login()
        allowed = ("luigi_csrf", "luigi_preview_session_58120", "luigi_media_preview_session_58120",
                   "luigi_cards_preview_58120", "luigi_finance_preview_ui_58120", "luigi_finance_preview_finance_58120",
                   "luigi_finance_preview_csrf_58120")
        rejected = ("luigi_session", "luigi_finance_session", "luigi_preview_session_57300", "untrusted",
                    "luigi_cards_preview_58121", "luigi_finance_preview_csrf_581200")
        for name in (*allowed, *rejected):
            self.client.cookies.set(name, "synthetic")

        def handler(request):
            from http.cookies import SimpleCookie
            incoming = SimpleCookie(request.headers["cookie"])
            self.assertEqual(set(incoming), set(allowed))
            return httpx.Response(200, headers=[("set-cookie", name + "=synthetic-next; Domain=ui.example.test; Path=/other; HttpOnly")
                                               for name in (*allowed, *rejected, preview.COOKIE)],
                                  stream=httpx.ByteStream(b"synthetic"))

        self.upstream(handler)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        outgoing = response.headers.get_list("set-cookie")
        self.assertEqual(len(outgoing), len(allowed))
        self.assertEqual({value.split("=", 1)[0] for value in outgoing}, set(allowed))
        for value in outgoing:
            self.assertNotIn("Domain=", value)
            for flag in ("Path=/", "Secure", "HttpOnly", "SameSite=strict"):
                self.assertIn(flag, value)

    def test_external_redirect_and_response_limits_fail_closed(self):
        self.login()
        for location in ("https://ui.example.test/", "//other.example.test/", "http://127.0.0.1:58120/x?token=bad"):
            with self.subTest(location=location), patch.object(preview, "_socket_address", self.socket_address), patch.object(
                preview.httpx, "AsyncHTTPTransport", return_value=httpx.MockTransport(
                    lambda request: httpx.Response(303, headers={"location": location}, stream=httpx.ByteStream(b"")))):
                self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 403)
        with patch.object(preview, "MAX_RESPONSE", 4):
            self.upstream(lambda request: httpx.Response(200, stream=httpx.ByteStream(b"oversized")))
            self.assertEqual(self.client.get("/").status_code, 403)

    def test_request_limits_and_path_traversal(self):
        self.login()
        with patch.object(preview, "MAX_BODY", 4), patch.object(preview, "_proxy", new_callable=AsyncMock) as proxy:
            self.assertEqual(self.client.post("/tasks", content=b"oversized",
                                             headers={"Origin": "https://preview.example.test"}).status_code, 403)
            proxy.assert_not_called()
        for path in ("//external", "/../data", "/%252e%252e/data", "/a/%2e%2e/data", "/a\\b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                preview._path(path)

    def test_not_testing_and_unrecorded_preview_deny_tickets(self):
        with review._transaction() as connection:
            connection.execute("UPDATE review_runs SET preview_commit = NULL WHERE id = ?", (self.run_record["id"],))
        with self.assertRaises(ValueError):
            preview.create_ticket(self.run_record)
        review.mark_attention(self.run_record["id"], "Synthetic attention")
        with self.assertRaises(ValueError):
            preview.create_ticket(self.run_record)

    def test_concurrent_ticket_redemption_only_one_succeeds(self):
        ticket = preview.create_ticket(self.run_record)

        def redeem():
            try:
                preview._redeem(ticket, self.run_record["id"])
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(lambda unused: redeem(), range(2))), [False, True])

    def test_ticket_store_is_bounded_and_contains_only_digests(self):
        ticket = preview.create_ticket(self.run_record)
        with maintainer._connect() as connection:
            rows = connection.execute("SELECT * FROM preview_tickets").fetchall()
            self.assertEqual(rows[0]["digest"], hashlib.sha256(ticket.encode()).hexdigest())
            connection.executemany("INSERT INTO preview_tickets VALUES (?, ?)",
                                   [(f"{index:064x}", int(time.time()) + 60) for index in range(255)])
        with self.assertRaisesRegex(ValueError, "limit"):
            preview.create_ticket(self.run_record)

    def test_status_requires_rendered_health_and_does_not_return_paths(self):
        self.upstream(lambda request: httpx.Response(200, stream=httpx.ByteStream(b"<html>synthetic</html>")))
        result = self.client.get("/_maintainer/status")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(set(result.json()), {"ready", "run_id", "head_commit", "instance"})
        self.assertNotIn(str(self.root), result.text)

    def ready_socket(self):
        socket = Mock()
        socket.lstat.return_value = SimpleNamespace(st_dev=1, st_ino=17)
        self.enterContext(patch.object(preview, "_socket", return_value=socket))
        return socket

    def test_verify_ready_checks_gateway_without_signing_key_or_mutations(self):
        self.ready_socket()
        os.environ.pop("LUIGI_MAINTAINER_PREVIEW_GATEWAY_KEY")
        before = review.get_run(self.run_record["id"])
        with patch.object(preview, "_gateway_ready", return_value=True) as ready, patch.object(
            preview.review_worker, "load_candidate", side_effect=AssertionError("candidate read")):
            self.assertTrue(preview.verify_ready(self.run_record))
        ready.assert_called_once_with(self.state)
        self.assertEqual(review.get_run(self.run_record["id"]), before)

    def test_verify_ready_missing_runtime_fails_closed(self):
        before = review.get_run(self.run_record["id"])
        with patch.object(preview, "_gateway_ready") as ready, self.assertRaisesRegex(ValueError, "unavailable"):
            preview.verify_ready(self.run_record)
        ready.assert_not_called()
        self.assertEqual(review.get_run(self.run_record["id"]), before)

    def test_verify_ready_expired_deadline_and_source_mismatch(self):
        self.ready_socket()
        original = dict(self.state)
        for changes in ({"expires": int(time.time())}, {"expires": int(time.time()) - 1},
                        {"expires": int(time.time()) + 7200}, {"head_commit": "e" * 40},
                        {"source_digest": "e" * 64}):
            with self.subTest(changes=changes):
                self.state = {**original, **changes}
                self.save_state()
                with patch.object(preview, "_gateway_ready") as ready, self.assertRaises(ValueError):
                    preview.verify_ready(self.run_record)
                ready.assert_not_called()

    def test_verify_ready_stale_caller_head_option_and_unrecorded_evidence(self):
        self.ready_socket()
        for changes in ({"id": str(uuid.uuid4())}, {"head_commit": "e" * 40},
                        {"selected_option_id": str(uuid.uuid4())}, {"state": "needs_attention"}):
            with self.subTest(changes=changes), patch.object(preview, "_gateway_ready") as ready, self.assertRaises(ValueError):
                preview.verify_ready({**self.run_record, **changes})
            ready.assert_not_called()
        with review._transaction() as connection:
            connection.execute("UPDATE review_runs SET preview_commit = NULL WHERE id = ?", (self.run_record["id"],))
        with patch.object(preview, "_gateway_ready") as ready, self.assertRaises(ValueError):
            preview.verify_ready(self.run_record)
        ready.assert_not_called()

    def test_verify_ready_rechecks_lease_and_socket_after_gateway(self):
        socket = self.ready_socket()
        original = dict(self.state)

        def expire(state):
            self.state["expires"] = int(time.time()) - 1
            self.save_state()
            return True

        def replace(state):
            self.state["instance"] = str(uuid.uuid4())
            self.state["container_name"] = "luigi-preview-" + self.state["instance"].replace("-", "")
            self.save_state()
            return True

        def change_head(state):
            review.record_head(self.run_record["id"], "e" * 40)
            return True

        for change in (expire, replace, change_head):
            with self.subTest(change=change.__name__):
                self.state = dict(original)
                self.save_state()
                with patch.object(preview, "_gateway_ready", side_effect=change), self.assertRaises(ValueError):
                    preview.verify_ready(self.run_record)
        review.record_head(self.run_record["id"], self.run_record["head_commit"])
        review.record_preview(self.run_record["id"], self.run_record["head_commit"], review.preview_path(self.run_record["id"]))
        socket.lstat.side_effect = [SimpleNamespace(st_dev=1, st_ino=17), SimpleNamespace(st_dev=1, st_ino=18)]
        with patch.object(preview, "_gateway_ready", return_value=True), self.assertRaises(ValueError):
            preview.verify_ready(self.run_record)

    def test_verify_ready_gateway_failure_and_explicit_metadata_only_mode(self):
        self.ready_socket()
        with patch.object(preview, "_gateway_ready", return_value=False) as ready:
            with self.assertRaises(ValueError):
                preview.verify_ready(self.run_record)
            self.assertTrue(preview.verify_ready(self.run_record, gateway_check=False))
        ready.assert_called_once_with(self.state)

    def test_release_stages_retain_existing_session_but_deny_new_tickets(self):
        self.assertEqual(self.login().status_code, 200)
        ticket = preview.create_ticket(self.run_record)
        self.ready_socket()
        self.upstream(lambda request: httpx.Response(200, stream=httpx.ByteStream(b"<html>synthetic</html>")))
        run = review.record_test_result(self.run_record["id"], self.run_record["head_commit"], True)
        run = review.approve_release(run["id"], run["revision"], run["head_commit"], "1.01", confirmed=True)
        for state in ("release_queued", "releasing"):
            with self.subTest(state=state):
                with review._transaction() as connection:
                    connection.execute("UPDATE review_runs SET state = ? WHERE id = ?", (state, run["id"]))
                run = review.get_run(run["id"])
                with patch.object(preview, "_gateway_ready", return_value=True):
                    self.assertTrue(preview.verify_ready(run))
                self.assertEqual(self.client.get("/").status_code, 200)
                self.assertEqual(self.client.get("/_maintainer/status").status_code, 200)
                with self.assertRaises(ValueError):
                    preview.create_ticket(run)
                self.assertEqual(self.login(ticket).status_code, 403)
        review.mark_attention(run["id"], "Synthetic attention")
        self.assertEqual(self.client.get("/").status_code, 403)
        self.assertEqual(self.client.get("/_maintainer/status").status_code, 403)
        with patch.object(preview, "_gateway_ready", return_value=True), self.assertRaises(ValueError):
            preview.verify_ready(run)

    def test_status_allows_startup_before_recording_and_rechecks_after_health(self):
        with review._transaction() as connection:
            connection.execute("UPDATE review_runs SET preview_commit = NULL WHERE id = ?", (self.run_record["id"],))
        self.upstream(lambda request: httpx.Response(200, stream=httpx.ByteStream(b"<html>synthetic</html>")))
        self.assertEqual(self.client.get("/_maintainer/status").status_code, 200)
        with self.assertRaises(ValueError):
            preview.create_ticket(self.run_record)

        async def expire(state):
            self.state["expires"] = int(time.time()) - 1
            self.save_state()

        with patch.object(preview, "_health", side_effect=expire):
            self.assertEqual(self.client.get("/_maintainer/status").status_code, 403)

    def test_gateway_ready_uses_only_fixed_loopback_status_and_exact_binding(self):
        client_factory = httpx.Client
        expected = {"ready": True, **{name: self.state[name] for name in ("run_id", "head_commit", "instance")}}
        responses = [(200, json.dumps(expected).encode(), True), (503, json.dumps(expected).encode(), False),
                     (200, json.dumps({**expected, "head_commit": "e" * 40}).encode(), False),
                     (200, json.dumps({**expected, "instance": str(uuid.uuid4())}).encode(), False),
                     (200, json.dumps({**expected, "run_id": str(uuid.uuid4())}).encode(), False),
                     (200, b"<html>not status</html>", False), (200, b"x" * 4097, False)]
        for status, content, ready in responses:
            def handler(request):
                self.assertEqual(str(request.url), "http://127.0.0.1:58120/_maintainer/status")
                self.assertNotIn("authorization", request.headers)
                self.assertNotIn("cookie", request.headers)
                return httpx.Response(status, stream=httpx.ByteStream(content))

            with self.subTest(status=status, ready=ready), patch.object(preview.httpx, "Client", side_effect=lambda **kwargs:
                client_factory(transport=httpx.MockTransport(handler), **kwargs)) as factory:
                self.assertEqual(preview._gateway_ready(self.state), ready)
                self.assertEqual(factory.call_args.kwargs, {"trust_env": False, "timeout": 3, "follow_redirects": False})

    def test_stopped_preview_invalidates_old_session_and_readiness(self):
        self.login()
        self.ready_socket()
        with patch.object(preview, "_controller_lock", return_value=nullcontext(self.root / "preview")), patch.object(preview, "_podman") as podman:
            preview.stop_preview()
        self.assertEqual(podman.call_count, 1)
        self.assertEqual(self.client.get("/").status_code, 403)
        with patch.object(preview, "_gateway_ready") as ready, self.assertRaises(ValueError):
            preview.verify_ready(self.run_record)
        ready.assert_not_called()

    def test_socket_regular_file_and_manifest_link_are_rejected(self):
        runtime = self.root / "preview" / "r" / self.state["instance"]
        runtime.mkdir(parents=True)
        (runtime / "app.sock").write_text("not a socket", encoding="ascii")
        with self.assertRaises(ValueError):
            preview._socket(self.state)
        with patch.object(Path, "is_symlink", return_value=True), self.assertRaises(ValueError):
            preview._active()

    def test_socket_inode_is_pinned_and_descriptor_closed(self):
        async def connect():
            async with preview._socket_address(self.state) as address:
                self.assertEqual(address, "/proc/self/fd/123")

        with patch.object(preview, "_socket", return_value=self.root / "app.sock"), patch.object(
            preview.os, "O_PATH", 0x200000, create=True), patch.object(preview.os, "O_NOFOLLOW", 0x20000, create=True), patch.object(
            preview.os, "open", return_value=123) as opened, patch.object(preview.os, "close") as closed, patch.object(
            preview.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFSOCK, st_nlink=1)) as inspected:
            asyncio.run(connect())
            self.assertEqual(opened.call_args.args[1], 0x220000)
            closed.assert_called_once_with(123)
            inspected.return_value.st_mode = stat.S_IFLNK
            with self.assertRaises(ValueError):
                asyncio.run(connect())
            self.assertEqual(closed.call_count, 2)

    def controller(self, *, occupied=False):
        if not occupied:
            (self.root / "preview" / "active.json").unlink()
        os.environ.pop("LUIGI_MAINTAINER_PREVIEW_GATEWAY_KEY")
        image = "localhost/luigi-maintainer@sha256:" + "f" * 64
        os.environ["LUIGI_MAINTAINER_SANDBOX_IMAGE"] = image
        config = preview.review_worker.WorkerConfig(self.root / "worker", "https://github.com/example/project.git")
        self.enterContext(patch.object(preview.sandbox, "available", return_value=SimpleNamespace(available=True)))
        self.enterContext(patch.object(preview, "_controller_lock", side_effect=lambda: nullcontext(self.root / "preview")))
        loader = self.enterContext(patch.object(preview.review_worker, "load_candidate",
                                               return_value=SimpleNamespace(metadata={"image": image})))
        prepare = self.enterContext(patch.object(preview, "prepare_published_source",
                                                return_value=SimpleNamespace(path=self.root / "worktree")))
        cleanup = self.enterContext(patch.object(preview, "_remove_worktree"))

        def export(source, destination):
            destination.mkdir()
            (destination / "app.py").write_text("raise RuntimeError('never execute on host')\n", encoding="ascii")
            return SimpleNamespace(source_digest="d" * 64)

        self.enterContext(patch.object(preview.sandbox, "export_candidate", side_effect=export))
        self.enterContext(patch.object(preview.sandbox, "_snapshot_digest", return_value="d" * 64))
        podman = self.enterContext(patch.object(preview, "_podman"))
        ready = self.enterContext(patch.object(preview, "_wait_ready", return_value=True))
        option = review.get_option(self.run_record["selected_option_id"])
        return SimpleNamespace(config=config, option=option, podman=podman, ready=ready,
                               prepare=prepare, cleanup=cleanup, loader=loader)

    def test_controller_receipt_only_after_source_socket_and_gateway(self):
        controls = self.controller()
        receipt = preview.start_preview(controls.config, self.run_record, controls.option)
        self.assertEqual(receipt, {"ready": True, "head_commit": self.run_record["head_commit"],
                                   "preview_url": review.preview_path(self.run_record["id"])})
        state = preview._active()
        self.assertEqual(controls.ready.call_args_list[0].kwargs, {"gateway": False})
        self.assertEqual(controls.ready.call_args_list[1].args[0], state)
        self.assertIsNone(review.get_run(self.run_record["id"])["preview_commit"])
        controls.cleanup.assert_called_once()
        command = controls.podman.call_args.args[0]
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--unsetenv-all", "--http-proxy=false",
                     "--log-driver=none", "--detach", "--timeout=3600", "--userns=keep-id:uid=65534,gid=65534"):
            self.assertIn(flag, command)
        self.assertIn("/opt/luigi-tests/preview.py", command)
        self.assertEqual(command[command.index("--public-origin") + 1], "https://preview.example.test")
        mounts = [argument for argument in command if argument.startswith("type=bind,")]
        self.assertEqual(len(mounts), 2)
        self.assertTrue(any("destination=/workspace,ro" in argument for argument in mounts))
        self.assertTrue(any("destination=/run/preview,rw" in argument for argument in mounts))
        self.assertFalse(any("GATEWAY_KEY" in argument or "GITHUB_TOKEN" in argument for argument in command))

    def test_parent_preview_once_records_only_verified_receipt(self):
        controls = self.controller()
        result = preview.review_worker.preview_once(controls.config, self.run_record["id"], preview_runner=preview.start_preview)
        self.assertEqual(result.status, "testing")
        self.assertEqual(review.get_run(self.run_record["id"])["preview_commit"], self.run_record["head_commit"])

    def test_controller_refuses_existing_preview(self):
        controls = self.controller(occupied=True)
        self.assertEqual(preview.start_preview(controls.config, self.run_record, controls.option), {"ready": False})
        controls.prepare.assert_not_called()
        controls.podman.assert_not_called()

    def test_source_mismatch_never_starts_container(self):
        controls = self.controller()
        with patch.object(preview.sandbox, "export_candidate", return_value=SimpleNamespace(source_digest="e" * 64)), self.assertRaises(ValueError):
            preview.start_preview(controls.config, self.run_record, controls.option)
        controls.podman.assert_not_called()
        self.assertFalse((self.root / "preview" / "active.json").exists())

    def test_gateway_failure_removes_container_without_receipt(self):
        controls = self.controller()
        controls.ready.side_effect = [True, False]
        with self.assertRaises(ValueError):
            preview.start_preview(controls.config, self.run_record, controls.option)
        self.assertEqual(controls.podman.call_count, 2)
        self.assertEqual(controls.podman.call_args.args[0][2:5], ["rm", "--force", "--ignore"])
        self.assertFalse((self.root / "preview" / "active.json").exists())
        self.assertIsNone(review.get_run(self.run_record["id"])["preview_commit"])

    def test_uncertain_cleanup_keeps_slot_blocked(self):
        controls = self.controller()
        controls.podman.side_effect = [ValueError("synthetic timeout"), ValueError("synthetic cleanup timeout")]
        with self.assertRaisesRegex(ValueError, "cleanup"):
            preview.start_preview(controls.config, self.run_record, controls.option)
        self.assertTrue((self.root / "preview" / "active.json").exists())
        self.assertEqual(preview.start_preview(controls.config, self.run_record, controls.option), {"ready": False})

    def test_stop_revokes_release_gate_before_container_removal(self):
        run = review.record_test_result(self.run_record["id"], self.run_record["head_commit"], True)
        review.approve_release(run["id"], run["revision"], run["head_commit"], "1.01", confirmed=True)

        def removed(command):
            current = review.get_run(run["id"])
            self.assertIsNone(current["preview_commit"])
            self.assertIsNone(current["release_approved_head"])
            self.assertEqual(current["state"], "needs_attention")
            self.assertEqual(command[-1], self.state["container_name"])

        with patch.object(preview, "_controller_lock", return_value=nullcontext(self.root / "preview")), patch.object(preview, "_podman", side_effect=removed):
            preview.stop_preview()
        self.assertFalse((self.root / "preview" / "active.json").exists())

    def test_roles_keep_gateway_key_out_of_controller_and_publisher_out_of_gateway(self):
        config = SimpleNamespace()
        with self.assertRaisesRegex(ValueError, "secrets"):
            preview.start_preview(config, self.run_record, {})
        with patch.dict(os.environ, {"LUIGI_MAINTAINER_GITHUB_TOKEN": "synthetic-sentinel"}), self.assertRaises(ValueError):
            preview.gateway_app()

    def test_gateway_rejects_application_and_mail_secrets(self):
        for name in ("LUIGI_WEB_TOKEN", "LUIGI_WEB_UI_TOKEN", "LUIGI_WEB_FINANCE_TOKEN",
                     "LUIGI_MAINTAINER_SMTP_PASSWORD", "LUIGI_MAINTAINER_SMTP_USER",
                     "LUIGI_RELEASE_GITHUB_TOKEN", "LUIGI_MAINTAINER_COPILOT_TOKEN",
                     "GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "synthetic-sentinel"}), self.assertRaises(ValueError):
                preview.gateway_app()

    def test_published_head_is_fetched_and_verified_before_detached_checkout(self):
        from luigi_web.modules.feedback import release
        config = preview.review_worker.WorkerConfig(self.root / "worker", "https://github.com/example/project.git")
        calls = []

        def git(config, arguments, **kwargs):
            calls.append((arguments, kwargs))
            return SimpleNamespace(stdout=self.run_record["head_commit"])

        with patch.object(release, "_git_metadata"), patch.object(release, "_git", side_effect=git):
            context = preview.prepare_published_source(config, self.run_record)
        self.assertEqual(context.base_commit, self.run_record["head_commit"])
        self.assertIn("--no-recurse-submodules", calls[0][0])
        self.assertTrue(calls[0][1]["auth"])
        self.assertEqual(calls[2][0][:3], ["worktree", "add", "--detach"])
        self.assertEqual(calls[3][0], ["rev-parse", "HEAD"])
        with patch.object(release, "_git_metadata"), patch.object(release, "_git", return_value=SimpleNamespace(stdout="e" * 40)) as git_mock:
            with self.assertRaisesRegex(ValueError, "head changed"):
                preview.prepare_published_source(config, self.run_record)
            self.assertFalse(any(call.args[1][:2] == ["worktree", "add"] for call in git_mock.call_args_list))


class RunnerTests(unittest.TestCase):
    def test_build_context_has_only_the_three_audited_inputs(self):
        root = Path(__file__).resolve().parents[1]
        copies = [line.strip() for line in (root / "examples/maintainer-sandbox.Dockerfile").read_text().splitlines()
                  if line.strip().startswith("COPY ")]
        self.assertEqual(copies, ["COPY requirements.txt /opt/luigi-tests/requirements.txt",
                                 "COPY scripts/maintainer_sandbox_check.py /opt/luigi-tests/check.py",
                                 "COPY --chmod=0444 scripts/maintainer_preview_app.py /opt/luigi-tests/preview.py"])

    def test_owned_files_have_clean_format(self):
        root = Path(__file__).resolve().parents[1]
        for relative in ("module-repos/feedback/src/luigi_web/modules/feedback/test_preview.py",
                         "scripts/maintainer_preview_app.py", "tests/test_maintainer_test_preview.py",
                         "examples/maintainer-sandbox.Dockerfile", "docs/maintainer-test-preview.md"):
            with self.subTest(file=relative):
                content = (root / relative).read_bytes()
                self.assertTrue(content.endswith(b"\n"), "Missing final newline")
                text = content.decode("ascii")
                self.assertTrue(all(line == line.rstrip(" \t") for line in text.splitlines()))

    def test_uds_scope_is_loopback_without_forwarded_host(self):
        captured = []

        async def app(scope, receive, send):
            captured.append(scope)

        scope = {"type": "http", "method": "POST", "client": None,
             "headers": [(b"host", b"preview.example.test"), (b"origin", b"https://preview.example.test"),
              (b"x-forwarded-host", b"ui.example.test"), (b"cookie", b"luigi_csrf=synthetic"),
              (b"hx-request", b"true")]}
        asyncio.run(runner.LoopbackScope(app)(scope, AsyncMock(), AsyncMock()))
        self.assertEqual(captured[0]["client"][0], "127.0.0.1")
        self.assertEqual(dict(captured[0]["headers"])[b"host"], b"127.0.0.1:58120")
        self.assertEqual(dict(captured[0]["headers"])[b"origin"], b"http://127.0.0.1:58120")
        self.assertEqual(dict(captured[0]["headers"])[b"hx-request"], b"true")
        self.assertEqual(dict(scope["headers"])[b"origin"], b"https://preview.example.test")
        self.assertNotIn(b"x-forwarded-host", dict(captured[0]["headers"]))
        self.assertIsNone(scope["client"])

    def test_all_profiles_keep_fixture_context_alive(self):
        for profile in runner.PROFILES:
            active = []
            fixture = Mock()

            @contextmanager
            def context():
                active.append(True)
                if profile == "media":
                    token = module._ORIGIN.set("http://127.0.0.1:58120")
                    self.assertEqual(module._ORIGIN.get(), "https://preview.example.test")
                    module._ORIGIN.reset(token)
                try:
                    yield fixture
                finally:
                    active.clear()

            module = SimpleNamespace(preview_context=context, seed_cards=Mock(), preview_app=Mock(return_value=fixture), _ORIGIN=Mock())
            with patch.object(runner.importlib, "import_module", return_value=module) as imported:
                with runner.fixture_app(profile, "https://preview.example.test") as application:
                    self.assertTrue(active)
                    self.assertIs(application.app, fixture)
                self.assertFalse(active)
                imported.assert_called_once_with("scripts.preview_" + profile)
                self.assertEqual(module.seed_cards.call_count, int(profile == "cards"))

    def test_gateway_cli_binds_only_trusted_port_loopback(self):
        with patch.object(preview, "gateway_app", return_value=Mock()) as factory, patch("uvicorn.run") as serve:
            self.assertEqual(preview.main(["--serve", "--port", "58120"]), 0)
        self.assertEqual(serve.call_args.kwargs["host"], "127.0.0.1")
        self.assertFalse(serve.call_args.kwargs["access_log"])
        self.assertFalse(serve.call_args.kwargs["proxy_headers"])
        factory.assert_called_once()

    def test_runner_cannot_start_on_host(self):
        with patch.object(runner.sys, "platform", "win32"), self.assertRaises(RuntimeError):
            runner.main(["--profile", "workspace", "--public-origin", "https://preview.example.test"])


if __name__ == "__main__":
    unittest.main()
