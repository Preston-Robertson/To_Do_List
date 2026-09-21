"""Offline overview regression checks using disposable, synthetic records only."""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date
from importlib.util import find_spec
from pathlib import Path
from importlib.resources import files as module_files
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from luigi_web.modules.finance import overview
from luigi_web.modules.finance import repository as finance


class OverviewFixture(unittest.TestCase):
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
        self.enterContext(patch.object(overview.clock, "local_today", return_value=date(2028, 2, 15)))
        finance.init_db()
        self.account("everyday", "checking", 10000)

    def account(self, identifier, kind, opening=0, active=1, currency="USD"):
        with finance.connect(write=True) as connection:
            connection.execute("INSERT INTO finance_accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (identifier, "Everyday account" if identifier == "everyday" else "Brokerage",
                                kind, currency, opening, active, "2028-01-01", "2028-01-01"))

    def transaction(self, identifier, amount, day="2028-02-10", account="everyday", category="Example category", memo=None):
        with finance.connect(write=True) as connection:
            connection.execute("INSERT INTO finance_transactions VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                               (identifier, account, day, amount, category, memo, "2028-02-10", "2028-02-10"))

    def holding(self, account, amount, as_of="2028-02-14", symbol="SYNTH"):
        with finance.connect(write=True) as connection:
            connection.execute("INSERT INTO finance_holdings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (account + symbol, account, symbol, "Example holding", 1000000, 0,
                                amount, as_of, "2028-01-01", "2028-01-01"))


