"""Offline task-view contracts using synthetic records and an optional Node VM."""
from __future__ import annotations

import os
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

from luigi_web.core.templating import create_templates
from luigi_web.modules.tasks import events, repository


ROOT = Path(__file__).resolve().parents[1]
TASKS_ROOT = ROOT / "luigi_web" / "modules" / "tasks"
HARNESS = Path(__file__).with_name("task_views_harness.js")
STATUSES = ("Not Started", "In Progress", "Completed")


def node_executable() -> str | None:
    for command in ("node", "nodejs"):
        executable = shutil.which(command)
        if executable:
            return executable
    if os.name != "nt":
        return None
    roots: list[Path] = []
    code = shutil.which("code")
    if code:
        roots.append(Path(code).resolve().parent.parent)
    for variable, suffix in (
        ("LOCALAPPDATA", "Programs/Microsoft VS Code"),
        ("ProgramFiles", "Microsoft VS Code"),
        ("ProgramFiles(x86)", "Microsoft VS Code"),
    ):
        if os.environ.get(variable):
            roots.append(Path(os.environ[variable]) / suffix)
    for root in dict.fromkeys(roots):
        for pattern in ("node.exe", "*/node.exe", "*/*/node.exe"):
            for candidate in sorted(root.glob(pattern)):
                if candidate.is_file():
                    return str(candidate)
    return None


class TaskViewsRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        executable = node_executable()
        if executable is None:
            raise unittest.SkipTest("Node is unavailable; task-view JavaScript runtime checks were NOT executed")
        cls.node = executable

    def run_case(self, name: str) -> None:
        environment = {
            key: value for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
        }
        environment["ELECTRON_RUN_AS_NODE"] = "1"
        result = subprocess.run(
            [self.node, str(HARNESS), name],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), f"PASS {name}")

    def test_exports_and_board_defaults(self) -> None:
        self.run_case("exports_and_board_defaults")

    def test_view_schema_whitelists(self) -> None:
        self.run_case("view_schema_whitelists")

    def test_filter_types_and_bounds(self) -> None:
        self.run_case("filter_types_and_bounds")

    def test_column_sets_reject_duplicates_unknown_and_zero_shown(self) -> None:
        self.run_case("column_sets_reject_duplicates_unknown_and_zero_shown")

    def test_names_and_store_schema_limits(self) -> None:
        self.run_case("names_and_store_schema_limits")

    def test_legacy_migration_and_limits(self) -> None:
        self.run_case("legacy_migration_and_limits")

    def test_storage_roundtrip_scope_and_legacy_precedence(self) -> None:
        self.run_case("storage_roundtrip_scope_and_legacy_precedence")

    def test_corrupt_storage_falls_back_without_overwriting(self) -> None:
        self.run_case("corrupt_storage_falls_back_without_overwriting")

    def test_inaccessible_storage_keeps_usable_page_state(self) -> None:
        self.run_case("inaccessible_storage_keeps_usable_page_state")

    def test_write_requires_validation_and_exact_readback(self) -> None:
        self.run_case("write_requires_validation_and_exact_readback")

    def test_source_keys_prevent_uuid_collisions(self) -> None:
        self.run_case("source_keys_prevent_uuid_collisions")

    def test_completion_dataset_and_status_are_not_truthiness(self) -> None:
        self.run_case("completion_dataset_and_status_are_not_truthiness")

    def test_filters_are_conjunctive_and_search_all_fields(self) -> None:
        self.run_case("filters_are_conjunctive_and_search_all_fields")

    def test_week_bounds_are_local_monday_to_sunday(self) -> None:
        self.run_case("week_bounds_are_local_monday_to_sunday")

    def test_smart_date_filters_respect_inclusive_boundaries(self) -> None:
        self.run_case("smart_date_filters_respect_inclusive_boundaries")

    def test_other_smart_filters_distinguish_source_and_completion(self) -> None:
        self.run_case("other_smart_filters_distinguish_source_and_completion")

    def test_sorting_handles_missing_dates_priorities_titles_and_ties(self) -> None:
        self.run_case("sorting_handles_missing_dates_priorities_titles_and_ties")

    def test_mount_renders_names_as_text_and_preserves_column_order(self) -> None:
        self.run_case("mount_renders_names_as_text_and_preserves_column_order")


class Markup(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))

    def matching(self, tag: str | None = None, **attributes: str) -> list[dict[str, str | None]]:
        return [
            attrs for name, attrs in self.elements
            if (tag is None or name == tag) and all(attrs.get(key) == value for key, value in attributes.items())
        ]

    def hooked(self, name: str) -> list[dict[str, str | None]]:
        return [attrs for _, attrs in self.elements if name in attrs]


