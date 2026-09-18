"""Offline module-contract checks using synthetic feature manifests."""
from __future__ import annotations

import sys
import unittest

from luigi_web.core.module_registry import (
    Module,
    ModuleConfigurationError,
    ModuleRegistry,
    NavigationItem,
)


def example_module(module_id: str, **extra) -> Module:
    return Module(
        id=module_id,
        label=module_id.title(),
        description="Synthetic test module",
        router=f"synthetic_{module_id}.routes:router",
        **extra,
    )


class ModuleRegistryTests(unittest.TestCase):
    def test_default_preserves_all_builtins(self) -> None:
        registry = ModuleRegistry([example_module("tasks"), example_module("cards")])
        self.assertEqual([module.id for module in registry.enabled], ["tasks", "cards"])

    def test_selection_never_imports_feature_code(self) -> None:
        registry = ModuleRegistry(
            [example_module("tasks"), example_module("cards")], "cards"
        )
        self.assertTrue(registry.is_enabled("cards"))
        self.assertFalse(registry.is_enabled("tasks"))
        self.assertNotIn("synthetic_tasks.routes", sys.modules)
        self.assertNotIn("synthetic_cards.routes", sys.modules)

    def test_none_is_an_explicit_core_only_configuration(self) -> None:
        registry = ModuleRegistry([example_module("tasks")], "none")
        self.assertEqual(registry.enabled, ())
        self.assertEqual(registry.navigation(), ())

    def test_unknown_duplicate_and_empty_selection_fail_closed(self) -> None:
        for selection in ("unknown", "tasks,tasks", "tasks,", ""):
            with self.subTest(selection=selection), self.assertRaises(ModuleConfigurationError):
                ModuleRegistry([example_module("tasks")], selection)

    def test_duplicate_module_ids_are_rejected(self) -> None:
        with self.assertRaises(ModuleConfigurationError):
            ModuleRegistry([example_module("tasks"), example_module("tasks")])

    def test_missing_dependency_is_not_silently_enabled(self) -> None:
        with self.assertRaisesRegex(ModuleConfigurationError, "requires tasks"):
            ModuleRegistry(
                [example_module("calendar", requires=("tasks",)), example_module("tasks")],
                "calendar",
            )

    def test_dependencies_load_first(self) -> None:
        registry = ModuleRegistry([
            example_module("calendar", requires=("tasks",)), example_module("tasks")
        ])
        self.assertEqual([module.id for module in registry.enabled], ["tasks", "calendar"])

    def test_dependency_cycles_are_rejected(self) -> None:
        with self.assertRaisesRegex(ModuleConfigurationError, "Circular"):
            ModuleRegistry([
                example_module("tasks", requires=("calendar",)),
                example_module("calendar", requires=("tasks",)),
            ])

    def test_navigation_comes_only_from_enabled_modules(self) -> None:
        registry = ModuleRegistry([
            example_module("tasks", navigation=(
                NavigationItem("tasks", "Tasks", "/tasks", "list-checks", "Focus"),
            )),
            example_module("cards", navigation=(
                NavigationItem("cards", "Trading Cards", "/cards", "layers", "Library"),
            )),
        ], "cards")
        self.assertEqual([group for group, _ in registry.navigation()], ["Library"])
        self.assertEqual(registry.navigation()[0][1][0].href, "/cards")

    def test_navigation_cannot_link_to_external_or_parameterized_urls(self) -> None:
        for href in ("https://example.invalid", "//example.invalid", "/tasks?q=private", "/\\host"):
            with self.subTest(href=href), self.assertRaises(ModuleConfigurationError):
                NavigationItem("tasks", "Tasks", href, "list-checks")

    def test_incompatible_api_versions_are_rejected(self) -> None:
        with self.assertRaises(ModuleConfigurationError):
            example_module("tasks", api_version=999)


if __name__ == "__main__":
    unittest.main()