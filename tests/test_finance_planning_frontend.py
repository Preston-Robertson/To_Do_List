"""Offline browser contracts using only synthetic values and a Node harness."""
from importlib.util import find_spec
import os
from pathlib import Path
from importlib.resources import files as module_files
import re
import shutil
import subprocess
import unittest


class PlanningFrontendTests(unittest.TestCase):
    def setUp(self):
        self.module = Path(str(module_files("luigi_web.modules.finance")))

    def run_javascript(self, harness, *arguments):
        node = shutil.which("node")
        if not node:
            package = find_spec("playwright")
            if package and package.origin:
                candidate = Path(package.origin).parent / "driver" / ("node.exe" if os.name == "nt" else "node")
                if candidate.is_file():
                    node = str(candidate)
        if not node:
            self.skipTest("Node is required for the offline planning harness")
        result = subprocess.run([node, "-e", harness, str(self.module / "static" / "planning.js"), *arguments],
                                capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, "Offline planning JavaScript contract failed: " + result.stderr)

    def test_template_and_javascript_icons_exist_in_local_bundle(self):
        template = (self.module / "templates" / "finance_planning.html").read_text(encoding="utf-8")
        script = (self.module / "static" / "planning.js").read_text(encoding="utf-8")
        icons = re.findall(r"shell_icon\(\s*['\"]([^'\"]+)['\"]", template)
        urls = re.findall(r"url\(['\"]?([^'\")]+)['\"]?\)", script)
        self.assertTrue(icons)
        self.assertTrue(urls)
        directory = Path(str(module_files("luigi_web.core"))) / "static"
        for icon in icons:
            with self.subTest(icon=icon):
                self.assertTrue((directory / "icons" / "lucide" / f"{icon}.svg").is_file(), "Missing bundled icon")
        for url in urls:
            with self.subTest(url=url):
                self.assertTrue(url.startswith("/static/icons/lucide/"))
                self.assertTrue((directory / url.removeprefix("/static/")).is_file(), "Missing bundled icon")

    def test_private_source_and_responsive_contract(self):
        script = (self.module / "static" / "planning.js").read_text(encoding="utf-8")
        template = (self.module / "templates" / "finance_planning.html").read_text(encoding="utf-8")
        css = (self.module / "static" / "planning.css").read_text(encoding="utf-8")
        for forbidden in ("localStorage", "sessionStorage", "indexedDB", "sendBeacon", "console.",
                          "URLSearchParams", "pushState", "replaceState", "innerHTML", "document.cookie"):
            self.assertNotIn(forbidden, script)
        for forbidden in ('method="get"', "https://", "http://", "investment_return_bps"):
            self.assertNotIn(forbidden, template)
        for required in ('hx-history="false"', 'planning_seed|tojson', 'aria-describedby="fp-tables"',
                         'role="tablist"', 'role="tabpanel"', 'method="post"'):
            self.assertIn(required, template)
        for required in ("seed.remove()", "root.replaceChildren()", '"pagehide"', '"htmx:beforeSwap"',
                         '"data-theme"', "chart?.destroy()", "response.record.version", "expected_version", "confirm("):
            self.assertIn(required, script)
        self.assertIn('@media (max-width: 700px)', css)
        self.assertIn('minmax(0, 1fr)', css)
        self.assertIn('overflow-wrap: anywhere', css)
        self.assertIn('height: 280px', css)

    def test_exact_parsing_transport_errors_and_cancellation(self):
        self.run_javascript(TRANSPORT_HARNESS)

    def test_editor_versions_saved_comparison_and_privacy_cleanup(self):
        self.run_javascript(EDITOR_HARNESS)

    def test_pagehide_and_persisted_restore_cleanup(self):
        self.run_javascript(EDITOR_HARNESS, "pagehide")


