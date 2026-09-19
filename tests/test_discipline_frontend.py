import unittest
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "luigi_web" / "modules" / "discipline" / "templates"


class Markup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def named(self, tag, name):
        return next(attrs for element, attrs in self.elements
                    if element == tag and attrs.get("name") == name)


class DisciplineFormTests(unittest.TestCase):
    def setUp(self):
        self.env = Environment(loader=FileSystemLoader(TEMPLATES),
                               autoescape=select_autoescape())

    def render(self, active=1, is_new=False):
        return self.env.get_template("partials/discipline_form.html").render(
            d={"uuid": "habit-example", "task": "Example habit", "catagory": "Example category",
               "frequency_per_week": 4, "active": active}, is_new=is_new)

    def test_labels_keep_existing_field_contract(self):
        source = self.render()
        self.assertIn("Discipline", source)
        self.assertIn("Weekly target", source)
        self.assertIn("Keep active", source)
        markup = Markup(source)
        self.assertEqual(markup.named("input", "frequency_per_week")["value"], "4")
        self.assertEqual(markup.named("input", "catagory")["value"], "Example category")
        self.assertEqual(markup.named("input", "task")["value"], "Example habit")

    def test_paused_values_are_not_checked(self):
        for active in (0, "0", False, None):
            with self.subTest(active=active):
                self.assertNotIn("checked", Markup(self.render(active)).named("input", "active"))
        for active in (1, "1", True):
            with self.subTest(active=active):
                self.assertIn("checked", Markup(self.render(active)).named("input", "active"))

    def test_new_form_defaults_to_daily_active(self):
        source = self.env.get_template("partials/discipline_form.html").render(d={}, is_new=True)
        markup = Markup(source)
        self.assertEqual(markup.named("input", "frequency_per_week")["value"], "7")
        self.assertIn("checked", markup.named("input", "active"))