def render_tasks(*, empty: bool = False) -> str:
    rows = [] if empty else [
        {
            "uuid": "example-shared-id", "task": "Example <task>", "status": "Not Started",
            "completed": False, "priority": 0, "project": "Example project", "catagory": "Example category",
            "task_group": "Example group", "sub_group": "Example subgroup", "due_date": "2030-01-02",
            "_endpoint_root": "/tasks",
        },
        {
            "uuid": "example-shared-id", "task": "Example recurring", "status": "Completed",
            "completed": True, "priority": 10, "completed_time": "2030-01-01T12:00:00",
            "recurring": 1, "recurring_interval": 7, "_endpoint_root": "/recurring",
        },
    ]
    templates = create_templates()
    templates.env.globals.update(
        reactivation_date=repository.reactivation_date,
        recurrence_schedule_label=repository.recurrence_schedule_label,
        completion_day_policy=events.server_time_policy,
    )
    with patch.dict(os.environ, {"LUIGI_WEB_TIMEZONE": "UTC", "LUIGI_WEB_DAY_CUTOFF": "04:00"}), patch.object(
        repository, "get_engine", side_effect=AssertionError("Template tests must not access a database")
    ):
        return templates.get_template("tasks.html").render(
            page_title="Tasks", active_nav="tasks", endpoint_root="/tasks", statuses=STATUSES,
            rows=rows, columns={status: [row for row in rows if row["status"] == status] for status in STATUSES},
            module_enabled=lambda module: module == "tasks", navigation_groups=[],
            landing_path="/tasks", module_count=1, asset_version="test", shell_asset_version="test",
            filter_date="2030-01-02",
        )