TRANSPORT_HARNESS = r'''
const assert = require("node:assert/strict");
const {scaledInteger, decimalString, formatMinor, transport} = require(process.argv[1]);
assert.equal(scaledInteger("0.29"), 29);
assert.equal(scaledInteger("-0.01"), -1);
assert.equal(scaledInteger("1.23"), 123);
assert.equal(scaledInteger("1000000000000.00"), 100000000000000);
assert.equal(scaledInteger("50", 0), 50);
assert.equal(decimalString(-1), "-0.01");
assert.equal(decimalString(100000000000000), "1000000000000.00");
assert.equal(formatMinor(29, "USD"), new Intl.NumberFormat(undefined, {style: "currency", currency: "USD"}).format(0.29));
for (const value of ["", "1e2", "1.001", "NaN", "Infinity", "1,000.00", "1000000000000.01", "--1"]) assert.throws(() => scaledInteger(value));
assert.throws(() => scaledInteger("1.1", 0));
assert.throws(() => decimalString(1.5));
(async () => {
  const requests = [];
  let status = 200;
  let redirected = "";
  global.fetch = async (url, options) => {
    requests.push({url, options});
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.cache, "no-store");
    assert.equal(options.referrerPolicy, "no-referrer");
    assert.ok(!url.includes("?"));
    return {ok: status === 200, status, json: async () => ({detail: "SYNTHETIC_PRIVATE_SENTINEL"})};
  };
  const api = transport(path => {redirected = path;});
  await api.request("save", {name: "Example scenario", amount_minor: 29});
  assert.equal(requests[0].url, "/finance/planning/save");
  assert.equal(requests[0].options.redirect, "error");
  assert.equal(requests[0].options.method, "POST");
  assert.equal(JSON.parse(requests[0].options.body).amount_minor, 29);
  await api.request("state");
  assert.equal(requests[1].options.method, "GET");
  assert.equal(requests[1].options.body, undefined);
  for (const code of [409, 422, 503]) {
    status = code;
    await assert.rejects(api.request("save", {}), error => error.status === code && !error.message.includes("SYNTHETIC_PRIVATE_SENTINEL"));
  }
  status = 403;
  await assert.rejects(api.request("save", {}));
  assert.equal(redirected, "/finance/unlock");
  status = 401;
  await assert.rejects(api.request("state"));
  assert.equal(redirected, "/login");
  assert.equal(requests.length, 7);
  await assert.rejects(api.request("unsupported", {}));
  assert.equal(requests.length, 7);
  global.fetch = async () => ({ok: false, status: 0, type: "opaqueredirect"});
  await api.request("lock", {});
  global.fetch = async (url, options) => new Promise((resolve, reject) => options.signal.addEventListener("abort", () => reject(new Error("SYNTHETIC_PRIVATE_SENTINEL"))));
  const pending = api.request("save", {});
  api.destroy();
  await assert.rejects(pending, error => error.status === 0 && !error.message.includes("SYNTHETIC_PRIVATE_SENTINEL"));
  await assert.rejects(api.request("state"));
})().catch(() => {process.exitCode = 1;});
'''


