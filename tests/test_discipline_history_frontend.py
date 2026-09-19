"""Offline live-history contracts. No app startup or repository imports."""
import json
import mimetypes
import re
import sys
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "luigi_web/modules/discipline"
BASE = """{% macro shell_icon(name) %}<span class="shell-icon" data-icon="{{ name }}"></span>{% endmacro %}
{% block head_scripts %}{% endblock %}<button data-command-open>Search</button>
{% if module_enabled('assistant') %}<button data-assistant-open>Assistant</button>{% endif %}
{% block content %}{% endblock %}"""


def synthetic_seed(year=2024):
    return {
        "uuid": "00000000-0000-4000-8000-000000000001", "name": "Example habit",
        "category": "Example category", "active": True, "today": "2026-09-18",
        "year": year, "timezone": "America/New_York", "weeklyTarget": 3,
        "weekly": {"count": 2, "target": 3, "remaining": 1, "target_met": False,
                   "week_start": "2026-09-14", "week_end": "2026-09-20"},
        "completions": [
            {"date": f"{year}-01-01", "loggedAt": f"{year}-01-02T09:45:00-05:00", "version": "a" * 64},
            {"date": f"{year}-12-31", "loggedAt": None, "version": "b" * 64},
        ], "emptyVersion": "0" * 64,
    }


def template_environment():
    environment = Environment(loader=ChoiceLoader([
        DictLoader({"base.html": BASE}), FileSystemLoader(MODULE / "templates"),
    ]), autoescape=select_autoescape())
    environment.globals["module_enabled"] = lambda name: name in {"assistant", "discipline", "tasks"}
    return environment


class Markup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.seed = ""
        self.in_seed = False
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag == "script" and attributes.get("id") == "dh-seed":
            self.in_seed = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_seed = False

    def handle_data(self, data):
        if self.in_seed:
            self.seed += data


class HistoryFrontendTests(unittest.TestCase):
    def setUp(self):
        self.environment = template_environment()
        self.script = (MODULE / "static/history.js").read_text(encoding="utf-8")
        self.styles = (MODULE / "static/history.css").read_text(encoding="utf-8")

    def render(self, seed=None):
        return self.environment.get_template("history.html").render(
            history_seed=synthetic_seed() if seed is None else seed,
            page_title="History", active_nav="discipline", asset_version="test")

    def test_template_compiles_and_preserves_shell(self):
        source = self.render()
        self.assertIn("data-command-open", source)
        self.assertIn("data-assistant-open", source)
        self.assertIn('href="/discipline"', source)
        self.assertIn("Example habit</h1>", source)
        self.assertIn("history-example.css?v=test", source)
        self.assertIn("history.css?v=test", source)
        self.assertIn("history.js?v=test", source)
        self.assertNotIn("history-example.js", source)

    def test_tabs_native_dialog_and_year_bounds(self):
        markup = Markup(self.render())
        ids = [attrs["id"] for _, attrs in markup.elements if "id" in attrs]
        self.assertEqual(len(ids), len(set(ids)))
        elements = {attrs["id"]: (tag, attrs) for tag, attrs in markup.elements if "id" in attrs}
        for view in ("month", "year", "log"):
            tab = elements[f"dh-{view}-tab"][1]
            panel = elements[f"dh-{view}-panel"][1]
            self.assertEqual(tab["role"], "tab")
            self.assertEqual(tab["aria-controls"], f"dh-{view}-panel")
            self.assertEqual(tab["aria-selected"], str(view == "month").lower())
            self.assertEqual(tab["tabindex"], "0" if view == "month" else "-1")
            self.assertEqual(panel["role"], "tabpanel")
            self.assertEqual(panel["aria-labelledby"], f"dh-{view}-tab")
            self.assertEqual("hidden" in panel, view != "month")
        self.assertEqual(elements["dh-day-dialog"][0], "dialog")
        year = elements["dh-year-input"][1]
        self.assertEqual((year["type"], year["min"], year["max"], year["step"]), ("number", "1900", "2027", "1"))
        self.assertNotIn('type="time"', self.render())

    def test_seed_and_user_values_are_escaped(self):
        seed = synthetic_seed()
        seed["name"] = '</script><img src=x onerror="example()">'
        seed["category"] = '<script>example()</script>'
        source = self.render(seed)
        self.assertNotIn(seed["name"], source)
        self.assertNotIn(seed["category"], source)
        self.assertEqual(json.loads(Markup(source).seed), seed)
        self.assertIn("&lt;/script&gt;", source)

    def test_main_link_is_encoded_numeric_and_not_a_heatmap_action(self):
        source = self.environment.get_template("discipline.html").render(
            disciplines=[{"uuid": 'synthetic?&"id', "task": "Example private habit",
                          "active": 0, "catagory": "", "frequency_per_week": 3,
                          "_streak": None, "_year_days": set()}],
            year="2024", years=[2024], today_iso="2026-09-18", grid=[],
            week_start="2026-09-14", week_end="2026-09-20", asset_version="test")
        links = [attrs for tag, attrs in Markup(source).elements if tag == "a" and "/history?" in attrs.get("href", "")]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["href"], "/discipline/synthetic%3F%26%22id/history?year=2024")
        self.assertFalse(any(key.startswith("hx-") or key == "data-dh-day" for key in links[0]))
        self.assertNotIn("Example private habit", links[0]["href"])

    def test_calendar_and_current_week_contracts(self):
        for snippet in ("length: 42", "(first.getUTCDay() + 6) % 7", "Date.UTC(state.year + 1, 0, 1)",
                        "Math.ceil((offset + count) / 7)", "length: columns * 7", "year !== state.year",
                        "const weekly = state.weekly", "Latest completion in ${state.year}",
                        "Current weekly target: ${state.weeklyTarget}", "countBetween(boundedStart, boundedEnd)"):
            self.assertIn(snippet, self.script)
        for key in ("ArrowLeft", "ArrowRight", "Home", "End", "ArrowDown", "ArrowUp"):
            self.assertIn(key, self.script)
        self.assertIn("var(--dh-year-columns, 53)", self.styles)
        self.assertNotRegex(self.styles, r"font-size:[^;]*(?:vw|clamp)")
        self.assertTrue(all(line.startswith(".dh-live") or line.startswith("@media") or line.startswith("  .dh-live") or line == "}" for line in self.styles.splitlines()))

    def test_logged_time_is_not_invented_and_pause_is_date_scoped(self):
        self.assertNotRegex(self.script + self.render(), r"completedAt|completed_at|Completion time|Completed at|type=\"time\"")
        self.assertIn('value === null ? "Not recorded"', self.script)
        self.assertIn('time.removeAttribute("datetime")', self.script)
        self.assertIn("Logged at (${state.timezone})", self.script)
        self.assertIn("value <= state.today && (Boolean(state.active) || value < state.today)", self.script)
        self.assertIn('"Paused today; read only."', self.script)

    def test_requests_are_confirmed_versioned_and_fail_closed(self):
        for snippet in ('credentials: "same-origin", cache: "no-store"', 'redirect: "error"',
                        'body.set("expected_version", records.get(selectedDay)?.version || state.emptyVersion)',
                        'body.set("token", undo.token)', 'body.set("year", String(state.year))',
                        'applyState(payload.state, month)', 'if (pending || needsRefresh) return',
                        'needsRefresh = true', 'Last confirmed values are unchanged.',
                        'Reload before another change.', 'Math.min(12000, payload.undo_ttl_ms)',
                        'performance.now() >= undo.expires', 'controller.abort()',
                        'if (!validState(next, state.uuid, year))', 'clearUndo(); syncControls()'):
            self.assertIn(snippet, self.script)
        self.assertIn('dialog.open && !pending && (!dialog.contains(document.activeElement)', self.script)
        self.assertIn('find("#dh-cancel").focus()', self.script)
        self.assertNotRegex(self.script, r"records\.(?:set|delete|clear)\(")
        self.assertLess(self.script.index('applyState(payload.state, month)'), self.script.index('rememberUndo(payload, started);'))

    def test_no_persistence_unsafe_html_or_shell_sandbox(self):
        self.assertNotRegex(self.script, r"localStorage|sessionStorage|indexedDB|innerHTML|outerHTML|insertAdjacentHTML|document\.cookie|window\.fetch\s*=|htmx:|stopImmediatePropagation|data-assistant-open|data-command-open")
        self.assertNotRegex(self.script, r"https?://|completedAt")
        self.assertIn("textContent", self.script)
        template = (MODULE / "templates/history.html").read_text(encoding="utf-8")
        for name in re.findall(r"shell_icon\('([\w-]+)'\)", template):
            self.assertTrue((ROOT / "luigi_web/core/static/icons/lucide" / f"{name}.svg").is_file(), name)