class OverviewDataTests(OverviewFixture):
    def test_bulk_reads_one_trend_aggregate_and_no_record_writes(self):
        statements = []
        original_connect = finance.connect

        class ReadProbe:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, statement, parameters=()):
                statements.append(statement)
                return self.connection.execute(statement, parameters)

        @contextmanager
        def instrumented_connect(*, write=False):
            with original_connect(write=write) as connection:
                yield connection if write else ReadProbe(connection)
                if not write:
                    self.assertEqual(connection.total_changes, 0)

        with patch.object(finance, "connect", instrumented_connect):
            overview.overview_state()
        reads = [statement for statement in statements if statement.strip().upper().startswith(("SELECT", "WITH"))]
        self.assertEqual(len(reads), 10)
        self.assertEqual(sum("WITH months(" in statement for statement in reads), 1)
        self.assertFalse(any("substr(" in statement.lower() for statement in reads))
        self.assertTrue(all("transaction_date >=" in statement for statement in reads
                            if "finance_transactions" in statement and "opening_balance_minor" not in statement))

    def test_inactive_recorded_flows_and_sqlite_category_budget_semantics(self):
        self.account("archived", "cash", 200, active=0)
        self.transaction("archived-record", -123, account="archived", category="EXAMPLE category")
        self.transaction("unicode-upper", -50, category="\u00c4lias")
        self.transaction("unicode-lower", -70, category="\u00e4lias")
        with finance.connect(write=True) as connection:
            connection.executemany("INSERT INTO finance_budgets VALUES (?, '2028-02', ?, 100, '', '')",
                                   [("ascii", "Example category"), ("upper", "\u00c4lias"), ("lower", "\u00e4lias")])
        state = overview.overview_state(account_id="archived")
        self.assertEqual(state["month_totals"]["outflow_minor"], 243)
        self.assertEqual(state["filtered"]["outflow_minor"], 123)
        self.assertEqual({row["category"]: row["spent_minor"] for row in state["budgets"]},
                         {"Example category": 123, "\u00c4lias": 50, "\u00e4lias": 70})

    def test_stale_boundary_and_allocation_totals(self):
        self.account("invest", "investment")
        self.account("invest-second", "investment")
        self.holding("invest", 7000, "2028-02-08")
        self.holding("invest-second", 3000, "2028-02-07")
        state = overview.overview_state()
        self.assertEqual(state["stale_holdings_count"], 1)
        self.assertEqual(state["allocation_symbols"], [{"label": "SYNTH", "value_minor": 10000}])
        self.assertEqual(sum(row["value_minor"] for row in state["allocation_accounts"]), 10000)

    def test_default_month_empty_baseline_and_twelve_months(self):
        state = overview.overview_state()
        self.assertEqual(state["month"], "2028-02")
        self.assertEqual(len(state["trend"]), 12)
        self.assertEqual(state["trend"][0]["month"], "2027-03")
        self.assertIsNone(state["comparison"]["inflow_minor"]["change_bps"])
        self.assertEqual(state["snapshots"], [])

    def test_active_balances_credit_and_holdings_not_double_counted(self):
        self.account("invest", "investment", 3000)
        self.account("credit-negative", "credit", -2000)
        self.account("credit-positive", "credit", 500)
        self.account("inactive", "investment", 9000, active=0)
        self.account("savings", "savings", 4000)
        self.holding("invest", 7000, "2028-02-01")
        self.holding("inactive", 8000)
        self.transaction("future-recorded", 100, "2028-05-01")
        state = overview.overview_state()
        self.assertEqual(state["net_worth_minor"], 22600)
        self.assertEqual(state["liquid_cash_minor"], 14100)
        self.assertEqual(state["liabilities_minor"], 2000)
        self.assertEqual(state["net_credit_balance_minor"], -1500)
        self.assertEqual(state["investment_cash_minor"], 3000)
        self.assertEqual(state["holdings_minor"], 7000)
        self.assertEqual(state["stale_holdings_count"], 1)
        self.assertEqual(state["allocation_symbols"], [{"label": "SYNTH", "value_minor": 7000}])

    def test_pagination_totals_stable_and_independent_of_filters(self):
        for number in range(57):
            self.transaction(f"transaction-{number:03}", -100, memo="Example private note")
        self.transaction("inflow", 5000, category="Example income")
        first = overview.overview_state()
        second = overview.overview_state(page=2)
        self.assertEqual(len(first["transactions"]), 50)
        self.assertEqual(len(second["transactions"]), 8)
        self.assertFalse({row["id"] for row in first["transactions"]} & {row["id"] for row in second["transactions"]})
        self.assertEqual(first["filtered"]["outflow_minor"], 5700)
        filtered = overview.overview_state(status="inflows", query="Example income")
        self.assertEqual(filtered["filtered"]["count"], 1)
        self.assertEqual(filtered["month_totals"]["outflow_minor"], 5700)
        self.assertEqual(overview.overview_state(page_size=999)["page_size"], 200)
        self.assertEqual(overview.overview_state(query="private note")["filtered"]["count"], 0)
        self.assertEqual(overview.overview_state(query="private note", include_memo=True)["filtered"]["count"], 57)

    def test_leap_boundaries_prior_comparison_and_gross_budget(self):
        self.transaction("prior", -100, "2028-01-31")
        self.transaction("leap", -250, "2028-02-29")
        self.transaction("refund", 50, "2028-02-29")
        self.transaction("after", -999, "2028-03-01")
        with finance.connect(write=True) as connection:
            connection.execute("INSERT INTO finance_budgets VALUES ('budget', '2028-02', 'Example category', 200, '', '')")
        state = overview.overview_state()
        self.assertEqual(state["month_totals"]["outflow_minor"], 250)
        self.assertEqual(state["categories"][0]["outflow_minor"], 250)
        self.assertEqual(state["comparison"]["outflow_minor"]["change_bps"], 15000)
        self.assertIsNone(state["comparison"]["inflow_minor"]["change_bps"])
        self.assertEqual(state["budgets"][0]["overspend_minor"], 50)
        self.assertEqual(state["budgets"][0]["remaining_minor"], -50)

    def test_foreign_currency_excluded_from_combined_totals(self):
        self.account("foreign", "investment", 99999, currency="EUR")
        self.holding("foreign", 99999)
        self.transaction("foreign-flow", -99999, account="foreign")
        state = overview.overview_state()
        self.assertTrue(state["foreign_currency_excluded"])
        self.assertEqual(state["currency"], "USD")
        self.assertEqual(state["net_worth_minor"], 10000)
        self.assertEqual(state["holdings_minor"], 0)
        self.assertEqual(state["month_totals"]["outflow_minor"], 0)

    def test_snapshots_exact_sparse_dates_never_backfilled(self):
        with finance.connect(write=True) as connection:
            connection.executemany("INSERT INTO finance_net_worth_snapshots VALUES (?, ?, ?, '')",
                                   [("first", "2027-04-05", 12345), ("second", "2028-02-29", 12555),
                                    ("old", "2027-02-28", 999)])
        state = overview.overview_state()
        self.assertEqual(state["snapshots"], [{"snapshot_date": "2027-04-05", "total_minor": 12345},
                                             {"snapshot_date": "2028-02-29", "total_minor": 12555}])
        self.assertFalse(state["snapshot_currency_known"])

    def test_recurring_signed_projection_overdue_anchor_unchanged(self):
        with finance.connect(write=True) as connection:
            connection.executemany("INSERT INTO finance_recurring_items VALUES (?, 'everyday', 'Example obligation', 'Example category', ?, 'monthly', ?, 1, '', '')",
                                   [("out", -500, "2028-01-31"), ("in", 800, "2028-02-29")])
        state = overview.overview_state()
        self.assertEqual(state["upcoming_outflow_minor"], 500)
        self.assertEqual(state["upcoming_inflow_minor"], 800)
        self.assertEqual(state["overdue"][0]["next_due_date"], "2028-01-31")
        self.assertEqual(state["upcoming"][0]["scheduled_date"], "2028-02-29")
        with finance.connect() as connection:
            self.assertEqual(connection.execute("SELECT next_due_date FROM finance_recurring_items WHERE id = 'out'").fetchone()[0], "2028-01-31")

    def test_filters_reject_invalid_and_treat_search_literally(self):
        for arguments in ({"month": "bad"}, {"month": "0001-01"}, {"page": 0},
                          {"status": "pending"}, {"query": "x" * 121}, {"account_id": "unknown"}):
            with self.assertRaises(ValueError):
                overview.overview_state(**arguments)
        self.transaction("ordinary", -100)
        self.assertEqual(overview.overview_state(query="%' OR 1=1 --")["filtered"]["count"], 0)