class TaskViewsTemplateTests(unittest.TestCase):
    def test_exact_assets_and_board_default(self) -> None:
        markup = Markup(render_tasks())
        scripts = [attrs.get("src") for attrs in markup.matching("script")]
        styles = [attrs.get("href") for attrs in markup.matching("link", rel="stylesheet")]
        self.assertEqual(scripts.count("/module-assets/tasks/task-views.js?v=test"), 1)
        self.assertEqual(styles.count("/module-assets/tasks/task-views.css?v=test"), 1)
        self.assertIn("defer", markup.matching("script", src="/module-assets/tasks/task-views.js?v=test")[0])
        self.assertLess(scripts.index("/static/js/app.js?v=test"), scripts.index("/module-assets/tasks/task-views.js?v=test"))
        self.assertEqual(markup.hooked("data-task-views")[0]["data-tasks-scope"], "/tasks")
        self.assertEqual(markup.hooked("data-task-views")[0]["data-task-calendar-date"], "2030-01-02")
        self.assertEqual(markup.matching("button", **{"data-task-view": "board"})[0]["aria-pressed"], "true")
        self.assertEqual(markup.matching("button", **{"data-task-view": "list"})[0]["aria-pressed"], "false")
        self.assertNotIn("hidden", markup.matching("section", **{"data-view-panel": "board"})[0])
        self.assertIn("hidden", markup.matching("section", **{"data-view-panel": "list"})[0])
        self.assertEqual(len(markup.matching("a", href="/tasks/preview")), 1)
        self.assertFalse(any("task-examples" in str(url) for url in scripts + styles))

    def test_canonical_columns_survive_empty_board_and_collapse_controls(self) -> None:
        for empty in (False, True):
            with self.subTest(empty=empty):
                markup = Markup(render_tasks(empty=empty))
                self.assertEqual([attrs["data-status"] for attrs in markup.hooked("data-task-column")], list(STATUSES))
                self.assertEqual([attrs["data-shown-column"] for attrs in markup.hooked("data-shown-column")], list(STATUSES))
                self.assertTrue(all("checked" in attrs for attrs in markup.hooked("data-shown-column")))
                self.assertTrue(all("hidden" not in attrs for attrs in markup.hooked("data-task-column")))
                for index, button in enumerate(markup.hooked("data-column-collapse"), 1):
                    self.assertEqual(button["aria-expanded"], "true")
                    self.assertEqual(button["aria-controls"], f"task-column-body-{index}")
                    self.assertEqual(len(markup.matching("div", id=f"task-column-body-{index}")), 1)

    def test_board_and_list_datasets_normalize_booleans_and_qualify_sources(self) -> None:
        markup = Markup(render_tasks())
        records = markup.hooked("data-task-key")
        self.assertEqual(len(records), 4)
        for source, endpoint, completed in (("task", "/tasks", "0"), ("recurring", "/recurring", "1")):
            pair = [attrs for attrs in records if attrs["data-task-source"] == source]
            self.assertEqual(len(pair), 2)
            for attrs in pair:
                self.assertEqual(attrs["data-task-key"], source + ":example-shared-id")
                self.assertEqual(attrs["data-endpoint"], endpoint)
                self.assertEqual(attrs["data-completed"], completed)
                for field in ("title", "priority", "status", "catagory", "project", "task-group", "sub-group", "due-date", "completed-time", "reactivation-date"):
                    self.assertEqual(pair[0]["data-" + field], pair[1]["data-" + field])
            if source == "task":
                self.assertEqual(pair[0]["data-title"], "example <task>")
                self.assertEqual(pair[0]["data-priority"], "0")
            else:
                self.assertEqual(pair[0]["data-reactivation-date"], "2030-01-08")
        self.assertFalse(markup.matching("task"))

    def test_existing_write_targets_and_no_tasks_today_actions(self) -> None:
        source = render_tasks()
        markup = Markup(source)
        posts = {str(attrs["hx-post"]) for _, attrs in markup.elements if "hx-post" in attrs}
        expected = {"/tasks/quick"}
        for endpoint in ("/tasks", "/recurring"):
            expected.update(f"{endpoint}/example-shared-id/{action}" for action in ("status", "complete", "delete"))
        expected.update(("/tasks/example-shared-id/snooze", "/recurring/example-shared-id/archive"))
        self.assertEqual(posts, expected)
        for attrs in markup.matching("form"):
            self.assertIn(attrs.get("hx-post") or attrs.get("action"), {
                "/logout", "/tasks/quick", "/tasks/example-shared-id/complete",
            })
        for endpoint in ("/tasks", "/recurring"):
            self.assertTrue(markup.matching(**{"hx-get": f"{endpoint}/example-shared-id/edit"}))
        self.assertEqual(len(markup.matching("button", **{"hx-get": "/tasks/new"})), 1)
        self.assertTrue(markup.matching("button", **{"hx-get": "/follow-ups/panel"}))
        self.assertTrue(markup.matching("a", href="/task-rules"))
        self.assertTrue(markup.matching("a", href="/archive"))
        for forbidden in ("/home/today", "data-home-today", "data-task-today", "Add to Today", "Remove from Today"):
            self.assertNotIn(forbidden, source)
        self.assertFalse(any("today" in key or "today" in str(value).lower() for _, attrs in markup.elements for key, value in attrs.items()))

    def test_saved_view_and_filter_hooks_have_no_server_write_forms(self) -> None:
        markup = Markup(render_tasks())
        for hook in (
            "data-filter-search", "data-filter-smartlist", "data-filter-project", "data-filter-status",
            "data-filter-source", "data-filter-catagory", "data-filter-priority", "data-task-sort",
            "data-task-group-list", "data-task-density", "data-saved-views", "data-saved-views-list",
            "data-saved-view-save", "data-filter-clear", "data-view-reset", "data-task-views-notice",
            "data-collapse-completed", "data-task-view-empty",
        ):
            self.assertEqual(len(markup.hooked(hook)), 1, hook)
        name = markup.hooked("data-saved-view-name")[0]
        self.assertEqual(name["maxlength"], "40")
        self.assertNotIn("name", name)
        for hook in ("data-saved-view-save", "data-filter-clear", "data-view-reset"):
            button = markup.hooked(hook)[0]
            self.assertEqual(button["type"], "button")
            self.assertNotIn("hx-post", button)
        script = (TASKS_ROOT / "static" / "task-views.js").read_text(encoding="utf-8")
        for forbidden in ("fetch(", "XMLHttpRequest", "sendBeacon", "/home/today", "innerHTML", "insertAdjacentHTML"):
            self.assertNotIn(forbidden, script)
        self.assertIn("apply.textContent = entry.name", script)

    def test_core_legacy_guards_leave_views_and_columns_to_controller(self) -> None:
        source = (ROOT / "luigi_web" / "core" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        for function, guard in (
            ("setTaskView", 'if (!scope || scope.hasAttribute("data-task-views")) return;'),
            ("initTaskView", 'if (!scope || scope.hasAttribute("data-task-views")) return;'),
            ("initTasksFilter", 'if (!bar || bar.closest("[data-task-views]")) return;'),
        ):
            with self.subTest(function=function):
                match = re.search(r"function " + function + r"\([^)]*\)\s*\{([^\n]*\n){0,3}", source)
                self.assertIsNotNone(match)
                assert match is not None
                self.assertIn(guard, match.group())
        self.assertIn('if (!board?.closest("[data-task-views]")) pushEmptyChildrenToEnd(board, kanbanColumnIsEmpty);', source)


if __name__ == "__main__":
    unittest.main()