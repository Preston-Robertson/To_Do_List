"""Offline synthetic preview checks, isolated from any host imported by discovery."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest.mock import patch


class FinancePreviewProcessTests(unittest.TestCase):
    def test_isolated_finance_preview(self):
        retained = {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT", "COMSPEC"}
        with tempfile.TemporaryDirectory(prefix="finance-preview-tests-") as temporary:
            directory = Path(temporary)
            environment = {key: value for key, value in os.environ.items() if key.upper() in retained}
            environment.update({key: temporary for key in (
                "TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR", "APPDATA", "LOCALAPPDATA", "HOME", "USERPROFILE",
            )})
            environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHON_DOTENV_DISABLED="1")
            result = subprocess.run(
                [sys.executable, "-W", "error::ResourceWarning", str(Path(__file__).resolve()), "--isolated"],
                cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True,
                text=True, timeout=180, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("failures=0 errors=0", result.stdout)
            self.assertFalse(any(directory.iterdir()), "Preview left temporary files behind")
        self.assertFalse(directory.exists())


def isolated_suite():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import preview_finance as preview

    class SyntheticFinanceTests(unittest.TestCase):
        def setUp(self):
            self.context = preview.preview_context()
            self.app = self.context.__enter__()
            self.directory = self.app.state.preview_directory
            self.addCleanup(self.close_context)
            self.client = preview.preview_client(self.app)
            self.client.__enter__()
            self.addCleanup(self.client.__exit__, None, None, None)
            self.ui_name, self.finance_name, self.csrf_name = preview.cookie_names(58111)

        def close_context(self):
            self.context.__exit__(None, None, None)
            self.assertFalse(self.directory.exists(), "Synthetic storage was not removed")

        def bootstrap(self):
            response = self.client.get("/", follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.private(response)
            return response

        def headers(self):
            return {"origin": "http://127.0.0.1:58111", "x-csrf-token": self.client.cookies[self.csrf_name]}

        def private(self, response):
            self.assertTrue(all(response.headers.get(key) == value for key, value in preview.PRIVATE_HEADERS.items()))

        def post(self, path, expected=200, **kwargs):
            response = self.client.post(path, headers=self.headers(), follow_redirects=False, **kwargs)
            self.assertEqual(response.status_code, expected)
            self.private(response)
            return response

        def test_smoke_all_composed_views_and_planning(self):
            counts = preview.smoke_check(self.app)
            self.assertEqual(counts, {"views": 14, "mutations": 12})

        def test_environment_and_module_lifecycle_are_isolated(self):
            from luigi_web.modules.finance import repository as finance

            self.assertEqual(os.environ["LUIGI_WEB_MODULES"], "finance")
            self.assertEqual(os.environ["PYTHON_DOTENV_DISABLED"], "1")
            self.assertEqual(Path(os.environ["LUIGI_WEB_DATA_DIR"]), self.directory)
            self.assertTrue(finance.db_path().is_relative_to(self.directory))
            self.assertFalse(Path(os.environ["LUIGI_WEB_MODULES_FILE"]).exists())
            for key in ("APPDATA", "LOCALAPPDATA", "TMP", "TEMP", "SQLITE_TMPDIR"):
                self.assertEqual(Path(os.environ[key]), self.directory)
            if os.name == "nt":
                self.assertTrue(all(os.environ.get(key) for key in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR")))
            self.assertEqual([module.id for module in self.app.state.modules.enabled], ["finance"])
            self.assertEqual(self.app.state.module_status, {"finance": "enabled"})
            self.assertEqual(self.app.state.module_cleanup, [])
            self.assertFalse(self.app.dependency_overrides)
            self.assertNotIn("luigi_web.application", sys.modules)
            self.assertNotIn("luigi_web.modules.tasks.repository", sys.modules)

        def test_synthetic_seed_and_private_pagination(self):
            from luigi_web.modules.finance import overview, plans, repository as finance

            state = overview.overview_state()
            self.assertEqual(state["filtered"]["count"], 65)
            self.assertEqual(state["pages"], 2)
            self.assertEqual(len(state["transactions"]), 50)
            self.assertEqual(len(overview.overview_state(page=2)["transactions"]), 15)
            self.assertEqual(len(finance.list_transactions(limit=None)), 81)
            self.assertEqual(len({row["transaction_date"][:7] for row in finance.list_transactions(limit=None)}), 3)
            self.assertEqual(len(finance.list_accounts()), 3)
            self.assertEqual(len(finance.list_holdings()), 2)
            self.assertEqual(len(finance.list_net_worth_snapshots()), 3)
            self.assertEqual(len(plans.list_income()), 2)
            self.assertEqual(len(plans.list_plans("cashflow")), 2)
            self.assertEqual(len(plans.list_plans("housing")), 1)
            self.assertTrue(any(row["amount_minor"] > 0 for row in finance.list_recurring_items()))
            self.assertTrue(any(row["amount_minor"] < 0 for row in finance.list_recurring_items()))
            self.bootstrap()
            self.post("/finance/overview/filter", data={"page": "2"})
            self.post("/finance/overview/filter", data={"query": "No synthetic match"})
            self.assertEqual(self.client.get("/finance?month=2000-01").status_code, 400)

        def test_real_auth_dependencies_and_production_cookies(self):
            from luigi_web import auth

            self.client.cookies.set(auth.COOKIE_NAME, os.environ["LUIGI_WEB_UI_TOKEN"])
            self.client.cookies.set(auth.FINANCE_COOKIE_NAME, auth._finance_session_value())
            self.client.cookies.set(auth.CSRF_COOKIE_NAME, "untouched-production-placeholder")
            self.assertEqual(self.client.get("/finance/records").status_code, 401)
            self.assertEqual(self.client.get("/finance", headers={
                "authorization": "Bearer " + os.environ["LUIGI_WEB_UI_TOKEN"],
            }).status_code, 401)
            response = self.bootstrap()
            outgoing = response.headers.get_list("set-cookie")
            self.assertTrue(all(header.startswith(preview.cookie_names(58111)) for header in outgoing))
            self.assertTrue(all("SameSite=strict" in header for header in outgoing))
            self.assertTrue(all("HttpOnly" in header for header in outgoing if not header.startswith(self.csrf_name)))
            with patch.object(auth, "_token_matches", wraps=auth._token_matches) as main_check, patch.object(
                auth, "is_finance_authenticated", wraps=auth.is_finance_authenticated,
            ) as finance_check:
                self.assertEqual(self.client.get("/finance/records").status_code, 200)
                self.assertTrue(main_check.called and finance_check.called)
            self.assertEqual(self.client.cookies[auth.COOKIE_NAME], os.environ["LUIGI_WEB_UI_TOKEN"])
            self.assertEqual(self.client.cookies[auth.FINANCE_COOKIE_NAME], auth._finance_session_value())
            self.assertEqual(self.client.cookies[auth.CSRF_COOKIE_NAME], "untouched-production-placeholder")
            self.assertFalse(self.client.get("/", follow_redirects=False).headers.get_list("set-cookie"))

        def test_cookies_are_port_scoped(self):
            self.bootstrap()
            with preview.preview_client(self.app, port=58112) as other:
                other.cookies.update(self.client.cookies)
                self.assertEqual(other.get("/finance").status_code, 401)
                self.assertEqual(other.get("/").status_code, 200)
                self.assertTrue(all(name in other.cookies for name in preview.cookie_names(58112)))
            self.app.state.preview_port = 58111
            with preview.preview_client(self.app, port=58112) as wrong:
                self.assertEqual(wrong.get("/").status_code, 403)

        def test_lock_preserves_main_session_until_explicit_root_restart(self):
            from luigi_web.modules.finance import repository as finance

            self.bootstrap()
            main_cookie = self.client.cookies[self.ui_name]
            locked = self.post("/finance/lock", expected=303)
            self.assertTrue(any(header.startswith(self.finance_name + "=") and "Max-Age=0" in header
                                for header in locked.headers.get_list("set-cookie")))
            self.assertFalse(any(header.startswith("luigi_finance_session=") for header in locked.headers.get_list("set-cookie")))
            self.assertNotIn(self.finance_name, self.client.cookies)
            self.assertEqual(self.client.cookies[self.ui_name], main_cookie)
            with patch.object(finance, "connect", side_effect=AssertionError("Locked read")) as database:
                for path in ("/finance", "/finance/records", "/finance/planning", "/finance/planning/state", "/finance/backup"):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 403)
                    self.private(response)
                response = self.client.get("/finance/unlock")
                self.assertEqual(response.status_code, 200)
                self.assertIn('href="/"', response.text)
                self.assertIn("Start synthetic preview", response.text)
                self.assertFalse(database.called)
            self.assertNotIn(self.finance_name, self.client.cookies)
            self.assertEqual(self.client.post("/finance/unlock", data={"token": "synthetic-rejected"},
                                              headers=self.headers()).status_code, 403)
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertIn(self.finance_name, self.client.cookies)

        def test_main_and_finance_sessions_are_independently_required(self):
            self.bootstrap()
            self.client.cookies.delete(self.ui_name)
            self.assertEqual(self.client.get("/finance").status_code, 401)
            self.assertEqual(self.client.get("/finance/unlock").status_code, 401)
            self.bootstrap()
            self.client.cookies.delete(self.finance_name)
            self.assertEqual(self.client.get("/finance").status_code, 403)
            response = self.client.get("/finance", headers={"accept": "text/html"}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers.get("location"), "/finance/unlock")

        def test_csrf_origin_referer_and_fetch_metadata(self):
            self.bootstrap()
            before = preview.ledger_metadata()
            cases = ({}, {"origin": "http://127.0.0.1:58111"},
                     {"x-csrf-token": self.client.cookies[self.csrf_name]},
                     {**self.headers(), "origin": "http://example.invalid"},
                     {**self.headers(), "origin": "null"},
                     {**self.headers(), "referer": "http://example.invalid/"},
                     {**self.headers(), "sec-fetch-site": "cross-site"},
                     {**self.headers(), "sec-fetch-site": "same-site"},
                     {**self.headers(), "x-csrf-token": "incorrect-synthetic-csrf"})
            for headers in cases:
                response = self.client.post("/finance/snapshot", headers=headers)
                self.assertEqual(response.status_code, 403)
                self.private(response)
            self.assertEqual(preview.ledger_metadata(), before)
            response = self.client.post("/finance/overview/filter", data={"page": "2"}, headers={
                "referer": "http://127.0.0.1:58111/finance", "x-csrf-token": self.client.cookies[self.csrf_name],
            })
            self.assertEqual(response.status_code, 200)
            response = self.client.post("/finance/planning/calculate", json={}, headers={
                "authorization": "Bearer " + os.environ["LUIGI_WEB_UI_TOKEN"],
            })
            self.assertEqual(response.status_code, 403)

        def test_loopback_host_and_client_restrictions(self):
            for host in ("example.invalid", "localhost:58111", "127.0.0.1:58111.example.invalid"):
                response = self.client.get("/", headers={"host": host})
                self.assertEqual(response.status_code, 403)
                self.private(response)
            with preview.preview_client(self.app, client_host="192.0.2.1") as remote:
                response = remote.get("/", headers={"x-forwarded-for": "127.0.0.1"})
                self.assertEqual(response.status_code, 403)
            for headers in ({"sec-fetch-site": "cross-site"}, {"origin": "http://example.invalid"},
                            {"referer": "http://example.invalid/"}):
                self.assertEqual(self.client.get("/", headers=headers).status_code, 403)

        def test_unmounted_paths_queries_and_private_errors(self):
            self.bootstrap()
            for path in ("/command-palette", "/chat", "/tasks", "/api/tasks", "/admin", "/modules",
                         "/openapi.json", "/docs", "/finance/arbitrary", "/finance/../admin"):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self.private(response)
            for path in ("/finance?token=synthetic", "/finance/records?month=2000-01",
                         "/finance/planning/state?amount=1234", "/?token=synthetic"):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 400)
                self.private(response)
            for path in ("/finance/restore", "/finance/unlock"):
                self.assertEqual(self.client.post(path, headers=self.headers()).status_code, 403)
            self.assertEqual(self.client.put("/finance/accounts", headers=self.headers()).status_code, 404)

        def test_errors_mask_body_paths_tokens_and_exception_messages(self):
            from luigi_web.modules.finance import repository as finance

            self.bootstrap()
            marker = "synthetic-sensitive-error-marker"
            with patch.object(finance, "dashboard", side_effect=RuntimeError(marker)):
                response = self.client.get("/finance/records")
                self.assertEqual(response.status_code, 500)
                self.private(response)
                self.assertNotIn(marker, response.text)
            response = self.client.post("/finance/planning/save", content=marker, headers={
                **self.headers(), "content-type": "application/json",
            })
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(marker, response.text)
            self.private(response)
            self.assertNotIn(str(self.directory), response.text)
            for path in ("/finance", "/finance/planning", "/finance/records", "/finance/unlock", "/finance/backup"):
                response = self.client.get(path)
                self.assertTrue(all(os.environ[key] not in response.text for key in (
                    "LUIGI_WEB_UI_TOKEN", "LUIGI_WEB_FINANCE_TOKEN",
                )))
            response = self.client.post("/finance/transactions", content=b"x" * 2_100_001, headers=self.headers())
            self.assertEqual(response.status_code, 413)
            self.private(response)

        def test_sqlite_is_confined_including_attach_and_uri(self):
            import sqlite3

            outside = self.directory.parent / "nonexistent-forbidden-preview.sqlite3"
            for connect in (sqlite3.connect, sqlite3.dbapi2.connect):
                with self.assertRaises(RuntimeError):
                    connect(outside)
                with self.assertRaises(RuntimeError):
                    connect("file::memory:", uri=True)
                with closing(connect(":memory:")) as connection:
                    self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
                    with self.assertRaises(sqlite3.DatabaseError):
                        connection.execute("ATTACH DATABASE ? AS forbidden", (str(outside),))
            self.assertFalse(outside.exists())

        def test_network_providers_engines_and_dotenv_are_blocked(self):
            import dotenv
            import httpx
            import socket
            import sqlalchemy

            for operation in (
                lambda: socket.getaddrinfo("example.invalid", 443),
                lambda: socket.create_connection(("127.0.0.1", 58111)),
                lambda: sqlalchemy.create_engine("sqlite:///:memory:"),
                lambda: dotenv.load_dotenv(), lambda: dotenv.dotenv_values(),
                lambda: dotenv.main.load_dotenv(), lambda: httpx.get("http://example.invalid/"),
            ):
                with self.assertRaises(RuntimeError):
                    operation()
            with socket.socket() as connection:
                with self.assertRaises(RuntimeError):
                    connection.connect(("127.0.0.1", 58111))
                with self.assertRaises(RuntimeError):
                    connection.connect_ex(("127.0.0.1", 58111))
            for name in ("psycopg", "psycopg2"):
                driver = sys.modules.get(name)
                if driver:
                    with self.assertRaises(RuntimeError):
                        driver.connect("")
            pair = socket.socketpair()
            for connection in pair:
                connection.close()

        def test_legacy_writes_verify_in_temporary_repository(self):
            from luigi_web import clock
            from luigi_web.modules.finance import repository as finance

            self.bootstrap()
            self.post("/finance/accounts", expected=204, data={"name": "Example added account", "opening_balance": "12.34"})
            account = next(row for row in finance.list_accounts() if row["name"] == "Example added account")
            self.assertEqual(account["opening_balance_minor"], 1234)
            self.post(f"/finance/accounts/{account['id']}", expected=204,
                      data={"name": "Example renamed account", "opening_balance": "123.45"})
            self.assertEqual(finance.get_account(account["id"])["opening_balance_minor"], 12345)
            self.post("/finance/transactions", expected=204, data={"account_id": account["id"],
                      "transaction_date": clock.local_today().isoformat(), "amount": "-12.34", "category": "Example purchase"})
            transaction = finance.list_transactions(account_id=account["id"])[0]
            self.assertEqual(transaction["amount_minor"], -1234)
            self.post(f"/finance/transactions/{transaction['id']}/delete", expected=204)
            self.assertFalse(finance.list_transactions(account_id=account["id"]))
            self.post("/finance/budgets", expected=204, data={"category": "Example added budget", "limit": "12.34"})
            budget = next(row for row in finance.list_budgets(finance.month_value()) if row["category"] == "Example added budget")
            self.post(f"/finance/budgets/{budget['id']}/delete", expected=204)
            self.post("/finance/holdings", expected=204, data={"account_id": account["id"], "symbol": "DEMO3",
                      "quantity": "1", "cost_basis": "12.34", "market_value": "123.45"})
            holding = next(row for row in finance.list_holdings() if row["symbol"] == "DEMO3")
            self.post(f"/finance/holdings/{holding['id']}/delete", expected=204)
            self.post("/finance/recurring", expected=204, data={"name": "Example added schedule", "category": "Example bill",
                      "amount": "-12.34", "next_due_date": clock.local_today().isoformat()})
            recurring = next(row for row in finance.list_recurring_items() if row["name"] == "Example added schedule")
            self.post(f"/finance/recurring/{recurring['id']}/delete", expected=204)
            self.post("/finance/reports", expected=204, data={"name": "Example added report"})
            report = next(row for row in finance.list_saved_reports() if row["name"] == "Example added report")
            self.post(f"/finance/reports/{report['id']}/delete", expected=204)
            self.post("/finance/snapshot", expected=204)

        def test_planned_income_versioned_write_leaves_ledger_unchanged(self):
            from luigi_web import clock
            from luigi_web.modules.finance import plans

            self.bootstrap()
            before = preview.ledger_metadata()
            payload = {"name": "Example new income", "amount_minor": 1234,
                       "cadence": "monthly", "start_date": clock.local_today().isoformat()}
            saved = self.post("/finance/planning/income/save", json=payload).json()["record"]
            update = {**payload, "id": saved["id"], "expected_version": saved["version"], "amount_minor": 12345}
            updated = self.post("/finance/planning/income/save", json=update).json()["record"]
            self.assertEqual(updated["version"], 2)
            self.assertEqual(next(row for row in plans.list_income() if row["id"] == saved["id"])["amount_minor"], 12345)
            self.post("/finance/planning/income/save", expected=409, json=update)
            self.post("/finance/planning/income/delete", json={"id": updated["id"], "expected_version": 2})
            self.assertEqual(preview.ledger_metadata(), before)

        def test_import_fixture_only_and_no_raw_upload_retention(self):
            from luigi_web.modules.finance import repository as finance

            self.bootstrap()
            account = finance.list_accounts()[0]
            before = len(finance.list_transactions(limit=None))
            response = self.post("/finance/import/preview", data={"account_id": account["id"]},
                                 files={"file": ("synthetic.csv", preview.synthetic_csv(), "text/csv")})
            self.assertEqual(len(finance.list_transactions(limit=None)), before)
            self.assertEqual(len(finance._IMPORT_CACHE), 1)
            token = next(iter(finance._IMPORT_CACHE))
            self.post("/finance/import/commit", expected=204, data={"token": token})
            self.assertEqual(len(finance.list_transactions(limit=None)), before + 1)
            self.assertFalse(finance._IMPORT_CACHE)
            self.post("/finance/import/commit", expected=422, data={"token": token})
            self.post("/finance/import/preview", expected=422, data={"account_id": account["id"]},
                      files={"file": ("synthetic.csv", b"not the generated fixture", "text/csv")})
            self.assertFalse(any(path.suffix == ".csv" for path in self.directory.rglob("*")))
            self.private(response)

        def test_backup_and_export_are_synthetic_downloads_only(self):
            self.bootstrap()
            for path in ("/finance/backup", "/finance/export.csv"):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("attachment", response.headers.get("content-disposition", ""))
                self.private(response)
            self.assertEqual(self.client.post("/finance/restore", headers=self.headers(),
                            files={"file": ("synthetic.json", b"{}", "application/json")}).status_code, 403)

        def test_scoped_csrf_asset_and_packaged_finance_assets(self):
            self.bootstrap()
            response = self.client.get("/static/js/app.js")
            self.assertEqual(response.status_code, 200)
            self.assertIn(self.csrf_name, response.text)
            self.assertNotIn('readCookie("luigi_csrf")', response.text)
            self.private(response)
            from importlib.resources import files as module_files

            root = Path(str(module_files("luigi_web.modules.finance"))) / "static"
            assets = [path for path in root.rglob("*") if path.is_file()]
            self.assertTrue(assets)
            for asset in assets:
                response = self.client.get("/module-assets/finance/" + asset.relative_to(root).as_posix())
                self.assertEqual(response.status_code, 200)
                self.private(response)

    return unittest.defaultTestLoader.loadTestsFromTestCase(SyntheticFinanceTests)


class PrivateResult(unittest.TestResult):
    def addError(self, test, error):
        super().addError(test, error)
        self.report(test, error)

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.report(test, error)

    @staticmethod
    def report(test, error):
        frames = traceback.extract_tb(error[2])
        locations = ", ".join(f"{frame.name}:{frame.lineno}" for frame in frames[-3:])
        print(f"{test._testMethodName}: {error[0].__name__} ({locations})")


if __name__ == "__main__":
    if "--isolated" in sys.argv:
        result = PrivateResult()
        isolated_suite().run(result)
        print(f"Synthetic regressions: tests={result.testsRun} failures={len(result.failures)} errors={len(result.errors)}")
        raise SystemExit(not result.wasSuccessful())
    unittest.main()