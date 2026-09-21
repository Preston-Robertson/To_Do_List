"""Cold-process, synthetic-only checks of Finance in the actual application host."""
from __future__ import annotations

from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import date
from http.cookies import SimpleCookie
from html.parser import HTMLParser
import inspect
import io
import json
import logging
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urljoin, urlsplit
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
HOST = "http://testserver"
BUILTINS = "tasks,discipline,planning,media,cards,characters,finance,assistant,admin,preview,feedback"


class PageAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = set()
        self.finance_links = set()
        self.finance_forms = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"script", "img", "link"}:
            source = attributes.get("src") or attributes.get("href")
            if source:
                self.assets.add(source)
        self.assets.update(re.findall(r"url\(['\"]?([^'\")]+)", attributes.get("style", "")))
        if tag == "a" and attributes.get("href", "").startswith("/finance"):
            self.finance_links.add(attributes["href"])
        if tag == "form" and attributes.get("action", "").startswith("/finance"):
            self.finance_forms.append(attributes)

    def handle_data(self, data):
        self.text.append(data)


class FinanceUpgradeProcessTests(unittest.TestCase):
    def test_actual_host_in_isolated_process(self):
        retained = {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR"}
        with TemporaryDirectory(prefix="finance-upgrade-tests-") as temporary:
            directory = Path(temporary)
            environment = {key: value for key, value in os.environ.items() if key.upper() in retained}
            environment.update({key: temporary for key in (
                "TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR", "APPDATA", "LOCALAPPDATA", "HOME", "USERPROFILE",
            )})
            environment.update(
                PYTHONDONTWRITEBYTECODE="1", PYTHON_DOTENV_DISABLED="1",
                LUIGI_WEB_DATA_DIR=temporary, LUIGI_WEB_MODULES=BUILTINS,
                LUIGI_WEB_MODULES_FILE=str(directory / "absent-modules.json"),
                LUIGI_WEB_UI_TOKEN="synthetic-main", LUIGI_WEB_FINANCE_TOKEN="synthetic-unlock",
                LUIGI_WEB_FINANCE_BASE_CURRENCY="USD", LUIGI_WEB_TIMEZONE="UTC",
                LUIGI_WEB_SECURE_COOKIES="0",
            )
            result = subprocess.run(
                [sys.executable, "-B", "-W", "error::ResourceWarning", str(Path(__file__).resolve()), "--isolated"],
                cwd=directory, env=environment, capture_output=True, text=True, timeout=180, check=False,
            )
        self.assertFalse(directory.exists(), "Temporary storage was not removed")
        try:
            summary = json.loads(result.stdout)
        except (ValueError, TypeError):
            self.fail("Isolated runner did not produce a sanitized summary")
        self.assertEqual(result.returncode, 0, summary)
        self.assertEqual(summary["failures"], 0)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["tests"], 8)
        self.assertFalse(result.stderr, "Isolated runner emitted unexpected diagnostics")


def _blocked(*args, **kwargs):
    raise RuntimeError("Non-fixture access blocked")