class OverviewRouteTests(OverviewFixture):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse
        from fastapi.testclient import TestClient
        from http.cookies import SimpleCookie
        from luigi_web import auth
        from luigi_web.modules.finance import overview_routes

        self.routes = overview_routes
        application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        application.include_router(overview_routes.router)
        self.client = self.enterContext(TestClient(application, follow_redirects=False))
        self.render = self.enterContext(patch.object(overview_routes.templates, "TemplateResponse",
                                                    return_value=HTMLResponse("Private overview")))
        self.ui_cookie = auth.COOKIE_NAME
        self.finance_cookie = auth.FINANCE_COOKIE_NAME
        response = auth.finance_unlock_response("synthetic-finance")
        cookies = SimpleCookie()
        cookies.load(response.headers["set-cookie"])
        self.finance_session = cookies[auth.FINANCE_COOKIE_NAME].value
        self.headers = {"origin": "http://testserver", "x-csrf-token": "synthetic-csrf"}

    def unlock(self):
        self.client.cookies.set(self.ui_cookie, "synthetic-ui")
        self.client.cookies.set(self.finance_cookie, self.finance_session)
        self.client.cookies.set("luigi_csrf", "synthetic-csrf")

    def private_response(self, response, status):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(response.headers.get("referrer-policy"), "no-referrer")

    def test_both_auth_boundaries_and_bearer_needs_finance_cookie(self):
        with patch.object(finance, "connect", side_effect=AssertionError("Database must not be read")):
            self.private_response(self.client.get("/finance/overview"), 401)
            self.client.cookies.set(self.finance_cookie, self.finance_session)
            self.private_response(self.client.get("/finance/overview"), 401)
            self.client.cookies.clear()
            self.client.cookies.set(self.ui_cookie, "synthetic-ui")
            self.private_response(self.client.get("/finance/overview"), 403)
            self.client.cookies.clear()
            self.private_response(self.client.get("/finance/overview", headers={"authorization": "Bearer synthetic-ui"}), 403)
        self.client.cookies.set(self.finance_cookie, self.finance_session)
        self.private_response(self.client.get("/finance/overview", headers={"authorization": "Bearer synthetic-ui"}), 200)

    def test_redirects_private_and_fixed_destinations(self):
        response = self.client.get("/finance/overview", headers={"accept": "text/html"})
        self.private_response(response, 303)
        self.assertEqual(response.headers["location"], "/login")
        self.client.cookies.set(self.ui_cookie, "synthetic-ui")
        response = self.client.get("/finance/overview", headers={"accept": "text/html"})
        self.private_response(response, 303)
        self.assertEqual(response.headers["location"], "/finance/unlock")

    def test_post_requires_same_origin_csrf_even_standalone(self):
        self.unlock()
        for headers in ({}, {"origin": "http://testserver"},
                        {"origin": "https://example.invalid", "x-csrf-token": "synthetic-csrf"},
                        {**self.headers, "sec-fetch-site": "cross-site"},
                        {**self.headers, "x-csrf-token": "invalid"},
                        {**self.headers, "origin": "http://[invalid"}):
            self.private_response(self.client.post("/finance/overview/filter", data={"month": "2028-02"}, headers=headers), 403)
        self.private_response(self.client.post("/finance/overview/filter", data={"month": "2028-02"}, headers=self.headers), 200)

    def test_bearer_post_still_requires_finance_unlock(self):
        headers = {"authorization": "Bearer synthetic-ui"}
        self.private_response(self.client.post("/finance/overview/filter", data={"month": "2028-02"}, headers=headers), 403)
        self.client.cookies.set(self.finance_cookie, self.finance_session)
        self.private_response(self.client.post("/finance/overview/filter", data={"month": "2028-02"}, headers=headers), 200)

    def test_errors_generic_private_and_query_filters_rejected(self):
        self.unlock()
        for arguments in ({"month": "invalid"}, {"query": "x" * 121}, {"page": "invalid"}, {"private": "Example note"}):
            response = self.client.post("/finance/overview/filter", data=arguments, headers=self.headers)
            self.private_response(response, 422)
            self.assertEqual(response.json(), {"detail": "Invalid overview filters"})
        self.private_response(self.client.get("/finance/overview?month=2028-02"), 400)
        self.private_response(self.client.delete("/finance/overview"), 405)
        with patch.object(overview, "overview_state", side_effect=RuntimeError("Synthetic sensitive provider detail")):
            response = self.client.get("/finance/overview")
        self.private_response(response, 500)
        self.assertEqual(response.json(), {"detail": "Finance is temporarily unavailable"})

    def test_real_template_escapes_private_data_and_uses_body_filters(self):
        self.unlock()
        self.transaction("synthetic-script", -250, memo="<script>synthetic</script>")
        with patch.object(self.routes.templates, "TemplateResponse", wraps=self.routes.templates.__class__.TemplateResponse.__get__(self.routes.templates)):
            response = self.client.get("/finance/overview")
        self.private_response(response, 200)
        self.assertIn("&lt;script&gt;synthetic&lt;/script&gt;", response.text)
        self.assertNotIn("<script>synthetic</script>", response.text)
        self.assertIn('hx-history="false"', response.text)
        self.assertIn('action="/finance/overview/filter"', response.text)
        self.assertIn('href="/finance/records"', response.text)
        self.assertIn('href="/finance/planning"', response.text)
        self.assertIn('/static/js/vendor/chart.umd.min.js', response.text)
        self.assertNotIn('method="get"', response.text)
        seed = response.text.split('<script id="fo-chart-seed" type="application/json">', 1)[1].split('</script>', 1)[0]
        self.assertNotIn("synthetic-script", seed)
        self.assertNotIn("synthetic</script>", seed)

    def test_body_limits_and_duplicate_keys(self):
        self.unlock()
        headers = {**self.headers, "content-type": "application/x-www-form-urlencoded"}
        self.private_response(self.client.post("/finance/overview/filter", content="month=2028-02&month=2028-03", headers=headers), 422)
        self.private_response(self.client.post("/finance/overview/filter", content="query=" + "x" * 8200, headers=headers), 413)
        self.private_response(self.client.post("/finance/overview/filter", json={"month": "2028-02"}, headers=self.headers), 415)

    def test_outer_privacy_middleware_covers_host_early_errors(self):
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from fastapi.testclient import TestClient

        application = FastAPI()

        @application.middleware("http")
        async def host_rejection(request, call_next):
            return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)

        application.middleware("http")(self.routes.overview_privacy_middleware)
        with TestClient(application) as client:
            self.private_response(client.post("/finance/overview/filter", data={"month": "2028-02"}), 403)

    def test_filtered_real_render_retains_selection_and_chart_seed_is_escaped(self):
        self.unlock()
        self.transaction("chosen", -75, category="Example selected category")
        self.holding("everyday", 100)
        with finance.connect(write=True) as connection:
            connection.execute("UPDATE finance_accounts SET name = ? WHERE id = 'everyday'",
                               ("</script><script>synthetic</script>",))
        with patch.object(self.routes.templates, "TemplateResponse", wraps=self.routes.templates.__class__.TemplateResponse.__get__(self.routes.templates)):
            response = self.client.post("/finance/overview/filter", data={"month": "2028-02", "category": "Example selected category", "page": "2"}, headers=self.headers)
        self.private_response(response, 200)
        self.assertTrue('value="Example selected category" selected' in response.text, "Filter selection was not retained")
        self.assertTrue('<script>synthetic</script>' not in response.text, "Private text must remain escaped")
        self.assertTrue('\\u003c/script\\u003e' in response.text, "JSON script data must escape closing tags")


class OverviewFrontendTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.module = Path(str(module_files("luigi_web.modules.finance")))

    def test_template_icons_exist_in_local_bundle(self):
        template = (self.module / "templates" / "finance_overview.html").read_text(encoding="utf-8")
        icons = re.findall(r"shell_icon\(\s*['\"]([^'\"]+)['\"]", template)
        self.assertTrue(icons)
        directory = self.root / "luigi_web" / "core" / "static" / "icons" / "lucide"
        for icon in icons:
            with self.subTest(icon=icon):
                self.assertTrue((directory / f"{icon}.svg").is_file(), "Missing bundled icon")

    def test_source_privacy_and_responsive_contract(self):
        script = (self.module / "static" / "overview.js").read_text(encoding="utf-8")
        template = (self.module / "templates" / "finance_overview.html").read_text(encoding="utf-8")
        css = (self.module / "static" / "overview.css").read_text(encoding="utf-8")
        for forbidden in ("localStorage", "sessionStorage", "serviceWorker", "console.", "fetch(",
                          "sendBeacon", "URLSearchParams", "innerHTML", "document.cookie"):
            self.assertNotIn(forbidden, script)
        for forbidden in ("hx-boost", "method=\"get\"", "?month=", "?account", "?query", "https://", "http://"):
            self.assertNotIn(forbidden, template)
        self.assertIn('hx-history="false"', template)
        self.assertIn('chart_seed|tojson', template)
        self.assertIn('@media (max-width: 700px)', css)
        self.assertIn('minmax(0, 1fr)', css)
        self.assertIn('overflow-wrap: anywhere', css)
        self.assertIn('aria-describedby="fo-trend-table"', template)
        self.assertIn('aria-describedby="fo-history-table"', template)

    def test_browser_logic_exact_money_chart_fallback_and_lifecycle(self):
        self.run_javascript("lock")

    def test_pagehide_clears_private_state_before_bfcache_restore(self):
        self.run_javascript("pagehide")

    def test_persisted_pageshow_wipes_before_reloading(self):
        self.run_javascript("pageshow")

    def test_persisted_pageshow_reloads_even_without_overview_root(self):
        self.run_javascript("missing-root")

    def run_javascript(self, mode):
        node = shutil.which("node")
        if not node:
            package = find_spec("playwright")
            if package and package.origin:
                candidate = Path(package.origin).parent / "driver" / ("node.exe" if os.name == "nt" else "node")
                if candidate.is_file():
                    node = str(candidate)
        if not node:
            self.skipTest("Node is needed for the offline frontend harness")
        result = subprocess.run([node, "-e", OVERVIEW_HARNESS, str(self.module / "static" / "overview.js"), mode],
                                capture_output=True, text=True, timeout=20, check=False)
        self.assertTrue(result.returncode == 0, "Offline overview JavaScript contract failed")


