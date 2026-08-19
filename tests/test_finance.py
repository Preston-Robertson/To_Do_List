"""Offline privacy, storage, and route checks for the Finance domain."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import Request

from luigi_web import application as app
from luigi_web import auth, chat_tools, finance


class FinanceRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "LUIGI_WEB_FINANCE_DB": os.path.join(self.temp_dir.name, "finance.db"),
            "LUIGI_WEB_FINANCE_BASE_CURRENCY": "USD",
        })
        self.env.start()
        finance._IMPORT_CACHE.clear()
        finance.init_db()

    def tearDown(self) -> None:
        finance._IMPORT_CACHE.clear()
        self.env.stop()
        self.temp_dir.cleanup()

    def create_account(self) -> str:
        return finance.create_account({
            "name": "Everyday",
            "account_type": "checking",
            "currency": "USD",
            "opening_balance": "100.00",
        })

    def test_money_uses_exact_minor_units(self) -> None:
        self.assertEqual(finance.to_minor(12), 1200)
        self.assertEqual(finance.to_minor("12.345"), 1235)
        self.assertEqual(finance.to_minor("($1,234.56)"), -123456)
        self.assertEqual(finance.from_minor(-123456), "-1234.56")

    def test_probable_pii_is_rejected(self) -> None:
        email_shaped = "name" + "@" + "example.test"
        identifier_shaped = "123" + "-" + "45" + "-" + "6789"
        for unsafe in (
            email_shaped,
            identifier_shaped,
            "Account 1234567890",
            "1234-5678-9012-3456",
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(ValueError, "personal|identifier"):
                    finance.safe_label(unsafe, "alias")
        self.assertEqual(finance.safe_label("Everyday account", "alias"), "Everyday account")

    def test_dashboard_aggregates_all_rows_but_limits_visible_ledger(self) -> None:
        account_id = self.create_account()
        for index in range(60):
            finance.create_transaction({
                "account_id": account_id,
                "transaction_date": "2026-08-01",
                "amount": "-1.00",
                "category": "General",
                "memo": f"entry {index}",
            })

        state = finance.dashboard("2026-08")

        self.assertEqual(len(state["transactions"]), 50)
        self.assertEqual(state["expense_minor"], 6000)
        self.assertEqual(state["cash_minor"], 4000)

    def test_budget_upsert_is_case_insensitive(self) -> None:
        first_id = finance.upsert_budget({
            "month": "2026-08", "category": "Groceries", "limit": "100.00",
        })
        second_id = finance.upsert_budget({
            "month": "2026-08", "category": "groceries", "limit": "125.00",
        })

        budgets = finance.list_budgets("2026-08")
        self.assertEqual(second_id, first_id)
        self.assertEqual(len(budgets), 1)
        self.assertEqual(budgets[0]["limit_minor"], 12500)

    def test_inactive_accounts_remain_manageable_but_leave_totals(self) -> None:
        account_id = self.create_account()
        finance.update_account(account_id, {
            "name": "Everyday",
            "account_type": "checking",
            "currency": "USD",
            "opening_balance": "100.00",
            "active": "0",
        })

        state = finance.dashboard("2026-08")

        self.assertEqual(len(state["accounts"]), 1)
        self.assertEqual(state["accounts"][0]["active"], 0)
        self.assertEqual(state["cash_minor"], 0)

    def test_mixed_currency_account_is_rejected_without_exchange_rates(self) -> None:
        with self.assertRaisesRegex(ValueError, "base currency"):
            finance.create_account({
                "name": "Travel",
                "account_type": "cash",
                "currency": "EUR",
                "opening_balance": "11.00",
            })

    def test_storage_health_exposes_metadata_only(self) -> None:
        self.create_account()

        detail = finance.storage_health()

        self.assertEqual(detail, "SQLite storage reachable; base currency USD")
        self.assertNotIn("Everyday", detail)
        self.assertNotIn(self.temp_dir.name, detail)

    def test_csv_preview_deduplicates_before_commit(self) -> None:
        account_id = self.create_account()
        content = (
            "date,amount,category,memo\n"
            "2026-08-01,-12.34,Groceries,Weekly shop\n"
            "2026-08-01,-12.34,Groceries,Weekly shop\n"
        ).encode()

        preview = finance.prepare_csv_import(account_id, content)
        result = finance.commit_csv_import(preview["token"])

        self.assertEqual(preview["duplicates"], 1)
        self.assertEqual(result, {"imported": 1, "skipped": 1})
        self.assertEqual(len(finance.list_transactions()), 1)

    def test_csv_export_requests_all_transactions(self) -> None:
        with patch.object(finance, "list_transactions", return_value=[]) as list_rows:
            finance.transactions_csv("2026-08")
        list_rows.assert_called_once_with(month="2026-08", limit=None)

    def test_audit_history_excludes_financial_values_and_labels(self) -> None:
        account_id = self.create_account()
        finance.create_transaction({
            "account_id": account_id,
            "transaction_date": "2026-08-01",
            "amount": "-42.50",
            "category": "Groceries",
            "memo": "Weekly shop",
        })

        serialized = json.dumps(finance.list_audit_events())

        self.assertNotIn("42.50", serialized)
        self.assertNotIn("Groceries", serialized)
        self.assertNotIn("Weekly shop", serialized)
        self.assertIn("Created transaction", serialized)

    def test_restore_revalidates_aliases(self) -> None:
        self.create_account()
        payload = finance.backup_payload()
        payload["tables"]["finance_accounts"][0]["name"] = (
            "name" + "@" + "example.test"
        )

        with self.assertRaisesRegex(ValueError, "personal identifying information"):
            finance.restore_backup(payload)

    def test_app_backup_round_trips_all_finance_features(self) -> None:
        account_id = self.create_account()
        finance.create_transaction({
            "account_id": account_id,
            "transaction_date": "2026-08-01",
            "amount": "-20.00",
            "category": "Groceries",
            "memo": "Weekly shop",
        })
        finance.upsert_budget({
            "month": "2026-08", "category": "Groceries", "limit": "100.00",
        })
        finance.upsert_holding({
            "account_id": account_id,
            "symbol": "EXAMPLE",
            "asset_name": "Index holding",
            "quantity": "2.5",
            "cost_basis": "100.00",
            "market_value": "110.00",
            "as_of_date": "2026-08-01",
        })
        finance.upsert_recurring_item({
            "account_id": account_id,
            "name": "Housing",
            "category": "Housing",
            "amount": "-75.00",
            "cadence": "monthly",
            "next_due_date": "2026-09-01",
        })
        finance.save_report({
            "name": "August cash flow",
            "report_type": "monthly",
            "month": "2026-08",
        })
        finance.save_net_worth_snapshot("2026-08-02")
        payload = finance.backup_payload()

        restored_path = os.path.join(self.temp_dir.name, "restored.db")
        with patch.dict(os.environ, {"LUIGI_WEB_FINANCE_DB": restored_path}):
            counts = finance.restore_backup(payload)
            restored = finance.dashboard("2026-08")
            reports = finance.list_saved_reports()
            snapshots = finance.list_net_worth_snapshots()

        self.assertEqual(counts["finance_accounts"], 1)
        self.assertEqual(restored["expense_minor"], 2000)
        self.assertEqual(len(restored["budgets"]), 1)
        self.assertEqual(len(restored["holdings"]), 1)
        self.assertEqual(len(restored["recurring_items"]), 1)
        self.assertEqual(len(reports), 1)
        self.assertEqual(len(snapshots), 1)


class FinanceSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "LUIGI_WEB_UI_TOKEN": "main-secret",
            "LUIGI_WEB_FINANCE_TOKEN": "finance-secret",
            "LUIGI_WEB_FINANCE_DB": os.path.join(self.temp_dir.name, "finance.db"),
            "LUIGI_WEB_SECURE_COOKIES": "0",
        })
        self.env.start()
        finance.init_db()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp_dir.cleanup()

    def test_finance_cookie_uses_derived_session_value(self) -> None:
        response = auth.finance_unlock_response("finance-secret")
        cookie = response.headers["set-cookie"]

        self.assertNotIn("finance-secret", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertTrue(auth.is_finance_authenticated(auth._finance_session_value()))

    def test_unconfigured_finance_unlock_field_explains_host_boundary(self) -> None:
        client = TestClient(app.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")
        client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        with patch.dict(os.environ, {"LUIGI_WEB_FINANCE_TOKEN": ""}):
            page = client.get("/finance/unlock")
            self.assertEqual(page.status_code, 200)
            self.assertIn('name="token"', page.text)
            self.assertNotIn('name="token" required autocomplete="current-password" disabled', page.text)

            response = client.post(
                "/finance/unlock",
                data={"token": "candidate"},
                headers={"X-CSRF-Token": "csrf-value", "HX-Request": "true"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("does not create or save one", response.text)

    def test_secure_cookie_flag_is_configurable(self) -> None:
        with patch.dict(os.environ, {"LUIGI_WEB_SECURE_COOKIES": "1"}):
            self.assertIn("Secure", auth.login_response("main-secret").headers["set-cookie"])
            self.assertIn("Secure", auth.finance_unlock_response("finance-secret").headers["set-cookie"])

    def test_cookie_authenticated_mutation_requires_csrf(self) -> None:
        client = TestClient(app.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")

        rejected = client.post(
            "/finance/unlock",
            data={"token": "finance-secret"},
            follow_redirects=False,
        )
        self.assertEqual(rejected.status_code, 403)

        client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        accepted = client.post(
            "/finance/unlock",
            data={"token": "finance-secret"},
            headers={"X-CSRF-Token": "csrf-value"},
            follow_redirects=False,
        )
        self.assertEqual(accepted.status_code, 303)
        self.assertNotIn("finance-secret", accepted.headers["set-cookie"])

        htmx_accepted = client.post(
            "/finance/unlock",
            data={"token": "finance-secret"},
            headers={"X-CSRF-Token": "csrf-value", "HX-Request": "true"},
            follow_redirects=False,
        )
        self.assertEqual(htmx_accepted.status_code, 200)
        self.assertEqual(htmx_accepted.headers["HX-Redirect"], "/finance")

    def test_finance_dashboard_requires_both_sessions(self) -> None:
        client = TestClient(app.app)
        client.cookies.set(auth.COOKIE_NAME, "main-secret")

        locked = client.get("/finance", headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(locked.status_code, 303)
        self.assertEqual(locked.headers["location"], "/finance/unlock")

        client.cookies.set(auth.FINANCE_COOKIE_NAME, auth._finance_session_value())
        unlocked = client.get("/finance", headers={"Accept": "text/html"})
        self.assertEqual(unlocked.status_code, 200)
        self.assertIn("Privacy boundary", unlocked.text)
        self.assertEqual(unlocked.headers["Cache-Control"], "no-store")

        notifications = client.get(
            "/finance/notifications", headers={"Accept": "text/html"}
        )
        self.assertEqual(notifications.status_code, 200)
        self.assertIn("Finance notifications", notifications.text)

    def test_finance_is_absent_from_llm_and_command_palette(self) -> None:
        registry = chat_tools.build_registry()
        self.assertFalse(any("finance" in name.lower() for name in registry))

        request = Request({
            "type": "http", "method": "GET", "path": "/command-palette",
            "query_string": b"", "headers": [],
        })
        body = app.command_palette_results(request, "").body.decode()
        self.assertNotIn("Finance", body)


if __name__ == "__main__":
    unittest.main()
