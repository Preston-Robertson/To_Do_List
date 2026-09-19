"""Offline on-demand Assistant contracts with synthetic chat state."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from luigi_web import application as host, auth
from luigi_web.core.module_registry import build_registry
from luigi_web.modules.assistant import providers, routes


class AssistantPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "synthetic-panel-token"}))
        self.app = FastAPI()
        self.app.state.modules = build_registry("tasks,discipline,media,assistant")
        self.app.include_router(routes.router)
        self.app.middleware("http")(host.csrf_middleware)
        self.client = TestClient(self.app)

    def test_panel_requires_authentication(self) -> None:
        response = self.client.get("/chat/panel")
        self.assertEqual(response.status_code, 401)

    def test_opening_panel_does_not_send_chat_or_initialize_tools(self) -> None:
        with (
            patch.object(host, "_LLM_PROVIDER", SimpleNamespace(name="Example", model="Synthetic")),
            patch.object(providers, "get_history", return_value=[]),
            patch.object(providers, "run_chat_with_tools") as run_chat,
            patch.object(host.chat_tools, "build_registry") as build_tools,
        ):
            response = self.client.get("/chat/panel", headers={"Authorization": "Bearer synthetic-panel-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn('id="chat-panel"', response.text)
        run_chat.assert_not_called()
        build_tools.assert_not_called()

    def test_only_visible_messages_are_restored(self) -> None:
        history = [
            {"role": "system", "content": "internal instructions"},
            {"role": "tool", "content": "internal tool result"},
            {"role": "assistant", "content": "internal planning", "tool_calls": [{"id": "example"}]},
            {"role": "user", "content": "Example question"},
            {"role": "assistant", "content": "Example answer"},
        ]
        with (
            patch.object(host, "_LLM_PROVIDER", SimpleNamespace(name="Example", model="Synthetic")),
            patch.object(providers, "get_history", return_value=history),
        ):
            response = self.client.get("/chat/panel", headers={"Authorization": "Bearer synthetic-panel-token"})
        self.assertEqual(response.context["chat_messages"], history[-2:])
        self.assertNotIn("internal instructions", response.text)
        self.assertNotIn("internal tool result", response.text)
        self.assertIn("Example question", response.text)
        self.assertIn("Example answer", response.text)

    def test_history_is_escaped_and_disabled_panel_has_no_active_composer(self) -> None:
        with (
            patch.object(host, "_LLM_PROVIDER", providers.DisabledProvider()),
            patch.object(providers, "get_history", return_value=[{"role": "user", "content": "<script>example()</script>"}]),
        ):
            response = self.client.get("/chat/panel", headers={"Authorization": "Bearer synthetic-panel-token"})
        self.assertNotIn("<script>example()", response.text)
        self.assertIn("&lt;script&gt;", response.text)
        self.assertIn("chat-disabled", response.text)
        self.assertIn('onsubmit="return false;"', response.text)

    def test_send_and_reset_use_session_history_and_require_csrf(self) -> None:
        self.client.cookies.set(auth.COOKIE_NAME, "synthetic-panel-token")
        self.client.cookies.set(auth.CSRF_COOKIE_NAME, "synthetic-csrf")
        with patch.object(providers, "run_chat_with_tools") as run_chat:
            self.assertEqual(self.client.post("/chat", data={"message": "Example question"}).status_code, 403)
            self.assertEqual(self.client.post("/chat/reset").status_code, 403)
            run_chat.assert_not_called()
        history = []
        with (
            patch.dict(host.__dict__, {"_LLM_PROVIDER": SimpleNamespace(name="Example"), "_LLM_TOOLS": {}}),
            patch.object(host, "_require_v2"),
            patch.object(providers, "get_history", return_value=history),
            patch.object(providers, "trim_history"),
            patch.object(providers, "run_chat_with_tools", return_value=SimpleNamespace(reply="Example answer", tool_calls=[])) as run_chat,
            patch.object(providers, "reset_history") as reset,
        ):
            response = self.client.post("/chat", data={"message": "Example question"}, headers={"X-CSRF-Token": "synthetic-csrf"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("Example answer", response.text)
            self.assertEqual(history[-1]["content"], "Example question")
            run_chat.assert_called_once()
            response = self.client.post("/chat/reset", headers={"X-CSRF-Token": "synthetic-csrf"})
            self.assertEqual(response.status_code, 200)
            reset.assert_called_once_with("cookie:synthetic-panel-token")


if __name__ == "__main__":
    unittest.main()