def browser_markup(year=2024):
    environment = Environment(loader=FileSystemLoader([
        MODULE / "templates", ROOT / "luigi_web/core/templates",
    ]), autoescape=select_autoescape())
    environment.globals["module_enabled"] = lambda name: name in {"discipline", "assistant"}
    return environment.get_template("history.html").render(
        history_seed=synthetic_seed(year), page_title="Discipline history", active_nav="discipline",
        asset_version="test", shell_asset_version="test", landing_path="/discipline", module_count=2,
        navigation_groups=[("Focus", [{"href": "/discipline", "key": "discipline", "label": "Discipline", "icon": "calendar-days"}])])


class BrowserFixtureTests(unittest.TestCase):
    def test_real_shell_compiles_without_app_imports(self):
        source = browser_markup()
        self.assertIn('src="/static/js/app.js?v=test"', source)
        self.assertIn('src="/module-assets/assistant/panel.js?v=test"', source)
        self.assertIn("data-command-open", source)
        self.assertIn("data-assistant-open", source)
        self.assertEqual(json.loads(Markup(source).seed), synthetic_seed())


class SyntheticBrowserHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            try:
                year = int(parse_qs(parsed.query).get("year", ["2024"])[0])
            except ValueError:
                self.send_error(400)
                return
            if not 1900 <= year <= 2027:
                self.send_error(400)
                return
            payload = browser_markup(year).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        else:
            roots = {
                "/static/": ROOT / "luigi_web/core/static",
                "/module-assets/discipline/": MODULE / "static",
                "/module-assets/assistant/": ROOT / "luigi_web/modules/assistant/static",
            }
            for prefix, directory in roots.items():
                if not parsed.path.startswith(prefix):
                    continue
                asset = (directory / parsed.path.removeprefix(prefix)).resolve()
                if not asset.is_relative_to(directory.resolve()) or not asset.is_file():
                    self.send_error(404)
                    return
                payload = asset.read_bytes()
                content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                break
            else:
                self.send_error(404)
                return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    if sys.argv[1:] == ["--serve-browser-fixture"]:
        with ThreadingHTTPServer(("127.0.0.1", 0), SyntheticBrowserHandler) as server:
            print(f"Synthetic history fixture: http://127.0.0.1:{server.server_port}", flush=True)
            server.serve_forever()
    else:
        unittest.main()