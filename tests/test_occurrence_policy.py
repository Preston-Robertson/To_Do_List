"""Scheduler ownership remains deployment-managed and status hides internals."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from luigi_web import application as host
from luigi_web.modules.admin import environment


class OccurrencePolicyTests(unittest.TestCase):
    def test_ownership_cannot_be_changed_by_admin_editor(self) -> None:
        self.assertIn("LUIGI_WEB_RECURRENCE_OWNER", environment.PROTECTED_KEYS)
        self.assertNotIn("LUIGI_WEB_RECURRENCE_OWNER", {key.name for key in environment.KNOWN_KEYS})

    def test_external_default_is_visible_and_requires_no_storage(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch.object(host, "_RECURRENCE_ERROR", None):
            state = host.recurrence_status()
        self.assertEqual(state["owner"], "external")
        self.assertFalse(state["enabled"])

    def test_generation_error_is_generic_and_reported(self) -> None:
        with patch.dict(os.environ, {"LUIGI_WEB_RECURRENCE_OWNER": "web"}), patch.object(host, "_RECURRENCE_ERROR", None), \
             patch.object(host.db, "reactivate_due_recurring", side_effect=RuntimeError("synthetic private failure")), \
             self.assertLogs("luigi_web.app", "WARNING") as captured:
            host._reactivate_recurring()
            state = host.recurrence_status()
        self.assertTrue(state["error"])
        self.assertIn("not reset", state["message"])
        self.assertNotIn("synthetic private failure", str(state) + str(captured.output))

    def test_success_clears_error_and_invalid_policy_is_safe(self) -> None:
        with patch.object(host, "_RECURRENCE_ERROR", "previous error"), patch.object(host.db, "reactivate_due_recurring", return_value=1), \
             patch.dict(os.environ, {"LUIGI_WEB_RECURRENCE_OWNER": "web"}):
            host._reactivate_recurring()
            self.assertFalse(host.recurrence_status()["error"])
        with patch.dict(os.environ, {"LUIGI_WEB_RECURRENCE_OWNER": "synthetic-invalid-owner"}):
            state = host.recurrence_status()
        self.assertTrue(state["error"])
        self.assertFalse(state["enabled"])
        self.assertNotIn("synthetic-invalid-owner", str(state))


if __name__ == "__main__":
    unittest.main()