"""Offline planning integration with disposable synthetic records only."""
from __future__ import annotations

import os
from datetime import date
from http.cookies import SimpleCookie
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from luigi_web import auth
from luigi_web.modules.finance import planning_routes as routes
from luigi_web.modules.finance import plans
from luigi_web.modules.finance import repository as finance


class PlanningRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR"}}
        environment.update({key: temporary.name for key in
                            ("TEMP", "TMP", "SQLITE_TMPDIR", "APPDATA", "LOCALAPPDATA")})
        environment.update(LUIGI_WEB_FINANCE_DB=str(Path(temporary.name) / "synthetic.db"),
                           LUIGI_WEB_FINANCE_BASE_CURRENCY="USD",
                           LUIGI_WEB_UI_TOKEN="synthetic-ui", LUIGI_WEB_FINANCE_TOKEN="synthetic-finance")
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        self.enterContext(patch.object(finance.clock, "local_today", return_value=date(2028, 2, 15)))
        application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        application.include_router(routes.router)
        self.client = self.enterContext(TestClient(application, follow_redirects=False))
        cookies = SimpleCookie()
        cookies.load(auth.finance_unlock_response("synthetic-finance").headers["set-cookie"])
        self.finance_session = cookies[auth.FINANCE_COOKIE_NAME].value
        self.client.cookies.set(auth.COOKIE_NAME, "synthetic-ui")
        self.client.cookies.set(auth.FINANCE_COOKIE_NAME, self.finance_session)
        self.client.cookies.set(auth.CSRF_COOKIE_NAME, "synthetic-csrf")
        self.headers = {"origin": "http://testserver", "x-csrf-token": "synthetic-csrf"}
        self.payload = {"kind": "cashflow", "name": "Example scenario", "config": {}}

    def post(self, path, payload):
        return self.client.post("/finance/planning/" + path, json=payload, headers=self.headers)

    def assert_private(self, response, status):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(response.headers.get("referrer-policy"), "no-referrer")
        self.assertNotIn("?", str(response.url))

    def test_save_update_stale_delete(self):
        response = self.post("save", self.payload)
        self.assert_private(response, 200)
        saved = response.json()["record"]
        self.assertEqual(saved["version"], 1)
        update = {**self.payload, "id": saved["id"], "expected_version": 1}
        self.assert_private(self.post("save", update), 200)
        self.assert_private(self.post("save", update), 409)
        self.assert_private(self.post("delete", {"id": saved["id"], "expected_version": 1}), 409)
        self.assert_private(self.post("delete", {"id": saved["id"], "expected_version": 2}), 200)
        self.assertEqual(plans.list_plans(), [])

    def test_both_auth_and_same_origin_csrf(self):
        self.client.cookies.clear()
        self.assert_private(self.post("save", self.payload), 401)
        self.client.cookies.set(auth.COOKIE_NAME, "synthetic-ui")
        self.assert_private(self.post("save", self.payload), 403)
        self.client.cookies.set(auth.FINANCE_COOKIE_NAME, self.finance_session)
        self.assert_private(self.post("save", self.payload), 403)
        self.client.cookies.set(auth.CSRF_COOKIE_NAME, "synthetic-csrf")
        for headers in ({}, {"x-csrf-token": "synthetic-csrf"},
                        {**self.headers, "origin": "https://example.invalid"},
                        {**self.headers, "sec-fetch-site": "cross-site"}):
            self.assert_private(self.client.post("/finance/planning/save", json=self.payload, headers=headers), 403)
        self.assert_private(self.post("save", self.payload), 200)

    def test_bearer_still_requires_finance_session(self):
        self.client.cookies.clear()
        headers = {"authorization": "Bearer synthetic-ui"}
        self.assert_private(self.client.post("/finance/planning/save", json=self.payload, headers=headers), 403)
        self.client.cookies.set(auth.FINANCE_COOKIE_NAME, self.finance_session)
        self.assert_private(self.client.post("/finance/planning/save", json=self.payload, headers=headers), 200)

    def test_json_limits_unknown_fields_and_generic_errors(self):
        for body in ('{"kind":"cashflow","kind":"housing"}', '{"config": NaN}',
                     '{"config": 1.2}', '[1]', '{"config":' + '[' * 10 + '0' + ']' * 10 + '}'):
            self.assert_private(self.client.post("/finance/planning/save", content=body,
                                headers={**self.headers, "content-type": "application/json"}), 422)
        self.assert_private(self.client.post("/finance/planning/save", content=" " * 32769,
                            headers={**self.headers, "content-type": "application/json"}), 413)
        self.assert_private(self.client.post("/finance/planning/save", data={"kind": "cashflow"},
                            headers=self.headers), 415)
        response = self.post("save", {**self.payload, "unsupported": "SYNTHETIC_PRIVATE_SENTINEL"})
        self.assert_private(response, 422)
        self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", response.text)
        self.assert_private(self.post("save", {**self.payload, "config": {"net_income_minor": True}}), 422)
        self.assert_private(self.client.get("/finance/planning/save"), 405)
        response = self.client.post("/finance/planning/save?unsupported=example", json=self.payload, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("example", response.text)

    def test_database_failure_does_not_echo(self):
        with patch.object(plans, "save_plan", side_effect=RuntimeError("SYNTHETIC_PRIVATE_SENTINEL")):
            response = self.post("save", self.payload)
        self.assert_private(response, 503)
        self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", response.text)

    def seed_accounts(self):
        finance.init_db()
        with finance.connect(write=True) as connection:
            connection.executemany("INSERT INTO finance_accounts VALUES (?, 'Example account', ?, ?, ?, ?, '', '')", [
                ("cash", "checking", "USD", 10000, 1), ("savings", "savings", "USD", 5000, 1),
                ("invest", "investment", "USD", 7000, 1), ("credit", "credit", "USD", -1000, 1),
                ("inactive", "checking", "USD", 8000, 0), ("foreign", "cash", "EUR", 6000, 1),
            ])
            connection.executemany("INSERT INTO finance_holdings VALUES (?, ?, 'SYNTH', 'Example holding', 1000000, 0, ?, '2028-02-01', '', '')", [
                ("active-holding", "invest", 20000), ("inactive-holding", "inactive", 30000),
                ("foreign-holding", "foreign", 40000),
            ])
            connection.executemany("INSERT INTO finance_recurring_items VALUES (?, 'cash', 'Example schedule', 'Example category', ?, 'monthly', '2028-01-15', 1, '', '')", [
                ("income", 1000), ("bill", -200),
            ])

    def recorded_rows(self):
        with finance.connect() as connection:
            return {table: [tuple(row) for row in connection.execute("SELECT * FROM " + table)]
                    for table in ("finance_accounts", "finance_holdings", "finance_transactions", "finance_recurring_items")}

    def test_state_cash_and_holdings_are_separate_and_no_failed_zero_baseline(self):
        self.seed_accounts()
        response = self.client.get("/finance/planning/state")
        self.assert_private(response, 200)
        state = response.json()
        self.assertEqual(state["defaults"]["opening_cash_minor"], 15000)
        self.assertEqual(state["defaults"]["opening_investments_minor"], 20000)
        self.assertEqual(state["defaults"]["start_month"], "2028-03")
        self.assertEqual(state["defaults"]["annual_return_bps"], 0)
        self.assertEqual(state["baseline_as_of"], "2028-02-15")
        self.assertNotIn("accounts", state)
        with patch.object(finance, "list_accounts", side_effect=RuntimeError("SYNTHETIC_PRIVATE_SENTINEL")):
            response = self.client.get("/finance/planning/state")
        self.assert_private(response, 503)
        self.assertNotIn("defaults", response.text)
        self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", response.text)

    def test_calculations_and_saves_never_mutate_recorded_finances(self):
        self.seed_accounts()
        before = self.recorded_rows()
        config = {"start_month": "2028-03", "income_mode": "fixed", "net_income_minor": 500,
                  "opening_cash_minor": 1000, "monthly_contribution_minor": 100, "variable_expense_minor": 50}
        result = self.post("calculate", {"kind": "cashflow", "config": config}).json()["result"]
        self.assertEqual(len(result["rows"]), 12)
        self.assertEqual(result["rows"][0]["income_minor"], 500)
        self.assertEqual(result["rows"][0]["bills_minor"], 200)
        self.assertEqual(result["rows"][0]["ending_cash_minor"], 1150)
        self.assertEqual(result["rows"][0]["net_worth_minor"], 1250)
        wealth = self.post("calculate", {"kind": "wealth", "config": config, "years": 2})
        self.assert_private(wealth, 200)
        self.assertEqual(wealth.json()["result"]["rows"][-1]["net_worth_minor"], 7000)
        housing = self.post("calculate", {"kind": "housing", "config": {"home_price_minor": 120000, "term_years": 1}})
        self.assert_private(housing, 200)
        self.assertEqual(housing.json()["result"]["monthly_payment_minor"], 10000)
        self.assert_private(self.post("save", {**self.payload, "config": config}), 200)
        self.assertEqual(self.recorded_rows(), before)

    def test_forecasts_exclude_inactive_account_schedules(self):
        self.seed_accounts()
        with finance.connect(write=True) as connection:
            connection.execute("""INSERT INTO finance_recurring_items
                VALUES ('inactive-bill', 'inactive', 'Example inactive bill', 'Example category',
                        -9999, 'monthly', '2028-03-01', 1, '', '')""")
        response = self.post("calculate", {"kind": "cashflow", "config": {"start_month": "2028-03"}})
        self.assert_private(response, 200)
        self.assertEqual(response.json()["result"]["rows"][0]["bills_minor"], 200)

    def test_income_versions_and_replacement_not_double_counted(self):
        self.seed_accounts()
        stream = {"name": "Example net pay", "amount_minor": 300, "cadence": "biweekly", "start_date": "2028-03-01"}
        saved = self.post("income/save", stream).json()["record"]
        response = self.post("calculate", {"kind": "cashflow", "config": {"start_month": "2028-03", "income_mode": "streams"}})
        self.assertEqual(response.json()["result"]["rows"][0]["income_minor"], 900)
        self.assertEqual(response.json()["result"]["rows"][0]["bills_minor"], 200)
        update = {**stream, "id": saved["id"], "expected_version": 1, "active": False}
        self.assert_private(self.post("income/save", update), 200)
        self.assert_private(self.post("income/save", update), 409)
        self.assert_private(self.post("income/delete", {"id": saved["id"], "expected_version": 1}), 409)
        self.assert_private(self.post("income/delete", {"id": saved["id"], "expected_version": 2}), 200)
        self.assertEqual(plans.list_income(), [])

    def test_comparisons_enforce_same_kind_dates_and_horizon(self):
        first = self.post("save", self.payload).json()["record"]
        second = self.post("save", {**self.payload, "config": {"opening_cash_minor": 500}}).json()["record"]
        identifiers = [first["id"], second["id"]]
        response = self.post("compare", {"kind": "cashflow", "plan_ids": identifiers})
        self.assert_private(response, 200)
        self.assertEqual(len(response.json()["scenarios"]), 2)
        self.assert_private(self.post("compare", {"kind": "housing", "plan_ids": identifiers}), 422)
        self.assert_private(self.post("compare", {"kind": "cashflow", "plan_ids": identifiers * 2}), 422)
        self.assert_private(self.post("compare", {"kind": "cashflow", "plan_ids": [first["id"], first["id"]]}), 422)
        for config in ({"start_month": "2028-04"}, {"months": 6}):
            self.assert_private(self.post("compare", {"kind": "cashflow", "configs": [{"start_month": "2028-03"}, config]}), 422)
        wealth = self.post("compare", {"kind": "wealth", "configs": [{"start_month": "2028-03"}, {"start_month": "2028-04"}], "years": 2})
        self.assert_private(wealth, 200)
        self.assertEqual([row["result"]["years"] for row in wealth.json()["scenarios"]], [2, 2])
        self.assertEqual([row["result"]["start_month"] for row in wealth.json()["scenarios"]], ["2028-03", "2028-04"])
        self.assert_private(self.post("calculate", {"kind": "wealth", "config": {}, "years": 51}), 422)
        self.assert_private(self.post("calculate", {"kind": "housing", "config": {}}), 422)
        self.assertEqual(len(plans.list_plans()), 2)

    def test_private_page_real_template_escaped_seed_and_post_only_forms(self):
        self.post("save", {**self.payload, "name": "</script><script>synthetic</script>"})
        response = self.client.get("/finance/planning")
        self.assert_private(response, 200)
        self.assertIn('hx-history="false"', response.text)
        self.assertIn('href="/finance"', response.text)
        self.assertIn('href="/finance/records"', response.text)
        self.assertIn('/static/js/vendor/chart.umd.min.js', response.text)
        self.assertIn('\\u003c/script\\u003e', response.text)
        self.assertNotIn('<script>synthetic</script>', response.text)
        self.assertNotIn('method="get"', response.text)
        self.assertNotIn('investment_return_bps', response.text)
        self.assertNotIn('name="home_price_minor" type="text" inputmode="decimal" data-scale="2" value="0', response.text)
        self.client.cookies.clear()
        response = self.client.get("/finance/planning", headers={"accept": "text/html"})
        self.assert_private(response, 303)
        self.assertEqual(response.headers["location"], "/login")


if __name__ == "__main__":
    unittest.main()