OVERVIEW_HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const handlers = new Map();
const windowHandlers = new Map();
function fixture() {
    const amount = {dataset: {minor: '900719925474099301', currency: 'USD'}, textContent: ''};
    const negative = {dataset: {minor: '-1', currency: 'USD'}, textContent: ''};
    const seed = {currency: 'USD', trend: [{month: '2028-02', inflow_minor: '250', outflow_minor: '0'}],
        snapshots: [{date: '2028-02-15', value_minor: '100'}], allocation: [{label: 'Brokerage', value_minor: '100'}]};
    const seedElement = {textContent: JSON.stringify(seed)};
    const canvases = Object.fromEntries(['fo-trend-chart', 'fo-history-chart', 'fo-allocation-chart'].map(identifier => {
        const status = {hidden: true};
        return [identifier, {parentElement: {hidden: false}, status, closest: () => ({querySelector: () => status})}];
    }));
    const root = {
        id: 'finance-overview', hidden: false, childElementCount: 6, textContent: 'SYNTHETIC_PRIVATE_SENTINEL',
        querySelectorAll() {return this.childElementCount ? [amount, negative] : [];},
        querySelector(selector) {
            if (!this.childElementCount) return null;
            return selector === '#fo-chart-seed' ? seedElement : canvases[selector.slice(1)];
        },
        replaceChildren() {this.childElementCount = 0; this.textContent = '';}
    };
    return {root, amount, negative, seed, seedElement, canvases};
}
let current = fixture();
const document = {documentElement: {}, readyState: 'complete', getElementById: identifier =>
    identifier === 'finance-overview' ? current?.root : null,
    addEventListener: (name, callback) => handlers.set(name, callback)};