def _install_audit_guard(directory):
    pair_code = getattr(socket.socketpair, "__code__", None)

    def audit(event, arguments):
        if event == "sqlite3.connect":
            database = os.fsdecode(arguments[0])
            if database != ":memory:" and (
                not database or database.startswith("file:")
                or not Path(database).resolve().is_relative_to(directory)
            ):
                _blocked()
        elif event == "socket.connect":
            frame = sys._getframe(1)
            while frame is not None:
                if pair_code is not None and frame.f_code is pair_code:
                    address = arguments[1]
                    if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
                        return
                frame = frame.f_back
            _blocked()
        elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.sendto"}:
            _blocked()
        elif event == "open" and isinstance(arguments[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(arguments[0])).resolve()
            if path.name == ".env" or "finance_tester" in str(path).lower() or path.is_relative_to(ROOT / "data"):
                _blocked()

    sys.addaudithook(audit)


def _isolated_suite(directory, stack):
    sys.path.insert(0, str(ROOT))
    _install_audit_guard(directory)
    import dotenv
    import dotenv.main
    import sqlalchemy

    for module in (dotenv, dotenv.main):
        stack.enter_context(patch.object(module, "load_dotenv", return_value=False))
        stack.enter_context(patch.object(module, "dotenv_values", return_value={}))
        stack.enter_context(patch.object(module, "find_dotenv", return_value=""))
    stack.enter_context(patch.object(sqlalchemy, "create_engine", _blocked))
    stack.enter_context(patch.object(sqlalchemy.engine.create, "create_engine", _blocked))
    original_connect = sqlite3.connect

    def temporary_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_authorizer(
            lambda action, *unused: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK
        )
        return connection

    stack.enter_context(patch.object(sqlite3, "connect", temporary_connect))
    stack.enter_context(patch.object(sqlite3.dbapi2, "connect", temporary_connect))

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from luigi_web import application, auth
    from luigi_web.core.module_registry import BUILTIN_MODULE_IDS, build_registry, mount_modules
    from luigi_web.modules.assistant import tools
    from luigi_web.modules.finance import overview, plans, repository as finance

    class ActualHostFinanceTests(unittest.TestCase):
        def setUp(self):
            self.enterContext(patch.dict(os.environ, {
                "LUIGI_WEB_FINANCE_DB": str(directory / (self._testMethodName + ".sqlite3")),
            }))
            self.enterContext(patch.object(finance.clock, "local_today", return_value=date(2028, 2, 15)))
            self.client = TestClient(application.app, base_url=HOST, follow_redirects=False, raise_server_exceptions=False)
            self.addCleanup(self.client.close)
            cookies = SimpleCookie()
            cookies.load(auth.finance_unlock_response("synthetic-unlock").headers["set-cookie"])
            self.finance_session = cookies[auth.FINANCE_COOKIE_NAME].value
            self.client.cookies.set(auth.COOKIE_NAME, "synthetic-main")
            self.client.cookies.set(auth.FINANCE_COOKIE_NAME, self.finance_session)
            self.client.cookies.set(auth.CSRF_COOKIE_NAME, "synthetic-csrf")
            self.headers = {"origin": HOST, "x-csrf-token": "synthetic-csrf"}

        def private(self, response, status):
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.headers.get("cache-control"), "no-store")
            self.assertEqual(response.headers.get("referrer-policy"), "no-referrer")
            self.assertEqual(response.headers.get("pragma"), "no-cache")
            self.assertEqual(response.headers.get("expires"), "0")
            self.assertEqual(response.request.headers["host"], "testserver")
            self.assertFalse(response.url.query)

        def test_host_csrf_rejects_before_dependencies(self):
            with patch.dict(application.app.dependency_overrides, {
                auth.require_auth: _blocked, auth.require_finance_auth: _blocked,
            }), patch.object(finance, "connect", side_effect=_blocked) as database:
                for headers in ({}, {"origin": HOST}, {**self.headers, "x-csrf-token": "incorrect"}):
                    response = self.client.post("/finance/planning/save", json={}, headers=headers)
                    self.private(response, 403)
                    self.assertEqual(response.json(), {"detail": "CSRF validation failed"})
                self.assertFalse(database.called)
            self.assertFalse(application.app.dependency_overrides)
            self.assertEqual(application.app.state.module_cleanup, [])
            self.assertEqual(set(application.app.state.module_status.values()), {"enabled"})

        def post(self, path, payload, status=200):
            response = self.client.post("/finance/planning/" + path, json=payload, headers=self.headers)
            self.private(response, status)
            return response

        def seed_ledger(self):
            finance.init_db()
            timestamp = "2028-02-01T00:00:00+00:00"

            def fixture_id(label):
                return str(uuid5(NAMESPACE_URL, "synthetic-finance:" + label))

            with finance.connect(write=True) as connection:
                connection.executemany(
                    "INSERT INTO finance_accounts VALUES (?, ?, ?, 'USD', ?, ?, ?, ?)",
                    [(fixture_id(label), alias, kind, balance, active, timestamp, timestamp)
                     for label, alias, kind, balance, active in (
                         ("cash", "Everyday account", "checking", 10000, 1),
                         ("brokerage", "Brokerage", "investment", 4000, 1),
                         ("inactive", "Example archived account", "checking", 90000, 0))],
                )
                connection.execute("""INSERT INTO finance_transactions VALUES
                    (?, ?, '2028-02-10', -300, 'Example category', 'Example merchant', NULL, ?, ?)""",
                    (fixture_id("transaction"), fixture_id("cash"), timestamp, timestamp))
                connection.execute("""INSERT INTO finance_holdings VALUES
                    (?, ?, 'SYNTH', 'Example holding', 1000000, 0, 20000, '2028-02-14', ?, ?)""",
                    (fixture_id("holding"), fixture_id("brokerage"), timestamp, timestamp))
                connection.executemany("""INSERT INTO finance_recurring_items
                    VALUES (?, ?, 'Example schedule', 'Example category', ?, 'monthly', '2028-03-01', 1, ?, ?)""",
                    [(fixture_id(label), fixture_id(account), amount, timestamp, timestamp)
                     for label, account, amount in
                     (("income", "cash", 1000), ("bill", "cash", -200), ("inactive-bill", "inactive", -9999))])
                connection.execute("""INSERT INTO finance_net_worth_snapshots
                    VALUES (?, '2028-02-01', 10000, ?)""", (fixture_id("snapshot"), timestamp))

        def recorded_rows(self):
            with finance.connect() as connection:
                return {table: [tuple(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY id")]
                        for table in ("finance_accounts", "finance_transactions", "finance_holdings", "finance_budgets",
                                      "finance_recurring_items", "finance_net_worth_snapshots", "finance_saved_reports")}

        def test_actual_composition_templates_assets_and_local_icons(self):
            self.seed_ledger()
            self.assertEqual(set(application.app.state.modules._enabled_ids), set(BUILTIN_MODULE_IDS))
            self.assertEqual(set(BUILTINS.split(",")), set(BUILTIN_MODULE_IDS))
            declarations = [(route.path, method) for route in application.app.routes
                            for method in getattr(route, "methods", ())]
            self.assertEqual(len(declarations), len(set(declarations)))
            assets = set()
            for path in ("/finance", "/finance/overview", "/finance/records", "/finance/planning"):
                with self.subTest(case="page", path=path):
                    response = self.client.get(path)
                    self.private(response, 200)
                    parsed = PageAssets()
                    parsed.feed(response.text)
                    assets.update(parsed.assets)
                    if path != "/finance/records":
                        self.assertIn('hx-history="false"', response.text)
                        self.assertTrue(all(not urlsplit(link).query for link in parsed.finance_links))
                        self.assertTrue(all(form.get("method", "get").lower() == "post" for form in parsed.finance_forms))
                    if path in {"/finance", "/finance/overview"}:
                        self.assertIn('id="finance-overview"', response.text)
                        self.assertTrue({"/finance/planning", "/finance/records"} <= parsed.finance_links)
                        self.assertIn("recorded total", " ".join(parsed.text).lower())
                        self.assertRegex(" ".join(parsed.text).lower(), r"currency.{0,35}(?:not stored|unrecorded|not recorded)")
            fetched = set()
            icons = set()
            while assets - fetched:
                source = sorted(assets - fetched)[0]
                fetched.add(source)
                parsed_url = urlsplit(source)
                self.assertFalse(parsed_url.netloc or parsed_url.scheme, "Page referenced a non-local asset")
                self.assertTrue(parsed_url.path.startswith(("/static/", "/module-assets/")))
                with self.subTest(case="asset", path=parsed_url.path):
                    response = self.client.get(source)
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.content)
                    if parsed_url.path.endswith(".css"):
                        for resource in re.findall(r"url\(['\"]?([^'\")\s]+)", response.text):
                            if not resource.startswith(("data:", "#")):
                                assets.add(urljoin(source, resource))
                    if parsed_url.path.startswith("/static/icons/lucide/"):
                        self.assertTrue(ElementTree.fromstring(response.content).tag.endswith("svg"))
                        icons.add(parsed_url.path)
            self.assertGreater(len(icons), 10)
            paths = {urlsplit(source).path for source in fetched}
            self.assertTrue({"/module-assets/finance/overview.css", "/module-assets/finance/overview.js",
                             "/module-assets/finance/planning.css", "/module-assets/finance/planning.js",
                             "/static/js/vendor/chart.umd.min.js", "/module-assets/assistant/panel.js"} <= paths)
            for path in ("/finance", "/finance/planning/state", "/finance/planning/calculate"):
                with self.subTest(case="query-rejected", path=path):
                    if path.endswith("calculate"):
                        response = self.client.post(path + "?unexpected=1", json={}, headers=self.headers)
                    else:
                        response = self.client.get(path + "?unexpected=1")
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.headers.get("cache-control"), "no-store")
                    self.assertEqual(response.headers.get("referrer-policy"), "no-referrer")

        def test_disabled_registry_and_no_finance_search_or_assistant_tools(self):
            selected = ",".join(name for name in BUILTIN_MODULE_IDS if name != "finance")
            registry = build_registry(selected)
            disabled = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
            mount_modules(disabled, registry)
            self.assertFalse(registry.is_enabled("finance"))
            self.assertFalse(any(getattr(route, "path", "").startswith(("/finance", "/module-assets/finance"))
                                 for route in disabled.routes))
            client = TestClient(disabled, base_url=HOST, follow_redirects=False)
            self.addCleanup(client.close)
            for path in ("/finance", "/finance/records", "/finance/planning", "/finance/planning/state",
                         "/finance/backup", "/module-assets/finance/overview.js"):
                self.assertEqual(client.get(path).status_code, 404)
            navigation = application.app.state.modules.navigation()
            finance_navigation = [item for unused, items in navigation for item in items if item.key == "finance"]
            self.assertEqual(len(finance_navigation), 1)
            self.assertFalse(finance_navigation[0].searchable)
            with patch.object(finance, "connect", side_effect=_blocked) as database:
                response = self.client.get("/command-palette")
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('href="/finance', response.text)
                tool_registry = tools.build_registry()
                self.assertTrue(tool_registry)
                names = [getattr(tool, "name", str(tool)) for tool in tool_registry]
                self.assertFalse(any("finance" in name.lower() for name in names))
                self.assertFalse(database.called)
            self.assertNotIn("finance", inspect.getsource(application.command_palette_results).lower())
            self.assertNotIn("finance", inspect.getsource(tools.build_registry).lower())
            self.assertEqual(disabled.state.module_cleanup, [])

        def test_forecasts_saves_and_comparisons_preserve_recorded_ledger(self):
            self.seed_ledger()
            before = self.recorded_rows()
            config = {"start_month": "2028-03", "income_mode": "fixed", "net_income_minor": 500,
                      "opening_cash_minor": 1000, "monthly_contribution_minor": 100, "variable_expense_minor": 50}
            result = self.post("calculate", {"kind": "cashflow", "config": config}).json()["result"]
            self.assertEqual(len(result["rows"]), 12)
            self.assertEqual(result["rows"][0]["bills_minor"], 200)
            self.assertEqual(result["rows"][0]["ending_cash_minor"], 1150)
            self.assertEqual(result["rows"][0]["net_worth_minor"], 1250)
            wealth = self.post("calculate", {"kind": "wealth", "config": config, "years": 2}).json()["result"]
            self.assertEqual(wealth["rows"][-1]["net_worth_minor"], 7000)
            housing_config = {"home_price_minor": 120000, "term_years": 1}
            housing = self.post("calculate", {"kind": "housing", "config": housing_config}).json()["result"]
            self.assertEqual(housing["monthly_payment_minor"], 10000)
            identifiers = [self.post("save", {"kind": "cashflow", "name": "Example scenario", "config": changed})
                           .json()["record"]["id"] for changed in (config, {**config, "net_income_minor": 700})]
            cashflow = self.post("compare", {"kind": "cashflow", "plan_ids": identifiers}).json()["scenarios"]
            self.assertEqual([row["result"]["rows"][0]["ending_cash_minor"] for row in cashflow], [1150, 1350])
            comparison = self.post("compare", {"kind": "wealth", "plan_ids": identifiers, "years": 2}).json()["scenarios"]
            self.assertEqual([row["result"]["rows"][-1]["net_worth_minor"] for row in comparison], [7000, 11800])
            housing_ids = [self.post("save", {"kind": "housing", "name": "Example housing", "config": changed})
                           .json()["record"]["id"] for changed in
                           (housing_config, {**housing_config, "home_price_minor": 240000})]
            comparison = self.post("compare", {"kind": "housing", "plan_ids": housing_ids}).json()["scenarios"]
            self.assertEqual([row["result"]["monthly_payment_minor"] for row in comparison], [10000, 20000])
            self.assertEqual(self.recorded_rows(), before)

        def test_http_backup_restore_new_columns_validation_and_legacy(self):
            self.seed_ledger()
            saved = self.post("save", {"kind": "cashflow", "name": "Example saved plan", "config": {}}).json()["record"]
            updated = self.post("save", {"kind": "cashflow", "name": "Example revised plan", "config": {},
                                         "id": saved["id"], "expected_version": 1}).json()["record"]
            stream = self.post("income/save", {"name": "Example net pay", "amount_minor": 1200,
                                               "cadence": "biweekly", "start_date": "2028-03-01",
                                               "end_date": "2029-03-01", "annual_growth_bps": 200,
                                               "active": False, "currency": "USD"}).json()["record"]
            backup = self.client.get("/finance/backup")
            self.private(backup, 200)
            self.assertIn("attachment;", backup.headers["content-disposition"])
            exported = self.client.get("/finance/export.csv")
            self.private(exported, 200)
            self.assertIn("attachment;", exported.headers["content-disposition"])
            self.assertEqual(len(exported.text.strip().splitlines()), 2)
            payload = backup.json()
            self.assertEqual(payload["format"], "luigi-finance-backup-v1")
            for table, columns in plans.BACKUP_COLUMNS.items():
                self.assertEqual(set(payload["tables"][table][0]), set(columns))

            def restore(content, status=204):
                response = self.client.post("/finance/restore", headers=self.headers,
                                            files={"file": ("synthetic-backup.json", json.dumps(content), "application/json")})
                self.private(response, status)
                return response

            with patch.dict(os.environ, {"LUIGI_WEB_FINANCE_DB": str(directory / "restored.sqlite3")}):
                restore(payload)
                self.assertEqual(plans.get_plan(saved["id"]), updated)
                self.assertEqual(plans.list_income(), [stream])
                before = self.recorded_rows()
                invalid = deepcopy(payload)
                invalid["tables"]["finance_accounts"][0]["opening_balance_minor"] = 123456
                invalid["tables"]["finance_plans"][0]["payload_json"] = '{"opening_cash_minor":1.5}'
                restore(invalid, 422)
                self.assertEqual(self.recorded_rows(), before)
                self.assertEqual(plans.get_plan(saved["id"]), updated)
                self.assertEqual(plans.list_income(), [stream])
            legacy = deepcopy(payload)
            for table in plans.BACKUP_TABLES:
                del legacy["tables"][table]
            with patch.dict(os.environ, {"LUIGI_WEB_FINANCE_DB": str(directory / "legacy-restored.sqlite3")}):
                restore(legacy)
                self.assertEqual(plans.list_plans(), [])
                self.assertEqual(plans.list_income(), [])
                self.assertEqual(len(finance.list_accounts(active_only=False)), 3)
                self.assertEqual(len(finance.list_transactions()), 1)

        def test_isolation_guards_and_no_host_startup(self):
            self.assertEqual(os.environ["PYTHON_DOTENV_DISABLED"], "1")
            self.assertFalse(Path(os.environ["LUIGI_WEB_MODULES_FILE"]).exists())
            self.assertTrue(finance.db_path().resolve().is_relative_to(directory))
            self.assertEqual(application.app.state.module_cleanup, [])
            self.assertEqual(set(application.app.state.module_status.values()), {"enabled"})
            self.assertIsNone(application.db._engine)
            self.assertFalse(application.app.dependency_overrides)
            if os.name == "nt":
                self.assertTrue(all(os.environ.get(key) for key in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR")))
            for key in ("TEMP", "TMP", "SQLITE_TMPDIR", "APPDATA", "LOCALAPPDATA", "HOME", "USERPROFILE"):
                self.assertEqual(Path(os.environ[key]), directory)
            self.assertFalse(dotenv.load_dotenv())
            self.assertEqual(dotenv.dotenv_values(), {})
            outside = directory.parent / "forbidden-synthetic-finance.sqlite3"
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
            with self.assertRaises(RuntimeError):
                socket.getaddrinfo("example.invalid", 443)
            with socket.socket() as connection, self.assertRaises(RuntimeError):
                connection.connect(("127.0.0.1", 9))
            with self.assertRaises(RuntimeError):
                sqlalchemy.create_engine("sqlite:///:memory:")
            pair = socket.socketpair()
            for connection in pair:
                connection.close()

        def test_both_sessions_bearer_and_same_origin_are_required(self):
            paths = ("/finance", "/finance/records", "/finance/planning", "/finance/planning/state",
                     "/finance/backup", "/finance/export.csv")
            with patch.object(finance, "connect", side_effect=_blocked) as database:
                for main, unlock, status in ((False, False, 401), (False, True, 401), (True, False, 403)):
                    self.client.cookies.clear()
                    if main:
                        self.client.cookies.set(auth.COOKIE_NAME, "synthetic-main")
                    if unlock:
                        self.client.cookies.set(auth.FINANCE_COOKIE_NAME, self.finance_session)
                    for path in paths:
                        with self.subTest(case="auth", path=path, main=main, unlock=unlock):
                            self.private(self.client.get(path), status)
                self.client.cookies.clear()
                bearer = {"authorization": "Bearer synthetic-main"}
                self.private(self.client.post("/finance/planning/calculate", json={"kind": "cashflow", "config": {}},
                                              headers=bearer), 403)
                self.assertFalse(database.called)
            self.client.cookies.set(auth.FINANCE_COOKIE_NAME, self.finance_session)
            with self.subTest(case="unlocked-bearer"):
                self.private(self.client.post("/finance/planning/calculate", json={"kind": "cashflow", "config": {}},
                                              headers=bearer), 200)
            self.client.cookies.clear()
            self.client.cookies.set(auth.COOKIE_NAME, "synthetic-main")
            self.client.cookies.set(auth.FINANCE_COOKIE_NAME, self.finance_session)
            self.client.cookies.set(auth.CSRF_COOKIE_NAME, "synthetic-csrf")
            with patch.object(finance, "connect", side_effect=_blocked) as database:
                for headers in (
                    {"x-csrf-token": "synthetic-csrf"},
                    {**self.headers, "origin": "https://example.invalid"},
                    {**self.headers, "sec-fetch-site": "cross-site"},
                    {"referer": "https://example.invalid/", "x-csrf-token": "synthetic-csrf"},
                    {**self.headers, "origin": "https://example.invalid", "x-forwarded-host": "example.invalid"},
                ):
                    self.private(self.client.post("/finance/planning/save", json={}, headers=headers), 403)
                self.assertFalse(database.called)
            with self.subTest(case="unlocked-browser"):
                self.post("save", {"kind": "cashflow", "name": "Example scenario", "config": {}})

        def test_private_errors_do_not_echo_database_paths_or_input(self):
            marker = "SYNTHETIC_PRIVATE_SENTINEL"
            failure = RuntimeError(str(directory / "synthetic-private.sqlite3") + " " + marker)
            cases = (
                ("overview-database", overview, "overview_state", "/finance", 500),
                ("legacy-database", finance, "dashboard", "/finance/records", 503),
                ("planning-database", plans, "list_plans", "/finance/planning/state", 503),
            )
            for label, module, name, path, status in cases:
                with self.subTest(case=label), patch.object(module, name, side_effect=failure):
                    response = self.client.get(path)
                    self.private(response, status)
                    self.assertEqual(set(response.json()), {"detail"})
                    self.assertIsInstance(response.json()["detail"], str)
                    self.assertNotIn(marker, response.text)
                    self.assertNotIn(str(directory), response.text)
                    self.assertNotIn("synthetic-private.sqlite3", response.text)
            with self.subTest(case="planning-extra-input"):
                response = self.post("save", {"kind": "cashflow", "name": "Example scenario", "config": {},
                                               "finance_extra": marker}, 422)
                self.assertEqual(set(response.json()), {"detail"})
                self.assertIsInstance(response.json()["detail"], str)
                self.assertNotIn(marker, response.text)
                self.assertNotIn("finance_extra", response.text)
            with self.subTest(case="overview-extra-input"):
                response = self.client.post("/finance/overview/filter", data={"finance_extra": marker}, headers=self.headers)
                self.private(response, 422)
                self.assertEqual(response.json(), {"detail": "Invalid Finance request. Check the submitted fields."})
                self.assertNotIn(marker, response.text)
                self.assertNotIn("finance_extra", response.text)
            with self.subTest(case="legacy-validation-input"):
                response = self.client.post("/finance/restore", data={"file": marker, "finance_extra": marker}, headers=self.headers)
                self.private(response, 422)
                self.assertNotIn(marker, response.text)
                self.assertNotIn("finance_extra", response.text)
                self.assertIsInstance(response.json()["detail"], str)

    return unittest.defaultTestLoader.loadTestsFromTestCase(ActualHostFinanceTests)


def _run_isolated():
    logging.disable(logging.CRITICAL)
    summary = {"tests": 0, "failures": 0, "errors": 1, "failed_tests": []}
    try:
        with ExitStack() as stack, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            suite = _isolated_suite(Path(os.environ["LUIGI_WEB_DATA_DIR"]).resolve(), stack)
            result = unittest.TestResult()
            suite.run(result)
            summary = {
                "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                "failed_tests": [test.id().split("ActualHostFinanceTests.")[-1]
                                 for test, unused in result.failures + result.errors],
                "failure_lines": [
                    [int(line) for line in re.findall(r'test_finance_upgrade_integration\.py", line (\d+)', trace)]
                    for unused, trace in result.failures + result.errors
                ],
            }
    except Exception as error:
        summary["setup_error"] = type(error).__name__
    print(json.dumps(summary))
    return int(bool(summary["failures"] or summary["errors"]))


if __name__ == "__main__":
    if "--isolated" in sys.argv:
        raise SystemExit(_run_isolated())
    unittest.main()