class DisciplinePageTests(unittest.TestCase):
    def setUp(self):
        self.env = Environment(loader=ChoiceLoader([
            DictLoader({
                "base.html": """{% macro shell_icon(name) %}<span data-icon="{{ name }}"></span>{% endmacro %}
                    {% block head_scripts %}{% endblock %}{% block content %}{% endblock %}""",
                "partials/discipline_cell.html": """<button class="heatmap-cell" role="gridcell"
                    data-day="{{ day }}" data-discipline-uuid="{{ discipline_uuid }}"></button>""",
            }), FileSystemLoader(TEMPLATES),
        ]), autoescape=select_autoescape())
        self.env.globals["module_enabled"] = lambda name: name == "tasks"

    def habit(self, **overrides):
        habit = {"uuid": "habit-example", "task": "Example habit", "catagory": "Example category",
                 "frequency_per_week": 4, "active": 1, "_today_done": False,
                 "_streak": 0, "_year_days": set(),
                 "_weekly": {"count": 3, "target": 4, "remaining": 1, "target_met": False,
                             "week_start": "2026-09-14", "week_end": "2026-09-20"}}
        habit.update(overrides)
        return habit

    def render(self, habits=None, year=2026, progress_error=False):
        first = date(year, 1, 1)
        days = (date(year + 1, 1, 1) - first).days
        cells = [None] * first.weekday() + [first + timedelta(days=offset) for offset in range(days)]
        cells += [None] * (-len(cells) % 7)
        return self.env.get_template("discipline.html").render(
            disciplines=[self.habit()] if habits is None else habits,
            grid=[cells[offset::7] for offset in range(7)], year=year, years=[2024, 2025, 2026],
            today_iso="2026-09-18", week_start="2026-09-14", week_end="2026-09-20",
            page_title="Discipline", asset_version="test", progress_error=progress_error)

    def test_current_progress_preserves_the_full_annual_grid(self):
        source = self.render()
        markup = Markup(source)
        self.assertEqual(sum(attrs.get("role") == "gridcell" for _, attrs in markup.elements), 365)
        self.assertEqual(sum(attrs.get("role") == "row" for _, attrs in markup.elements), 7)
        self.assertIn("3 / 4 this week", source)
        self.assertIn("1 remaining", source)
        self.assertNotIn("Daily streak:", source)
        self.assertNotIn("failed", source.lower())
        self.assertIn('href="/discipline?year=2026"', source)
        self.assertIn('href="/discipline/history-preview"', source)

    def test_past_year_and_leap_year_keep_current_week_and_daily_label(self):
        source = self.render([self.habit(frequency_per_week=7, _streak=2)], year=2024)
        self.assertIn("Current week:", source)
        self.assertIn('datetime="2026-09-14"', source)
        self.assertIn("Daily streak: 2 days (current)", source)
        self.assertEqual(sum(attrs.get("role") == "gridcell" for _, attrs in Markup(source).elements), 366)
        self.assertIn('data-day="2024-12-31"', source)

    def test_excess_completions_remain_in_text_but_bar_is_capped(self):
        weekly = {"count": 6, "target": 4, "remaining": 0, "target_met": True}
        source = self.render([self.habit(_weekly=weekly)])
        progress = next(attrs for tag, attrs in Markup(source).elements if tag == "progress")
        self.assertEqual((progress["value"], progress["max"]), ("4", "4"))
        self.assertIn("6 / 4 this week", source)
        self.assertIn("Target met", source)

    def test_failed_progress_is_unavailable_not_zero(self):
        source = self.render([self.habit(_weekly=None, frequency_per_week=7, _streak=None)], progress_error=True)
        self.assertIn("Current week progress unavailable.", source)
        self.assertIn("Daily streak unavailable.", source)
        self.assertNotIn("0 / 4", source)
        details = next(attrs for _, attrs in Markup(source).elements if "data-discipline-weekly-details" in attrs)
        self.assertIn("hidden", details)
        notice = next(attrs for _, attrs in Markup(source).elements if "data-discipline-progress-error" in attrs)
        self.assertNotIn("hidden", notice)

    def test_active_paused_controls_and_history_are_preserved(self):
        for active in (0, "0", False):
            with self.subTest(active=active):
                source = self.render([self.habit(active=active)])
                markup = Markup(source)
                card = next(attrs for tag, attrs in markup.elements if tag == "article")
                self.assertEqual(card["data-discipline-active"], "false")
                self.assertIn("hidden", card)
                self.assertIn('hx-post="/discipline/habit-example/resume"', source)
                self.assertNotIn("data-discipline-today", source)
                self.assertEqual(sum(attrs.get("role") == "gridcell" for _, attrs in markup.elements), 365)
        source = self.render()
        self.assertIn('hx-post="/discipline/habit-example/deactivate"', source)
        self.assertIn('data-endpoint="/discipline/habit-example/today"', source)
        self.assertIn('data-discipline-status="active" aria-pressed="true"', source)

    def test_delete_confirmation_only_promises_undo_with_tasks(self):
        for tasks_enabled in (True, False):
            with self.subTest(tasks_enabled=tasks_enabled):
                self.env.globals["module_enabled"] = lambda name: tasks_enabled and name == "tasks"
                markup = Markup(self.render())
                delete = next(attrs for _, attrs in markup.elements if attrs.get("hx-post", "").endswith("/delete"))
                self.assertIn("all its completion history", delete["hx-confirm"])
                self.assertEqual("12 seconds" in delete["hx-confirm"], tasks_enabled)
                self.assertEqual("permanent" in delete["hx-confirm"], not tasks_enabled)

    def test_organization_controls_assets_and_escaping(self):
        source = self.render([self.habit(task='<script>example</script>', catagory='Example " category')])
        self.assertIn("&lt;script&gt;example&lt;/script&gt;", source)
        self.assertNotIn("<script>example</script>", source)
        for name in ("search", "category-filter", "target-filter", "pin", "move", "reset-layout", "clear-filters"):
            self.assertIn("data-discipline-" + name, source)
        self.assertIn('/module-assets/discipline/discipline.css?v=test', source)
        self.assertIn('/module-assets/discipline/discipline.js?v=test', source)
        for icon in ("plus", "search", "settings-2", "pin", "chevron-down", "arrow-left", "rotate-ccw"):
            self.assertTrue((ROOT / "luigi_web/core/static/icons/lucide" / f"{icon}.svg").is_file())

    def test_every_habit_keeps_an_annual_heatmap(self):
        source = self.render([self.habit(), self.habit(uuid="habit-paused", active=0)])
        self.assertEqual(sum(attrs.get("role") == "gridcell" for _, attrs in Markup(source).elements), 730)
        self.assertEqual(sum(attrs.get("role") == "grid" for _, attrs in Markup(source).elements), 2)