EDITOR_HARNESS = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Element {
    constructor(tag = "div", id = "") {
        Object.assign(this, {tag, id, children: [], dataset: {}, attributes: {}, handlers: {},
            value: "", defaultValue: "", type: "", name: "", required: false, checked: false,
            disabled: false, hidden: false, parent: null, style: {setProperty() {}}});
    }
    set textContent(value) {this.text = String(value); this.children = [];}
    get textContent() {return (this.text || "") + this.children.map(child => child.textContent).join("");}
    get childElementCount() {return this.children.length;}
    get options() {return this.children;}
    append(...nodes) {for (const child of nodes) {child.parent = this; this.children.push(child);}}
    prepend(node) {node.parent = this; this.children.unshift(node);}
    add(node) {this.append(node);}
    replaceChildren(...nodes) {this.children = []; this.text = ""; this.append(...nodes);}
    remove() {if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this);}
    setAttribute(name, value) {this.attributes[name] = value;}
    removeAttribute(name) {delete this.attributes[name];}
    addEventListener(name, action, options) {
        (this.handlers[name] ||= []).push(action);
        options?.signal?.addEventListener("abort", () => {this.handlers[name] = this.handlers[name].filter(item => item !== action);});
    }
    emit(name, target = this, properties = {}) {
        for (const action of this.handlers[name] || []) action({target, preventDefault() {}, ...properties});
    }
    matches(selector) {
        if (selector.endsWith(":checked")) return this.checked && this.matches(selector.slice(0, -8));
        if (selector.startsWith("#")) return this.id === selector.slice(1);
        const attribute = /^\[([^=\]]+)(?:="([^"]*)")?\]$/.exec(selector);
        if (attribute) {
            const key = attribute[1];
            const value = key.startsWith("data-") ? this.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())]
                : this.attributes[key] ?? this[key];
            return attribute[2] === undefined ? value !== undefined && value !== "" : String(value) === attribute[2];
        }
        return this.tag === selector;
    }
    querySelectorAll(selector) {
        const selectors = selector.split(",").map(part => part.trim());
        return this.children.flatMap(child => [...(selectors.some(part => child.matches(part)) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
    querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
    closest(selector) {return this.matches(selector) ? this : this.parent?.closest(selector);}
    contains(node) {return node === this || this.children.some(child => child.contains(node));}
    focus() {}
    reset() {for (const input of this.querySelectorAll("input, select")) {input.value = input.defaultValue; input.checked = input.defaultChecked;}}
}
const root = new Element("div", "finance-planning");
const nodes = {};
function add(id, tag = "div", parent = root) {const node = new Element(tag, id); nodes[id] = node; parent.append(node); return node;}
function input(parent, name, value, type = "text", scale = false) {
    const node = new Element(type === "select" ? "select" : "input");
    Object.assign(node, {name, type, value: String(value), defaultValue: String(value)});
    if (scale) node.dataset.scale = "2";
    parent.append(node); return node;
}
const defaults = {schema_version: 1, currency: "USD", start_month: "2028-03", income_mode: "fixed", months: 12, years: 2,
    opening_cash_minor: 10000, opening_investments_minor: 20000, net_income_minor: 300,
    variable_expense_minor: 0, monthly_contribution_minor: 0, reserve_minor: 0,
    annual_income_growth_bps: 0, annual_expense_growth_bps: 0, annual_return_bps: 0, inflation_bps: 0};
const housing = {home_price_minor: 120000, down_payment_minor: 0, years: 1};
let savedState = {currency: "USD", baseline_as_of: "2028-02-15", defaults,
    plans: [{id: "first", kind: "cashflow", name: "Example first", config: defaults, version: 1},
                    {id: "second", kind: "cashflow", name: "Example second", config: {...defaults}, version: 1},
                    {id: "home-first", kind: "housing", name: "Example home first", config: housing, version: 1},
                    {id: "home-second", kind: "housing", name: "Example home second", config: housing, version: 1}],
    income_streams: [{id: "income-first", version: 1, name: "Example pay", amount_minor: 300, cadence: "monthly",
        start_date: "2028-03-01", end_date: null, annual_growth_bps: 0, active: true}]};
add("fp-seed", "script").textContent = JSON.stringify(savedState);
for (const id of ["fp-status", "fp-version", "fp-baseline", "fp-income-version"]) add(id);
for (const id of ["fp-load", "fp-new", "fp-save", "fp-delete", "fp-current", "fp-reload", "fp-compare", "fp-compare-current", "fp-income-new", "fp-income-delete"]) add(id, "button");
for (const id of ["fp-saved", "fp-compare-draft"]) add(id, "select");
add("fp-name", "input");
const tabs = add("tabs"); tabs.setAttribute("role", "tablist");
for (const mode of ["cashflow", "wealth", "housing"]) add(`fp-tab-${mode}`, "button", tabs).dataset.tab = mode;
const calculate = add("fp-calculate", "form");
const cash = add("fp-cash-fields", "fieldset", calculate);
const home = add("fp-housing-fields", "fieldset", calculate);
add("fp-run", "button", calculate);
const fixed = add("fp-fixed-income", "div", cash);
const growth = add("fp-income-growth", "div", cash);
for (const [key, value] of Object.entries(defaults)) {
    if (["currency", "schema_version"].includes(key)) continue;
    const parent = key === "net_income_minor" ? fixed : key === "annual_income_growth_bps" ? growth : cash;
    input(parent, key, value, ["months", "years"].includes(key) ? "number" : "text", key.endsWith("_minor") || key.endsWith("_bps"));
}
input(home, "home_price_minor", "", "text", true).required = true;
input(home, "down_payment_minor", "0.00", "text", true);
input(home, "years", "1", "number");
const incomeForm = add("fp-income-form", "form");
const incomeFields = add("fp-income-fields", "fieldset", incomeForm);
for (const [key, value] of Object.entries(savedState.income_streams[0])) {
    if (["id", "version"].includes(key)) continue;
    const field = input(incomeFields, key, value ?? "", key === "active" ? "checkbox" : "text", key.endsWith("_minor") || key.endsWith("_bps"));
    if (key === "active") field.defaultChecked = true;
}
add("income-submit", "button", incomeForm).type = "submit";
add("fp-income-list", "tbody");
add("fp-compare-list", "fieldset");
const output = add("fp-output");
for (const id of ["fp-result-state", "fp-summary", "fp-warnings", "fp-tables", "fp-assumptions", "fp-chart-status"]) add(id, "div", output);
add("fp-chart", "canvas", add("fp-chart-wrap", "div", output));
add("lock-submit", "button", add("fp-lock", "form"));
global.Option = class extends Element {constructor(text, value) {super("option"); this.textContent = text; this.value = value;}};
global.document = new Element("document");
document.documentElement = new Element("html"); document.createElement = tag => new Element(tag);
document.readyState = "complete";
document.getElementById = id => id === "finance-planning" ? root : null;
global.window = new Element("window");
let destination;
global.location = {replace: value => {
    assert.equal(root.childElementCount, 0);
    assert.equal(charts.size, 0);
    assert.equal(observer.disconnected, true);
    destination = value;
}};
global.confirm = () => true;
global.getComputedStyle = () => ({fontFamily: "IBM Plex Sans", getPropertyValue: () => "#359876"});
let observer;
global.MutationObserver = class {constructor(callback) {this.callback = callback; observer = this;} observe() {} disconnect() {this.disconnected = true;}};
const charts = new Map();
global.Chart = class {constructor(canvas, config) {this.canvas = canvas; this.config = config; charts.set(canvas, this);} destroy() {charts.delete(this.canvas);} static getChart(canvas) {return charts.get(canvas);}};
const requests = [];
let failNext = false;
global.fetch = async (url, options) => {
    requests.push({url, body: options.body && JSON.parse(options.body)});
    if (failNext) {failNext = false; throw new Error("SYNTHETIC_PRIVATE_SENTINEL");}
    const body = requests.at(-1).body;
    let data = savedState;
    if (url.endsWith("/save")) {
        const collection = url.includes("/income/") ? savedState.income_streams : savedState.plans;
        const previous = collection.find(record => record.id === body.id);
        if (previous && previous.version !== body.expected_version) return {ok: false, status: 409};
        const record = {...body, id: body.id || "new", version: previous ? previous.version + 1 : 1};
        if (previous) collection.splice(collection.indexOf(previous), 1, record); else collection.push(record);
        data = {record};
    }
    if (url.endsWith("/compare")) {
        const result = {rows: [{year: 1, rent_cost_minor: 0, owner_cost_minor: 10000, mortgage_balance_minor: 110000,
            equity_minor: 10000, total_cash_out_minor: 10000, cumulative_rent_cost_minor: 0}], monthly_payment_minor: 1000, assumptions: []};
        data = {kind: "housing", scenarios: body.plan_ids.map(id => ({...savedState.plans.find(plan => plan.id === id), result}))};
    }
    if (url === "/finance/lock") return {ok: false, status: 0, type: "opaqueredirect"};
    return {ok: true, status: 200, json: async () => structuredClone(data)};
};
const settle = () => new Promise(resolve => setImmediate(resolve));
async function click(id) {nodes[id].emit("click"); await settle();}
(async () => {
    const browser = {document, window, location, confirm, getComputedStyle, MutationObserver, Chart,
        Option, fetch, AbortController, setTimeout, clearTimeout};
    vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), browser);
    assert.equal(requests.length, 0);
    assert.equal(root.querySelector("#fp-seed"), null);
    window.emit("pageshow", window, {persisted: false});
    assert.equal(destination, undefined);
    assert.equal(cash.querySelector('[name="opening_cash_minor"]').value, "100.00");
    assert.equal(home.querySelector('[name="home_price_minor"]').value, "");
    nodes["fp-saved"].value = "first";
    await click("fp-load");
    cash.querySelector('[name="net_income_minor"]').value = "0.29";
    await click("fp-save");
    assert.equal(requests[0].body.config.net_income_minor, 29);
    assert.equal(requests[0].body.expected_version, 1);
    assert.match(nodes["fp-status"].textContent, /saved and verified/);
    assert.match(nodes["fp-version"].textContent, /2/);
    savedState.plans.find(plan => plan.id === "first").version = 3;
    await click("fp-save");
    assert.match(nodes["fp-status"].textContent, /Record changed/);
    await click("fp-reload");
    assert.match(nodes["fp-version"].textContent, /2; reload required/);
    await click("fp-load");
    assert.match(nodes["fp-version"].textContent, /3/);
    failNext = true;
    await click("fp-save");
    assert.equal(nodes["fp-save"].disabled, true);
    assert.match(nodes["fp-status"].textContent, /Write not confirmed/);
    assert.ok(!nodes["fp-status"].textContent.includes("SYNTHETIC_PRIVATE_SENTINEL"));
    await click("fp-reload");
    assert.equal(nodes["fp-save"].disabled, false);
    const incomeButton = nodes["fp-income-list"].querySelector("[data-income-id]");
    nodes["fp-income-list"].emit("click", incomeButton);
    incomeForm.emit("submit"); await settle();
    assert.match(nodes["fp-income-version"].textContent, /2/);
    await click("fp-tab-housing");
    for (const checkbox of root.querySelectorAll("[data-plan-choice]")) checkbox.checked = true;
    await click("fp-compare");
    assert.match(nodes["fp-status"].textContent, /Comparison complete/);
    assert.deepEqual(requests.find(request => request.url.endsWith("/compare")).body.plan_ids, ["home-first", "home-second"]);
    assert.equal(charts.size, 1);
    observer.callback(); assert.equal(charts.size, 1);
    browser.Chart = undefined;
    observer.callback(); assert.equal(nodes["fp-chart-status"].hidden, false);
    assert.equal(nodes["fp-chart-wrap"].hidden, true);
    browser.Chart = Chart;
    observer.callback(); assert.equal(charts.size, 1);
    if (process.argv[2] === "pagehide") {
        window.emit("pagehide", window, {persisted: true});
        assert.equal(root.childElementCount, 0);
        assert.equal(charts.size, 0);
        assert.equal(observer.disconnected, true);
        assert.equal(destination, undefined);
        window.emit("pageshow", window, {persisted: true});
        assert.equal(destination, "/finance/planning");
    } else {
        nodes["fp-lock"].emit("submit"); await settle();
        assert.equal(destination, "/finance/unlock");
    }
    assert.equal(root.childElementCount, 0);
    assert.equal(root.textContent, "");
    assert.equal(root.querySelector("#fp-seed"), null);
    assert.equal(charts.size, 0);
    assert.equal(observer.disconnected, true);
    observer.callback();
    document.emit("htmx:afterSwap");
    const previousRequests = requests.length;
    await click("fp-reload");
    assert.equal(requests.length, previousRequests);
    assert.equal(root.childElementCount, 0);
    assert.equal(charts.size, 0);
})().catch(error => {process.stderr.write(error.stack); process.exitCode = 1;});
'''


if __name__ == "__main__":
    unittest.main()