let observer;
let destroyed = 0;
let created = 0;
let reloads = 0;
const destinations = [];
const active = new Map();
const internalMaps = [];
class Chart {
    constructor(canvas, configuration) {created++; this.canvas = canvas; active.set(canvas, this); this.configuration = configuration;}
    destroy() {destroyed++; active.delete(this.canvas);}
    static getChart(canvas) {return active.get(canvas);}
}
function assertClean() {
    assert.equal(active.size, 0);
    assert.equal(internalMaps.length, 1);
    assert.equal(internalMaps[0].size, 0);
    assert.equal(observer.disconnected, true);
    if (current) {
        assert.equal(current.root.hidden, true);
        assert.equal(current.root.childElementCount, 0);
        assert.equal(current.root.textContent, '');
        assert.equal(current.root.querySelector('#fo-chart-seed'), null);
        assert.deepEqual(current.root.querySelectorAll('[data-minor]'), []);
    }
}
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
    document, Chart,
    window: {addEventListener: (name, callback) => windowHandlers.set(name, callback)},
    getComputedStyle: () => ({fontFamily: 'IBM Plex Sans', getPropertyValue: () => '#123456'}),
    MutationObserver: class {
        constructor(callback) {this.callback = callback; observer = this; this.disconnected = false;}
        observe() {}
        disconnect() {this.disconnected = true;}
    },
    Map: class extends Map {constructor(...entries) {super(...entries); internalMaps.push(this);}},
    location: {
        replace(path) {assertClean(); assert.equal(path, '/finance/unlock'); destinations.push(path);},
        reload() {assertClean(); reloads++;}
    }
});
const expected = new Intl.NumberFormat(undefined, {style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2})
    .formatToParts(9007199254740993n).map(part => part.type === 'fraction' ? '01' : part.value).join('');
assert.equal(current.amount.textContent, expected);
assert.equal(current.negative.textContent, new Intl.NumberFormat(undefined, {style:'currency', currency:'USD'}).format(-0.01));
assert.equal(active.size, 3);
assert.equal(active.get(current.canvases['fo-trend-chart']).configuration.data.datasets[0].data[0], 2.5);
handlers.get('htmx:afterSwap')();
assert.equal(created, 3);
handlers.get('htmx:beforeSwap')({detail: {target: current.root}});
assert.equal(destroyed, 3);
const oldCanvases = Object.values(current.canvases);
current = fixture();
handlers.get('htmx:afterSwap')();
assert.equal(active.size, 3);
assert.ok(oldCanvases.every(canvas => !active.has(canvas)));
observer.callback();
assert.equal(active.size, 3);
assert.equal(observer.disconnected, false);
handlers.get('htmx:beforeSwap')({detail: {target: current.root}});
current.seed.trend[0].inflow_minor = '900719925474099301';
current.seedElement.textContent = JSON.stringify(current.seed);
handlers.get('htmx:afterSwap')();
assert.equal(current.canvases['fo-trend-chart'].parentElement.hidden, true);
assert.equal(current.canvases['fo-trend-chart'].status.hidden, false);
assert.equal(active.size, 2);
handlers.get('htmx:afterRequest')({detail: {elt: {id: 'fo-lock'}, successful: false}});
assert.equal(active.size, 2);
assert.equal(current.root.hidden, false);
assert.equal(observer.disconnected, false);
assert.equal(destinations.length, 0);
windowHandlers.get('pageshow')({persisted: false});
assert.equal(reloads, 0);
assert.equal(active.size, 2);
switch (process.argv[2]) {
    case 'lock':
        handlers.get('htmx:afterRequest')({detail: {elt: {id: 'fo-lock'}, successful: true}});
        assert.equal(destinations.length, 1);
        break;
    case 'pagehide':
    case 'missing-root':
        windowHandlers.get('pagehide')({persisted: true});
        assertClean();
        windowHandlers.get('pagehide')({persisted: false});
        assertClean();
        if (process.argv[2] === 'missing-root') current = null;
        windowHandlers.get('pageshow')({persisted: true});
        assert.equal(reloads, 1);
        break;
    case 'pageshow':
        windowHandlers.get('pageshow')({persisted: true});
        assert.equal(reloads, 1);
        break;
    default: throw new Error('Unknown synthetic lifecycle');
}
assertClean();
const createdBeforeCleanup = created;
observer.callback();
assertClean();
current = fixture();
handlers.get('htmx:afterSwap')();
assertClean();
assert.equal(created, createdBeforeCleanup);
"""


if __name__ == "__main__":
    unittest.main()