class DisciplineAssetTests(unittest.TestCase):
    def setUp(self):
        assets = ROOT / "luigi_web/modules/discipline/static"
        self.script = (assets / "discipline.js").read_text(encoding="utf-8")
        self.styles = (assets / "discipline.css").read_text(encoding="utf-8")

    def test_verified_event_only_refreshes_authenticated_uncached_progress(self):
        self.assertIn('document.addEventListener("luigi:discipline-updated"', self.script)
        self.assertIn('fetch("/discipline/progress"', self.script)
        self.assertIn('credentials: "same-origin", cache: "no-store"', self.script)
        self.assertIn("new AbortController()", self.script)
        self.assertIn("sequence !== progressSequence", self.script)
        self.assertIn("Last confirmed values are unchanged.", self.script)
        self.assertIn('document.body.addEventListener("undoCleared"', self.script)
        self.assertNotIn("setInterval", self.script)
        self.assertNotIn("htmx:afterSwap", self.script)
        self.assertNotIn('method: "POST"', self.script)
        self.assertNotIn("innerHTML", self.script)

    def test_storage_is_versioned_bounded_ids_with_readback_verification(self):
        self.assertIn('STORAGE_KEY = "luigi.discipline.layout"', self.script)
        self.assertIn("VERSION = 1", self.script)
        self.assertIn("MAX_IDS = 500", self.script)
        self.assertIn("MAX_ID_LENGTH = 128", self.script)
        self.assertIn("JSON.stringify({ version: VERSION, order: layout.order, pinned: layout.pinned })", self.script)
        self.assertIn("window.localStorage.getItem(STORAGE_KEY) !== serialized", self.script)
        self.assertIn("not confirmed saved", self.script)
        self.assertNotIn("sessionStorage", self.script)

    def test_current_progress_and_pin_group_contracts(self):
        self.assertIn("Math.min(weekly.count, weekly.target)", self.script)
        self.assertIn("${weekly.count} / ${weekly.target} this week", self.script)
        self.assertIn("layout.pinned.includes(candidate.dataset.disciplineUuid) === isPinned", self.script)
        self.assertIn('let statusFilter = "active"', self.script)
        self.assertIn("Daily streak:", self.script)
        self.assertNotIn("\U0001f525", self.script)

    def test_styles_preserve_contained_heatmap_and_readable_paused_history(self):
        self.assertIn(".discipline-workspace .heatmap { max-width: 100%; }", self.styles)
        self.assertIn(".discipline-workspace .discipline-inactive { opacity: 1; }", self.styles)
        self.assertIn("letter-spacing: 0", self.styles)
        self.assertNotIn(".heatmap-row", self.styles)
        self.assertNotIn(".heatmap-cell", self.styles)
        self.assertNotRegex(self.styles, r"font-size:\s*[^;]*(?:vw|clamp)")
        self.assertNotIn("https://", self.styles + self.script)


class DisciplineCellTests(unittest.TestCase):
    def test_paused_current_day_has_no_mutation_target(self):
        environment = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
        template = environment.get_template("partials/discipline_cell.html")
        for day, readonly, disabled in (("2030-04-05", True, True), ("2030-04-04", False, False), ("2030-04-06", False, True)):
            with self.subTest(day=day):
                markup = Markup(template.render(discipline_uuid="example", task="Example habit", catagory="Example", day=day,
                                                today_iso="2030-04-05", marked=False, readonly=readonly))
                cell = next(attrs for tag, attrs in markup.elements if tag == "button")
                self.assertEqual("disabled" in cell, disabled)
                self.assertEqual("hx-post" not in cell, disabled)


if __name__ == "__main__":
    unittest.main()