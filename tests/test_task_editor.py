"""Offline editor contracts, rendered only with synthetic task data."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import unittest

from jinja2 import Environment, FileSystemLoader, select_autoescape


TASKS_ROOT = Path(__file__).resolve().parents[1] / "module-repos" / "tasks" / "src" / "luigi_web" / "modules" / "tasks"


class FormMarkup(HTMLParser):
    def __init__(self, markup: str) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.feed(markup)

    def matching(self, tag: str, **attributes: str) -> list[dict[str, str | None]]:
        return [
            attrs for name, attrs in self.elements
            if name == tag and all(attrs.get(key) == value for key, value in attributes.items())
        ]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


def render_form(endpoint: str = "/tasks", is_new: bool = True, **row: object) -> str:
    environment = Environment(
        loader=FileSystemLoader(TASKS_ROOT / "templates"),
        autoescape=select_autoescape(("html",)),
    )
    environment.globals.update(
        recurring_days_list=lambda value: [int(day) for day in str(value or "").split(",") if day],
        recurrence_schedule_type=lambda task: (
            "monthly" if task.get("recurring_month_ordinal") else
            "weekdays" if task.get("recurring_days") else "interval"
        ),
        WEEKDAY_LABELS=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        MONTH_ORDINAL_OPTIONS=((1, "First"), (2, "Second"), (3, "Third"), (4, "Fourth"), (-1, "Last")),
    )
    return environment.get_template("partials/task_form.html").render(
        endpoint_root=endpoint,
        is_new=is_new,
        t={"uuid": "example-task", "priority": 0, **row},
        statuses=("Not Started", "In Progress", "Done"),
    )


class TaskEditorRenderTests(unittest.TestCase):
    def test_new_task_defaults_repeat_off_and_posts_to_tasks(self) -> None:
        markup = FormMarkup(render_form())
        self.assertEqual(markup.matching("form")[0]["hx-post"], "/tasks")
        self.assertNotIn("checked", markup.matching("input", name="recurring", type="checkbox")[0])
        self.assertEqual(markup.matching("input", name="recurring", type="hidden")[0]["value"], "0")

    def test_one_off_edit_has_no_recurring_controls(self) -> None:
        markup = FormMarkup(render_form(is_new=False, recurring=1, recurring_interval=7))
        self.assertEqual(markup.matching("form")[0]["hx-post"], "/tasks/example-task")
        self.assertFalse([attrs for _, attrs in markup.elements if str(attrs.get("name", "")).startswith("recurring")])

    def test_legacy_new_recurring_keeps_source_locked_on(self) -> None:
        markup = FormMarkup(render_form("/recurring"))
        self.assertEqual(markup.matching("form")[0]["hx-post"], "/recurring")
        toggle = markup.matching("input", name="recurring", type="checkbox")[0]
        self.assertIn("checked", toggle)
        self.assertIn("disabled", toggle)
        self.assertEqual(markup.matching("input", name="recurring", type="hidden")[0]["value"], "1")

    def test_recurring_edit_can_deactivate_without_changing_source(self) -> None:
        for active in (0, 1):
            with self.subTest(active=active):
                markup = FormMarkup(render_form("/recurring", False, recurring=active))
                self.assertEqual(markup.matching("form")[0]["hx-post"], "/recurring/example-task")
                toggle = markup.matching("input", name="recurring", type="checkbox")[0]
                self.assertEqual("checked" in toggle, bool(active))
                self.assertNotIn("disabled", toggle)

    def test_primary_fields_preserve_values_and_priority_range(self) -> None:
        markup = FormMarkup(render_form(
            is_new=False, task="Example <task>", status="In Progress",
            due_date="2030-10-14", project="Example project", priority=10,
        ))
        for name, value in (("task", "Example <task>"), ("due_date", "2030-10-14"), ("project", "Example project"), ("priority", "10")):
            self.assertEqual(markup.matching("input", name=name)[0]["value"], value)
        priority = markup.matching("input", name="priority")[0]
        self.assertEqual((priority["min"], priority["max"]), ("0", "10"))
        self.assertIn("selected", markup.matching("option", value="In Progress")[0])
        self.assertNotIn("onkeydown", markup.matching("input", name="due_date")[0])

    def test_collapsed_details_keep_enabled_values_including_zero(self) -> None:
        markup = FormMarkup(render_form(
            catagory="Example category", task_group="Example group", sub_group="Example subgroup",
            relevant_link="https://example.com", estimated_time=0,
        ))
        self.assertNotIn("open", markup.matching("details")[0])
        for name, value in (("catagory", "Example category"), ("task_group", "Example group"), ("sub_group", "Example subgroup"), ("relevant_link", "https://example.com"), ("estimated_time", "0")):
            control = markup.matching("input", name=name)[0]
            self.assertEqual(control["value"], value)
            self.assertNotIn("disabled", control)

    def test_inactive_schedules_are_disabled_before_javascript(self) -> None:
        for endpoint, is_new, active in (("/tasks", True, 0), ("/recurring", False, 0)):
            with self.subTest(endpoint=endpoint):
                markup = FormMarkup(render_form(endpoint, is_new, recurring=active))
                for _, attrs in markup.elements:
                    if str(attrs.get("name", "")).startswith("recurring_"):
                        self.assertIn("disabled", attrs)
                    if "data-task-repeat-panel" in attrs:
                        self.assertIn("hidden", attrs)

    def test_weekday_schedule_retains_repeated_keys_and_selected_days(self) -> None:
        markup = FormMarkup(render_form("/recurring", False, recurring=1, recurring_days="0,4"))
        days = markup.matching("input", name="recurring_days")
        self.assertEqual([day["value"] for day in days], [str(day) for day in range(7)])
        self.assertEqual([day["value"] for day in days if "checked" in day], ["0", "4"])
        self.assertTrue(all("disabled" not in day for day in days))
        self.assertIn("disabled", markup.matching("input", name="recurring_interval")[0])
        self.assertIn("disabled", markup.matching("select", name="recurring_month_ordinal")[0])

    def test_monthly_schedule_preserves_last_weekday_and_edit_target(self) -> None:
        markup = FormMarkup(render_form("/recurring", False, recurring=1, recurring_month_ordinal=-1, recurring_month_weekday=4))
        self.assertIn("selected", markup.matching("option", value="-1")[0])
        self.assertTrue(any("selected" in option for option in markup.matching("option", value="4")))
        self.assertNotIn("disabled", markup.matching("select", name="recurring_month_ordinal")[0])
        form = markup.matching("form")[0]
        self.assertEqual(form["hx-target"], "#card-example-task")
        self.assertEqual(form["hx-swap"], "outerHTML")
        self.assertIn("entity-form", str(form["class"]).split())


class TaskEditorAssetContracts(unittest.TestCase):
    def test_editor_owns_only_its_repeat_hooks_and_preserves_drafts(self) -> None:
        source = (TASKS_ROOT / "static" / "task-editor.js").read_text(encoding="utf-8")
        template = (TASKS_ROOT / "templates" / "partials" / "task_form.html").read_text(encoding="utf-8")
        self.assertNotIn("data-recurring-toggle", template)
        for hook in ("data-task-repeat", "htmx:afterSwap", "DOMContentLoaded", "editors.has(form)", "control.disabled = !active"):
            self.assertIn(hook, source)
        for forbidden in (".reset(", 'setAttribute("hx-post"', "fetch(", "localStorage", "sessionStorage", "createToday", "data-home-today"):
            self.assertNotIn(forbidden, source + template)
        self.assertIn("Number.isSafeInteger(value)", source)
        self.assertIn('days[0].setCustomValidity("Choose at least one weekday.")', source)
        self.assertIn('["1", "2", "3", "4", "-1"].includes(ordinal.value)', source)

    def test_verified_board_response_order_and_home_guard(self) -> None:
        source = (TASKS_ROOT / "static" / "task-editor.js").read_text(encoding="utf-8")
        self.assertIn('xhr.status >= 200 && xhr.status < 300', source)
        self.assertIn('form.hasAttribute("data-home-task-form")', source)
        self.assertIn('document.getElementById("kanban-board")', source)
        self.assertIn('triggers?.flashSuccess?.message !== expected.message', source)
        self.assertIn('article.card[data-uuid][data-endpoint]', source)
        events = ['new CustomEvent("flashSuccess"', 'new CustomEvent("closeModal"', 'new CustomEvent("reloadBoard"']
        self.assertEqual(sorted(source.index(event) for event in events), [source.index(event) for event in events])
        for event in events:
            self.assertIn("document.body.dispatchEvent(" + event, source)
        for event in ("htmx:beforeRequest", "htmx:afterRequest", "htmx:sendError", "htmx:timeout", "htmx:sendAbort"):
            self.assertIn(event, source)
        self.assertIn('error.textContent = message', source)
        self.assertNotIn("xhr.responseText", source[source.index('document.addEventListener("htmx:beforeOnLoad"'):])

    def test_css_is_scoped_and_mobile_has_stable_grid_and_hidden_panels(self) -> None:
        source = (TASKS_ROOT / "static" / "task-editor.css").read_text(encoding="utf-8")
        self.assertIn(".task-editor [hidden]", source)
        self.assertIn("display: none !important", source)
        self.assertIn("repeat(7, minmax(0, 1fr))", source)
        self.assertIn("@media (max-width: 520px)", source)
        self.assertNotIn("100vw", source)
        self.assertNotIn("https://", source)


if __name__ == "__main__":
    unittest.main()