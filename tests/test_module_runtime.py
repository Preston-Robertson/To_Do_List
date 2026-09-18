"""Offline runtime contracts with synthetic modules and temporary settings only."""
from __future__ import annotations

import importlib
import importlib.abc
import json
import os
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
from contextlib import ExitStack, contextmanager
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient
from jinja2 import TemplateNotFound

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SYSTEM_ENVIRONMENT = {
    key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ
}

with tempfile.TemporaryDirectory(prefix="luigi-module-import-") as import_directory:
    with mock.patch.dict(os.environ, {
        "LUIGI_WEB_MODULES": "none",
        "LUIGI_WEB_MODULES_FILE": str(Path(import_directory) / "modules.json"),
    }, clear=True):
        from luigi_web import auth
        from luigi_web.core import module_registry, module_settings, modules_routes, templating


def synthetic_module(module_id: str = "example", **overrides) -> module_registry.Module:
    values = {
        "id": module_id,
        "label": module_id.title(),
        "description": "Synthetic runtime fixture",
        "router": f"runtime_test_{module_id}:router",
    }
    values.update(overrides)
    return module_registry.Module(**values)


def synthetic_entry_point(module_id: str = "example", **overrides) -> mock.Mock:
    entry = mock.Mock(spec=["name", "load"])
    entry.name = module_id
    entry.load.return_value = synthetic_module(
        module_id, package=f"runtime_test_{module_id}", **overrides,
    )
    return entry


def synthetic_router(path: str, *, methods: tuple[str, ...] = ("GET",)) -> APIRouter:
    router = APIRouter()

    async def endpoint():
        return {"status": "synthetic"}

    router.add_api_route(path, endpoint, methods=list(methods))
    return router


def small_app() -> FastAPI:
    return FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def clean_environment(directory: Path) -> dict[str, str]:
    return {
        **_SYSTEM_ENVIRONMENT,
        "LUIGI_WEB_MODULES": "none",
        "LUIGI_WEB_MODULES_FILE": str(directory / "modules.json"),
        "LUIGI_WEB_UI_TOKEN": "synthetic-runtime-session",
        "LUIGI_WEB_TASK_METADATA_FILE": str(directory / "task-metadata.json"),
        **{key: str(directory) for key in (
            "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        )},
    }


@contextmanager
def fake_references(references):
    with (
        mock.patch.object(module_registry, "_load_reference", side_effect=references.__getitem__) as load,
        mock.patch.object(templating, "configure_module_templates"),
        mock.patch.object(templating, "mount_module_assets"),
    ):
        yield load


class ShellMarkup(HTMLParser):
    def __init__(self, markup: str) -> None:
        super().__init__()
        self.targets: set[str] = set()
        self.controls: list[dict[str, str | None]] = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs) -> None:
        attributes = dict(attrs)
        for attribute in ("href", "hx-get", "hx-post"):
            target = attributes.get(attribute)
            if target and target.startswith("/"):
                self.targets.add(target)
        if tag in {"input", "button", "form"}:
            self.controls.append({"tag": tag, **attributes})


class IsolatedRuntimeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="luigi-module-runtime-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.settings_file = self.directory / "modules.json"
        self.enterContext(mock.patch.dict(os.environ, clean_environment(self.directory), clear=True))

    def use_saved_selection(self) -> None:
        os.environ.pop("LUIGI_WEB_MODULES", None)

    def authenticated_client(self, application: FastAPI) -> TestClient:
        client = self.enterContext(TestClient(application, follow_redirects=False))
        client.headers["Authorization"] = "Bearer " + os.environ["LUIGI_WEB_UI_TOKEN"]
        return client


class ExternalDiscoveryTests(IsolatedRuntimeTestCase):
    def test_installed_entry_point_is_not_discovered_or_loaded_without_allowlist(self) -> None:
        entry = synthetic_entry_point()
        with mock.patch.object(module_registry.metadata, "entry_points", return_value=[entry]) as discover:
            catalog = module_registry.discover_modules()
        self.assertEqual(tuple(module.id for module in catalog), module_registry.BUILTIN_MODULE_IDS)
        discover.assert_not_called()
        entry.load.assert_not_called()

    def test_allowlist_and_explicit_selection_load_valid_external_manifest(self) -> None:
        entry = synthetic_entry_point()
        os.environ["LUIGI_WEB_EXTERNAL_MODULES"] = "example"
        os.environ["LUIGI_WEB_MODULES"] = "example"
        with mock.patch.object(module_registry.metadata, "entry_points", return_value=[entry]) as discover:
            registry = module_registry.build_registry()
        discover.assert_called_once_with(group=module_registry.ENTRY_POINT_GROUP)
        entry.load.assert_called_once_with()
        self.assertEqual(tuple(module.id for module in registry.enabled), ("example",))
        self.assertEqual(registry.enabled[0].source, "External package")
        self.assertEqual(registry.enabled[0].package, "runtime_test_example")

    def test_only_allowlisted_entry_points_are_loaded(self) -> None:
        approved = synthetic_entry_point()
        unapproved = synthetic_entry_point("unapproved")
        os.environ["LUIGI_WEB_EXTERNAL_MODULES"] = "example"
        with mock.patch.object(module_registry.metadata, "entry_points", return_value=[unapproved, approved]):
            module_registry.build_registry("example")
        approved.load.assert_called_once_with()
        unapproved.load.assert_not_called()

    def test_default_keeps_every_builtin_when_no_config_exists(self) -> None:
        self.use_saved_selection()
        registry = module_registry.build_registry()
        self.assertEqual(
            {module.id for module in registry.enabled}, set(module_registry.BUILTIN_MODULE_IDS),
        )
        self.assertFalse(self.settings_file.exists())

    def test_installed_external_package_does_not_join_default_selection(self) -> None:
        self.use_saved_selection()
        entry = synthetic_entry_point()
        with mock.patch.object(module_registry.metadata, "entry_points", return_value=[entry]):
            registry = module_registry.build_registry()
        self.assertFalse(registry.is_enabled("example"))
        entry.load.assert_not_called()

    def test_allowlisted_external_package_still_requires_enabled_selection(self) -> None:
        self.use_saved_selection()
        entry = synthetic_entry_point()
        os.environ["LUIGI_WEB_EXTERNAL_MODULES"] = "example"
        with mock.patch.object(module_registry.metadata, "entry_points", return_value=[entry]):
            registry = module_registry.build_registry()
        self.assertIn("example", {module.id for module in registry.catalog})
        self.assertFalse(registry.is_enabled("example"))
        self.assertEqual(
            {module.id for module in registry.enabled}, set(module_registry.BUILTIN_MODULE_IDS),
        )

    def test_unknown_allowlisted_entry_point_fails_closed(self) -> None:
        os.environ["LUIGI_WEB_EXTERNAL_MODULES"] = "missing"
        with mock.patch.object(module_registry.metadata, "entry_points", return_value=[]):
            with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "Expected one installed entry point"):
                module_registry.discover_modules()

    def test_unapproved_external_id_cannot_be_selected(self) -> None:
        entry = synthetic_entry_point()
        with mock.patch.object(module_registry.metadata, "entry_points", return_value=[entry]):
            with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "Unknown modules"):
                module_registry.build_registry("example")
        entry.load.assert_not_called()

    def test_duplicate_allowlist_is_rejected_before_discovery(self) -> None:
        os.environ["LUIGI_WEB_EXTERNAL_MODULES"] = "example,example"
        with mock.patch.object(module_registry.metadata, "entry_points") as discover:
            with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "allow-list"):
                module_registry.discover_modules()
        discover.assert_not_called()

    def test_duplicate_installed_entry_point_names_are_not_loaded(self) -> None:
        entries = [synthetic_entry_point(), synthetic_entry_point()]
        os.environ["LUIGI_WEB_EXTERNAL_MODULES"] = "example"
        with mock.patch.object(module_registry.metadata, "entry_points", return_value=entries):
            with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "Expected one installed entry point"):
                module_registry.discover_modules()
        for entry in entries:
            entry.load.assert_not_called()

    def test_external_id_cannot_replace_builtin(self) -> None:
        entry = synthetic_entry_point("tasks")
        os.environ["LUIGI_WEB_EXTERNAL_MODULES"] = "tasks"
        with mock.patch.object(module_registry.metadata, "entry_points", return_value=[entry]):
            with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "Duplicate module ID"):
                module_registry.build_registry("none")

    def test_external_manifest_must_match_entry_point_and_declare_package(self) -> None:
        invalid = (
            object(),
            synthetic_module("different", package="runtime_test_different"),
            synthetic_module(),
        )
        os.environ["LUIGI_WEB_EXTERNAL_MODULES"] = "example"
        for index, manifest in enumerate(invalid):
            with self.subTest(case=index):
                entry = synthetic_entry_point()
                entry.load.return_value = manifest
                with mock.patch.object(module_registry.metadata, "entry_points", return_value=[entry]):
                    with self.assertRaises(module_registry.ModuleConfigurationError):
                        module_registry.discover_modules()

    def test_incompatible_external_api_is_rejected_without_loader_details(self) -> None:
        entry = synthetic_entry_point()
        entry.load.side_effect = lambda: synthetic_module(
            api_version=module_registry.MODULE_API_VERSION + 1, package="runtime_test_example",
        )
        os.environ["LUIGI_WEB_EXTERNAL_MODULES"] = "example"
        with mock.patch.object(module_registry.metadata, "entry_points", return_value=[entry]):
            with self.assertRaises(module_registry.ModuleConfigurationError) as caught:
                module_registry.discover_modules()
        self.assertEqual(str(caught.exception), "Unable to load approved module example")
        self.assertTrue(caught.exception.__suppress_context__)


class ModuleSettingsTests(IsolatedRuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_saved_selection()
        self.catalog = (
            synthetic_module("dependent", requires=("foundation",)),
            synthetic_module("foundation"),
            synthetic_module("unused"),
        )
        self.discover = self.enterContext(mock.patch.object(
            module_registry, "discover_modules", return_value=self.catalog,
        ))

    def test_missing_settings_are_read_without_creating_storage(self) -> None:
        self.assertIsNone(module_settings.read_selection())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_save_round_trip_orders_dependencies_and_supports_none(self) -> None:
        module_settings.save_selection(["dependent", "foundation"])
        self.assertEqual(module_settings.read_selection(), "foundation,dependent")
        self.assertEqual(json.loads(self.settings_file.read_text(encoding="utf-8")), {
            "version": 1, "enabled": ["foundation", "dependent"],
        })
        self.assertEqual(
            tuple(module.id for module in module_registry.build_registry().enabled),
            ("foundation", "dependent"),
        )
        module_settings.save_selection([])
        self.assertEqual(module_settings.read_selection(), "none")
        self.assertEqual(module_registry.build_registry().enabled, ())

    def test_atomic_replace_uses_flushed_sibling_and_preserves_previous_until_commit(self) -> None:
        module_settings.save_selection(["foundation"])
        previous = self.settings_file.read_bytes()
        replace = os.replace
        fsync = os.fsync

        def verify_replace(source, destination):
            self.assertEqual(Path(source).parent, self.settings_file.parent)
            self.assertNotEqual(Path(source), Path(destination))
            self.assertEqual(self.settings_file.read_bytes(), previous)
            self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8"))["enabled"], ["unused"])
            flush.assert_called_once()
            replace(source, destination)

        with (
            mock.patch.object(module_settings.os, "fsync", wraps=fsync) as flush,
            mock.patch.object(module_settings.os, "replace", side_effect=verify_replace) as commit,
        ):
            module_settings.save_selection(["unused"])
        commit.assert_called_once()
        self.assertEqual(module_settings.read_selection(), "unused")
        self.assertEqual(list(self.directory.iterdir()), [self.settings_file])

    def test_failed_replace_leaves_old_selection_and_removes_temporary_file(self) -> None:
        module_settings.save_selection(["foundation"])
        previous = self.settings_file.read_bytes()
        with mock.patch.object(module_settings.os, "replace", side_effect=OSError("synthetic private detail")):
            with self.assertRaises(module_registry.ModuleConfigurationError) as caught:
                module_settings.save_selection(["unused"])
        self.assertEqual(str(caught.exception), "Module settings could not be saved")
        self.assertEqual(self.settings_file.read_bytes(), previous)
        self.assertEqual(list(self.directory.iterdir()), [self.settings_file])

    def test_failed_fsync_never_replaces_or_leaves_a_temporary_file(self) -> None:
        module_settings.save_selection(["foundation"])
        previous = self.settings_file.read_bytes()
        with (
            mock.patch.object(module_settings.os, "fsync", side_effect=OSError("synthetic storage failure")),
            mock.patch.object(module_settings.os, "replace") as replace,
        ):
            with self.assertRaises(module_registry.ModuleConfigurationError):
                module_settings.save_selection(["unused"])
        replace.assert_not_called()
        self.assertEqual(self.settings_file.read_bytes(), previous)
        self.assertEqual(list(self.directory.iterdir()), [self.settings_file])

    def test_silent_failed_replace_cannot_report_a_successful_save(self) -> None:
        module_settings.save_selection(["foundation"])
        with mock.patch.object(module_settings.os, "replace", return_value=None):
            with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "could not be saved"):
                module_settings.save_selection(["unused"])
        self.assertEqual(module_settings.read_selection(), "foundation")
        self.assertEqual(list(self.directory.iterdir()), [self.settings_file])

    def test_environment_managed_selection_rejects_edits_even_when_none_or_empty(self) -> None:
        for selection in ("none", "", "foundation"):
            with self.subTest(selection=selection):
                os.environ["LUIGI_WEB_MODULES"] = selection
                with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "managed"):
                    module_settings.save_selection([])
        self.discover.assert_not_called()
        self.assertFalse(self.settings_file.exists())

    def test_unknown_duplicate_and_unmet_requires_are_not_saved(self) -> None:
        module_settings.save_selection(["foundation"])
        previous = self.settings_file.read_bytes()
        for enabled in (["unknown"], ["foundation", "foundation"], ["dependent"]):
            with self.subTest(enabled=enabled):
                with self.assertRaises(module_registry.ModuleConfigurationError):
                    module_settings.save_selection(enabled)
                self.assertEqual(self.settings_file.read_bytes(), previous)
                self.assertEqual(list(self.directory.iterdir()), [self.settings_file])

    def test_malformed_settings_fail_closed_without_raw_payload_details(self) -> None:
        invalid = (
            b"{", b"[]", b"null", b"\xff",
            b'{"version":2,"enabled":[]}',
            b'{"version":1}',
            b'{"version":1,"enabled":"foundation"}',
            b'{"version":1,"enabled":[123]}',
            b" " * 16385,
        )
        for index, content in enumerate(invalid):
            with self.subTest(case=index):
                self.settings_file.write_bytes(content)
                with self.assertRaises(module_registry.ModuleConfigurationError) as caught:
                    module_settings.read_selection()
                self.assertEqual(str(caught.exception), "Module settings are invalid")

    def test_invalid_saved_ids_and_dependencies_are_rejected_when_building_registry(self) -> None:
        for enabled in (["unknown"], ["foundation", "foundation"], ["dependent"], [""]):
            with self.subTest(enabled=enabled):
                self.settings_file.write_text(json.dumps({"version": 1, "enabled": enabled}), encoding="utf-8")
                with self.assertRaises(module_registry.ModuleConfigurationError):
                    module_registry.build_registry()

    def test_explicit_selection_overrides_environment_and_environment_overrides_saved(self) -> None:
        module_settings.save_selection(["foundation"])
        os.environ["LUIGI_WEB_MODULES"] = "none"
        self.assertEqual(module_registry.build_registry().enabled, ())
        self.assertEqual(
            tuple(module.id for module in module_registry.build_registry("unused").enabled),
            ("unused",),
        )

    def test_unreadable_settings_hide_storage_details(self) -> None:
        with mock.patch.object(Path, "open", side_effect=PermissionError("synthetic inaccessible path")):
            with self.assertRaises(module_registry.ModuleConfigurationError) as caught:
                module_settings.read_selection()
        self.assertEqual(str(caught.exception), "Module settings are not readable")


class ModuleMountTests(IsolatedRuntimeTestCase):
    def test_only_selected_router_references_are_loaded_and_mounted(self) -> None:
        selected = synthetic_module()
        disabled = synthetic_module("disabled")
        registry = module_registry.ModuleRegistry([disabled, selected], "example")
        application = small_app()
        with fake_references({selected.router: synthetic_router("/example")}) as load:
            module_registry.mount_modules(application, registry)
        load.assert_called_once_with(selected.router)
        self.assertEqual({route.path for route in application.routes}, {"/example"})
        self.assertIs(application.state.modules, registry)
        client = self.authenticated_client(application)
        self.assertEqual(client.get("/example").status_code, 200)
        self.assertEqual(client.get("/disabled").status_code, 404)

    def test_external_router_without_own_dependencies_is_forced_to_authenticate(self) -> None:
        module = synthetic_module(source="External package", package="runtime_test_example")
        application = small_app()
        with fake_references({module.router: synthetic_router("/extensions/example")}):
            module_registry.mount_modules(application, module_registry.ModuleRegistry([module]))
        client = self.enterContext(TestClient(application, follow_redirects=False))
        self.assertEqual(client.get("/extensions/example").status_code, 401)
        response = client.get("/extensions/example", headers={"Accept": "text/html"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")
        client.headers["Authorization"] = "Bearer " + os.environ["LUIGI_WEB_UI_TOKEN"]
        self.assertEqual(client.get("/extensions/example").status_code, 200)

    def test_reserved_paths_and_their_descendants_are_rejected(self) -> None:
        reserved = (
            "/", "/login", "/logout", "/healthz", "/static", "/modules",
            "/module-assets", "/command-palette",
        )
        for path in (*reserved, *(prefix + "/nested" for prefix in reserved if prefix != "/")):
            with self.subTest(path=path):
                module = synthetic_module()
                application = small_app()
                with fake_references({module.router: synthetic_router(path)}):
                    with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "reserved"):
                        module_registry.mount_modules(application, module_registry.ModuleRegistry([module]))
                self.assertEqual(application.routes, [])

    def test_external_routes_cannot_escape_their_exact_namespace(self) -> None:
        module = synthetic_module(source="External package", package="runtime_test_example")
        for path in ("/example", "/extensions/other", "/extensions/example-other", "/extensions"):
            with self.subTest(path=path), fake_references({module.router: synthetic_router(path)}):
                with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "namespace"):
                    module_registry.mount_modules(small_app(), module_registry.ModuleRegistry([module]))

    def test_external_namespace_root_and_children_are_allowed(self) -> None:
        module = synthetic_module(source="External package", package="runtime_test_example")
        for path in ("/extensions/example", "/extensions/example/items/{item_id}"):
            with self.subTest(path=path):
                application = small_app()
                with fake_references({module.router: synthetic_router(path)}):
                    module_registry.mount_modules(application, module_registry.ModuleRegistry([module]))
                self.assertEqual({route.path for route in application.routes}, {path})

    def test_duplicate_parameter_patterns_are_rejected_within_one_router(self) -> None:
        module = synthetic_module()
        router = synthetic_router("/example/{first_id}")
        router.include_router(synthetic_router("/example/{second_id}"))
        with fake_references({module.router: router}):
            with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "Conflicting route"):
                module_registry.mount_modules(small_app(), module_registry.ModuleRegistry([module]))

    def test_conflicts_with_core_and_other_modules_do_not_partially_mount(self) -> None:
        first = synthetic_module("first")
        second = synthetic_module("second")
        conflicts = (
            ("/shared/{first_id}", "/shared/{second_id}"),
            ("/shared/{item_id}", "/shared/fixed"),
            ("/shared/fixed", "/shared/{item_id}"),
        )
        for existing, incoming in conflicts:
            for owner in ("core", "module"):
                with self.subTest(existing=existing, incoming=incoming, owner=owner):
                    application = small_app()
                    modules = [second]
                    references = {second.router: synthetic_router(incoming)}
                    if owner == "core":
                        application.include_router(synthetic_router(existing))
                    else:
                        modules.insert(0, first)
                        references[first.router] = synthetic_router(existing)
                    previous = list(application.routes)
                    with fake_references(references):
                        with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "Conflicting route"):
                            module_registry.mount_modules(application, module_registry.ModuleRegistry(modules))
                    self.assertEqual(application.routes, previous)

    def test_same_path_with_disjoint_methods_is_allowed(self) -> None:
        first, second = synthetic_module("first"), synthetic_module("second")
        references = {
            first.router: synthetic_router("/shared"),
            second.router: synthetic_router("/shared", methods=("POST",)),
        }
        application = small_app()
        with fake_references(references):
            module_registry.mount_modules(application, module_registry.ModuleRegistry([first, second]))
        self.assertEqual(len(application.routes), 2)

    def test_import_failure_hides_exception_details(self) -> None:
        with mock.patch.object(module_registry, "_load_reference", side_effect=RuntimeError("synthetic private detail")):
            with self.assertRaises(module_registry.ModuleConfigurationError) as caught:
                module_registry.mount_modules(small_app(), module_registry.ModuleRegistry([synthetic_module()]))
        self.assertEqual(str(caught.exception), "Unable to load routes for example")
        self.assertTrue(caught.exception.__suppress_context__)

    def test_invalid_router_and_router_owned_hooks_are_rejected(self) -> None:
        module = synthetic_module()
        with_startup = synthetic_router("/example")
        with_startup.on_startup.append(mock.Mock())
        with_shutdown = synthetic_router("/example")
        with_shutdown.on_shutdown.append(mock.Mock())
        for index, router in enumerate((object(), with_startup, with_shutdown)):
            with self.subTest(case=index), fake_references({module.router: router}):
                with self.assertRaises(module_registry.ModuleConfigurationError):
                    module_registry.mount_modules(small_app(), module_registry.ModuleRegistry([module]))


class ModuleLifecycleTests(IsolatedRuntimeTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_sync_async_and_returned_awaitable_hooks_start_in_order_and_stop_in_reverse(self) -> None:
        events = []
        loop_thread = threading.get_ident()
        application = small_app()

        def sync_start(received):
            self.assertIs(received, application)
            self.assertNotEqual(threading.get_ident(), loop_thread)
            events.append("foundation:start")

        async def async_start(received):
            self.assertIs(received, application)
            self.assertEqual(threading.get_ident(), loop_thread)
            events.append("middle:start")

        async def deferred_start(received):
            self.assertIs(received, application)
            events.append("leaf:start")

        def sync_stop(received):
            self.assertIs(received, application)
            events.append("foundation:stop")

        async def async_stop(received):
            self.assertIs(received, application)
            events.append("middle:stop")

        async def deferred_stop(received):
            self.assertIs(received, application)
            events.append("leaf:stop")

        modules = [
            synthetic_module("leaf", requires=("middle",), startup="runtime_test_leaf:start", shutdown="runtime_test_leaf:stop"),
            synthetic_module("middle", requires=("foundation",), startup="runtime_test_middle:start", shutdown="runtime_test_middle:stop"),
            synthetic_module("foundation", startup="runtime_test_foundation:start", shutdown="runtime_test_foundation:stop"),
        ]
        references = {module.router: synthetic_router("/" + module.id) for module in modules}
        references.update({
            "runtime_test_foundation:start": sync_start,
            "runtime_test_foundation:stop": sync_stop,
            "runtime_test_middle:start": async_start,
            "runtime_test_middle:stop": async_stop,
            "runtime_test_leaf:start": lambda received: deferred_start(received),
            "runtime_test_leaf:stop": lambda received: deferred_stop(received),
        })
        with fake_references(references):
            module_registry.mount_modules(application, module_registry.ModuleRegistry(modules))
            await module_registry.start_modules(application)
            self.assertEqual(application.state.module_status, {
                "foundation": "ready", "middle": "ready", "leaf": "ready",
            })
            await module_registry.stop_modules(application)
            await module_registry.stop_modules(application)
        self.assertEqual(events, [
            "foundation:start", "middle:start", "leaf:start", "leaf:stop", "middle:stop", "foundation:stop",
        ])
        self.assertEqual(application.state.module_cleanup, [])

    async def test_disabled_lifecycle_hooks_are_never_resolved(self) -> None:
        module = synthetic_module(startup="runtime_test_example:start", shutdown="runtime_test_example:stop")
        application = small_app()
        with fake_references({}) as load:
            module_registry.mount_modules(application, module_registry.ModuleRegistry([module], "none"))
            await module_registry.start_modules(application)
            await module_registry.stop_modules(application)
        load.assert_not_called()
        self.assertEqual(application.state.module_status, {})

    async def test_failed_start_is_isolated_dependents_blocked_and_errors_are_private(self) -> None:
        events = []
        private_marker = "synthetic-lifecycle-private-marker"
        application = small_app()

        def fail_start(received):
            events.append("broken:start")
            raise RuntimeError(private_marker)

        async def healthy_start(received):
            events.append("healthy:start")

        def broken_stop(received):
            events.append("broken:stop")

        async def healthy_stop(received):
            events.append("healthy:stop")
            raise RuntimeError(private_marker)

        modules = [
            synthetic_module("dependent", requires=("broken",), startup="runtime_test_dependent:start", shutdown="runtime_test_dependent:stop"),
            synthetic_module("transitive", requires=("dependent",), startup="runtime_test_transitive:start"),
            synthetic_module("broken", startup="runtime_test_broken:start", shutdown="runtime_test_broken:stop"),
            synthetic_module("healthy", startup="runtime_test_healthy:start", shutdown="runtime_test_healthy:stop"),
        ]
        references = {module.router: synthetic_router("/" + module.id) for module in modules}
        references.update({
            "runtime_test_broken:start": fail_start,
            "runtime_test_broken:stop": broken_stop,
            "runtime_test_healthy:start": healthy_start,
            "runtime_test_healthy:stop": healthy_stop,
        })
        with fake_references(references) as load, self.assertLogs("luigi_web.modules", level="ERROR") as logs:
            module_registry.mount_modules(application, module_registry.ModuleRegistry(modules))
            await module_registry.start_modules(application)
            self.assertEqual(application.state.module_status, {
                "broken": "unavailable", "dependent": "blocked", "transitive": "blocked", "healthy": "ready",
            })
            with TestClient(application) as client:
                client.headers["Authorization"] = "Bearer " + os.environ["LUIGI_WEB_UI_TOKEN"]
                for module_id in ("broken", "dependent", "transitive"):
                    response = client.get("/" + module_id)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json(), {"detail": "Module is unavailable"})
                self.assertEqual(client.get("/healthy").status_code, 200)
            await module_registry.stop_modules(application)
        self.assertEqual(events, ["broken:start", "healthy:start", "healthy:stop", "broken:stop"])
        self.assertEqual(application.state.module_cleanup, [])
        self.assertNotIn(mock.call("runtime_test_dependent:start"), load.call_args_list)
        self.assertNotIn(mock.call("runtime_test_transitive:start"), load.call_args_list)
        self.assertFalse(private_marker in "\n".join(logs.output), "Lifecycle logs exposed exception details")
        self.assertEqual(len(logs.records), 2)
        self.assertTrue(all(record.exc_info is None and record.stack_info is None for record in logs.records))


class ModuleTemplatingTests(IsolatedRuntimeTestCase):
    def test_shell_context_contains_only_enabled_navigation_and_one_modules_link(self) -> None:
        selected = synthetic_module(navigation=(
            module_registry.NavigationItem("example", "Example", "/extensions/example", "blocks", "System"),
        ))
        disabled = synthetic_module("disabled", navigation=(
            module_registry.NavigationItem("disabled", "Disabled", "/disabled", "blocks", "Library"),
        ))
        application = small_app()
        application.state.modules = module_registry.ModuleRegistry([disabled, selected], "example")
        request = Request({"type": "http", "app": application})
        for _ in range(2):
            context = templating.shell_context(request)
            self.assertEqual(context["module_count"], 1)
            self.assertEqual(context["landing_path"], "/extensions/example")
            self.assertFalse(context["module_enabled"]("disabled"))
            self.assertEqual(
                [item.href for _, items in context["navigation_groups"] for item in items],
                ["/modules", "/extensions/example"],
            )

    def render_shell(self, registry: module_registry.ModuleRegistry) -> ShellMarkup:
        application = small_app()
        application.state.modules = registry
        templates = templating.create_templates()

        @application.get("/shell")
        def shell(request: Request):
            return templates.TemplateResponse("base.html", {
                "request": request, "page_title": "Synthetic shell", "active_nav": "modules",
            })

        with TestClient(application) as client:
            response = client.get("/shell")
        self.assertEqual(response.status_code, 200)
        return ShellMarkup(response.text)

    def test_rendered_core_only_shell_has_no_disabled_links_or_background_requests(self) -> None:
        registry = module_registry.build_registry("none")
        markup = self.render_shell(registry)
        disabled = {item.href for module in registry.catalog for item in module.navigation}
        disabled.update({"/reminders", "/reminders/count", "/feedback/new", "/home", "/chat/send"})
        self.assertEqual(sorted(markup.targets.intersection(disabled)), [])

    def test_rendered_shell_includes_module_manager_and_selected_external_navigation(self) -> None:
        module = synthetic_module(navigation=(
            module_registry.NavigationItem("example", "Example", "/extensions/example", "blocks"),
        ))
        markup = self.render_shell(module_registry.ModuleRegistry([module]))
        self.assertEqual({"/modules", "/extensions/example"}.difference(markup.targets), set())

    def test_external_templates_require_namespace_and_do_not_shadow_shared_shell(self) -> None:
        package = self.directory / "package"
        directory = package / "templates"
        directory.mkdir(parents=True)
        (directory / "runtime_entry.html").write_text("{{ label }}", encoding="utf-8")
        (directory / "base.html").write_text("Synthetic external shell", encoding="utf-8")
        with mock.patch.object(templating.resources, "files", return_value=package):
            with self.assertRaisesRegex(ValueError, "namespace"):
                templating.create_templates(package="runtime_test_example")
            templates = templating.create_templates(package="runtime_test_example", namespace="example")
        self.assertEqual(templates.env.get_template("example/runtime_entry.html").render(label="Synthetic"), "Synthetic")
        self.assertEqual(templates.env.get_template("example/base.html").render(), "Synthetic external shell")
        self.assertNotEqual(Path(templates.env.get_template("base.html").filename), directory / "base.html")
        with self.assertRaises(TemplateNotFound):
            templates.env.get_template("runtime_entry.html")

    def test_only_enabled_template_setup_references_are_called(self) -> None:
        templates = templating.create_templates()
        setup = mock.Mock()
        selected = synthetic_module(template_setup="runtime_test_example:configure")
        disabled = synthetic_module("disabled", template_setup="runtime_test_disabled:configure")
        registry = module_registry.ModuleRegistry([disabled, selected], "example")
        with (
            mock.patch.object(templating, "_TEMPLATES", [templates]),
            mock.patch.object(templating, "import_module", return_value=SimpleNamespace(configure=setup)) as imported,
        ):
            templating.configure_module_templates(registry)
        imported.assert_called_once_with("runtime_test_example")
        setup.assert_called_once_with(templates.env)


class ModuleManagerTests(IsolatedRuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.catalog = (
            synthetic_module("foundation"),
            synthetic_module("dependent", requires=("foundation",)),
            synthetic_module("unused"),
        )
        self.enterContext(mock.patch.object(module_registry, "discover_modules", return_value=self.catalog))

    def manager_app(self, selection: str = "none") -> FastAPI:
        application = small_app()
        application.include_router(modules_routes.router, dependencies=[Depends(auth.require_auth)])
        references = {module.router: synthetic_router("/" + module.id) for module in self.catalog}
        with fake_references(references):
            module_registry.mount_modules(application, module_registry.ModuleRegistry(self.catalog, selection))
        return application

    def test_manager_get_and_post_require_authentication_before_saving(self) -> None:
        application = self.manager_app()
        client = self.enterContext(TestClient(application, follow_redirects=False))
        with mock.patch.object(modules_routes, "save_selection") as save:
            self.assertEqual(client.get("/modules").status_code, 401)
            browser = client.get("/modules", headers={"Accept": "text/html"})
            self.assertEqual(browser.status_code, 303)
            self.assertEqual(browser.headers["location"], "/login")
            self.assertEqual(client.post("/modules", data={"enabled": "foundation"}).status_code, 401)
        save.assert_not_called()
        self.assertFalse(self.settings_file.exists())

    def test_authenticated_manager_renders_synthetic_catalog_with_no_store(self) -> None:
        self.use_saved_selection()
        client = self.authenticated_client(self.manager_app("foundation"))
        response = client.get("/modules")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.context["enabled_ids"], {"foundation"})
        self.assertEqual(response.context["selected_ids"], {"foundation"})
        markup = ShellMarkup(response.text)
        options = [control for control in markup.controls if control.get("name") == "enabled"]
        self.assertEqual({control["value"] for control in options}, {module.id for module in self.catalog})
        self.assertEqual({control["value"] for control in options if "checked" in control}, {"foundation"})

    def test_environment_managed_form_is_disabled_and_forged_edits_are_rejected(self) -> None:
        client = self.authenticated_client(self.manager_app())
        response = client.get("/modules")
        self.assertTrue(response.context["environment_managed"])
        controls = ShellMarkup(response.text).controls
        checkboxes = [control for control in controls if control.get("name") == "enabled"]
        save_buttons = [control for control in controls if control["tag"] == "button" and control.get("type") == "submit"]
        self.assertTrue(checkboxes)
        self.assertTrue(all("disabled" in control for control in checkboxes))
        self.assertTrue(any("disabled" in control for control in save_buttons))
        response = client.post("/modules", data={"enabled": "foundation"})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(response.context["saved"])
        self.assertFalse(self.settings_file.exists())

    def test_selection_changes_only_after_restart_not_in_save_or_following_requests(self) -> None:
        self.use_saved_selection()
        module_settings.save_selection(["foundation"])
        application = self.manager_app(module_settings.read_selection())
        original_registry = application.state.modules
        original_routes = list(application.routes)
        client = self.authenticated_client(application)
        with mock.patch.object(module_registry, "_load_reference") as load:
            response = client.post("/modules", data={"enabled": "unused"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["saved"])
            self.assertTrue(response.context["pending_restart"])
            self.assertEqual(response.context["enabled_ids"], {"foundation"})
            self.assertEqual(response.context["selected_ids"], {"unused"})
            self.assertIn("Restart required", response.text)
            self.assertEqual(module_settings.read_selection(), "unused")
            self.assertIs(application.state.modules, original_registry)
            self.assertEqual(application.routes, original_routes)
            self.assertEqual(client.get("/foundation").status_code, 200)
            self.assertEqual(client.get("/unused").status_code, 404)
            following = client.get("/modules")
            self.assertTrue(following.context["pending_restart"])
            self.assertFalse(following.context["saved"])
            self.assertEqual(following.context["enabled_ids"], {"foundation"})
        load.assert_not_called()
        restarted_registry = module_registry.build_registry()
        restarted = self.manager_app(",".join(module.id for module in restarted_registry.enabled))
        restarted_client = self.authenticated_client(restarted)
        self.assertEqual(restarted_client.get("/unused").status_code, 200)
        self.assertEqual(restarted_client.get("/foundation").status_code, 404)
        self.assertFalse(restarted_client.get("/modules").context["pending_restart"])

    def test_empty_form_saves_explicit_none_but_keeps_current_routes_until_restart(self) -> None:
        self.use_saved_selection()
        application = self.manager_app("foundation")
        client = self.authenticated_client(application)
        response = client.post("/modules", data={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["saved"])
        self.assertEqual(module_settings.read_selection(), "none")
        self.assertEqual(response.context["selected_ids"], set())
        self.assertEqual(response.context["enabled_ids"], {"foundation"})
        self.assertEqual(client.get("/foundation").status_code, 200)

    def test_invalid_selection_is_not_saved_and_never_shows_success(self) -> None:
        self.use_saved_selection()
        module_settings.save_selection(["foundation"])
        previous = self.settings_file.read_bytes()
        client = self.authenticated_client(self.manager_app("foundation"))
        for enabled in (["unknown"], ["dependent"], ["foundation", "foundation"]):
            with self.subTest(enabled=enabled):
                response = client.post("/modules", data={"enabled": enabled})
                self.assertEqual(response.status_code, 422)
                self.assertFalse(response.context["saved"])
                self.assertNotIn("Selection saved", response.text)
                self.assertEqual(self.settings_file.read_bytes(), previous)

    def test_failed_save_and_failed_verification_return_error_not_false_success(self) -> None:
        self.use_saved_selection()
        module_settings.save_selection(["foundation"])
        client = self.authenticated_client(self.manager_app("foundation"))
        for failure in (OSError("synthetic-hidden-storage-detail"), None):
            with self.subTest(case="replace error" if failure else "silent replace failure"):
                with mock.patch.object(module_settings.os, "replace", side_effect=failure):
                    response = client.post("/modules", data={"enabled": "unused"})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertFalse(response.context["saved"])
                self.assertEqual(response.context["error"], "Module settings could not be saved")
                self.assertFalse("synthetic-hidden-storage-detail" in response.text)
                self.assertNotIn("Selection saved", response.text)
                self.assertEqual(module_settings.read_selection(), "foundation")
                self.assertEqual(list(self.directory.iterdir()), [self.settings_file])

    def test_invalid_saved_config_renders_generic_error_and_current_selection(self) -> None:
        self.use_saved_selection()
        self.settings_file.write_text("synthetic-invalid-private-payload", encoding="utf-8")
        client = self.authenticated_client(self.manager_app("foundation"))
        response = client.get("/modules")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["error"], "Saved module selection is invalid")
        self.assertEqual(response.context["selected_ids"], {"foundation"})
        self.assertFalse(response.context["saved"])
        self.assertFalse("synthetic-invalid-private-payload" in response.text)


def disabled_implementation(module_name: str) -> bool:
    for prefix in (
        "luigi_web.cards", "luigi_web.finance", "luigi_web.rpg", "luigi_web.feedback",
        "luigi_web.llm", "luigi_web.gnw", "luigi_web.chat_tools",
    ):
        if module_name == prefix or module_name.startswith((prefix + ".", prefix + "_")):
            return True
    for module_id in module_registry.BUILTIN_MODULE_IDS:
        package = f"luigi_web.modules.{module_id}"
        private_domain = module_id in {"cards", "finance", "characters", "feedback", "assistant", "media"}
        if private_domain and module_name.startswith(package + ".") and module_name != package + ".manifest":
            return True
        if module_name == package + ".routes" or module_name.startswith(package + ".routes."):
            return True
    return False


def install_probe_guards(directory: Path) -> list[str]:
    attempts: list[str] = []
    private_root = _PROJECT_ROOT / "data"

    class DisabledImportGuard(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if disabled_implementation(fullname):
                attempts.append("disabled implementation import")
                raise RuntimeError("Offline probe refused a disabled implementation")
            return None

    def is_internal_socketpair() -> bool:
        frame = sys._getframe()
        while frame is not None:
            if frame.f_code.co_name == "_fallback_socketpair" and frame.f_globals.get("__name__") == "socket":
                return True
            frame = frame.f_back
        return False

    def audit(event, arguments):
        if event in {
            "socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname",
            "socket.sendto", "subprocess.Popen", "os.system", "sqlite3.connect",
        }:
            if event.startswith("socket.") and is_internal_socketpair():
                return
            attempts.append("network, process, or database access")
            raise RuntimeError("Offline probe refused an external side effect")
        if event not in {"open", "os.listdir", "os.scandir", "os.mkdir", "os.remove", "os.rmdir", "os.rename"}:
            return
        for value in arguments[:2] if event == "os.rename" else arguments[:1]:
            if not isinstance(value, (str, bytes, os.PathLike)):
                continue
            path = Path(os.path.abspath(os.fsdecode(value)))
            private = (
                path.is_relative_to(private_root)
                or path.name.lower().startswith(".env")
                or path.name.lower() in {"gnw-credentials.json", "task-web-metadata.json"}
                or path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
            )
            write = event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename"}
            if event == "open":
                mode = arguments[1] or ""
                flags = arguments[2] or 0
                write = any(character in mode for character in "wax+") or bool(
                    flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
                )
            if private or (write and not path.is_relative_to(directory)):
                attempts.append("private file or out-of-sandbox write")
                raise RuntimeError("Offline probe refused private storage access")

    sys.meta_path.insert(0, DisabledImportGuard())
    sys.addaudithook(audit)
    return attempts


def external_security_probe(host, *, forged_bearer: bool = False) -> dict:
    calls = []
    package = ModuleType("runtime_test_example")
    router = APIRouter()

    async def mutate(request: Request):
        calls.append(request.method)
        return {"status": "synthetic"}

    router.add_api_route("/extensions/example", mutate, methods=["POST", "PUT", "PATCH", "DELETE"])
    package.router = router
    manifest = synthetic_module(source="External package", package=package.__name__)
    application = small_app()
    application.middleware("http")(host.csrf_middleware)
    with (
        mock.patch.dict(sys.modules, {package.__name__: package}),
        mock.patch.object(templating, "mount_module_assets") as assets,
    ):
        module_registry.mount_modules(application, module_registry.ModuleRegistry([manifest]))
    result = {"asset_mount_calls": assets.call_count}
    with TestClient(application, follow_redirects=False) as client:
        result["unauthenticated"] = client.post("/extensions/example").status_code
        client.cookies.set(auth.COOKIE_NAME, os.environ["LUIGI_WEB_UI_TOKEN"])
        if forged_bearer:
            result["forged_bearer_statuses"] = [
                client.post("/extensions/example", headers={"Authorization": header}).status_code
                for header in ("Bearer synthetic-invalid-header", "Bearer ", "bEaReR synthetic-invalid-header")
            ]
        else:
            result["missing_csrf"] = []
            result["mismatched_csrf"] = []
            result["valid_csrf"] = []
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                result["missing_csrf"].append(client.request(method, "/extensions/example").status_code)
                result["mismatched_csrf"].append(client.request(
                    method, "/extensions/example", headers={"X-CSRF-Token": "synthetic-mismatch"},
                ).status_code)
                result["valid_csrf"].append(client.request(
                    method, "/extensions/example", headers={"X-CSRF-Token": client.cookies[auth.CSRF_COOKIE_NAME]},
                ).status_code)
            client.cookies.clear()
            client.cookies.set(auth.COOKIE_NAME, "synthetic-invalid-session")
            result["valid_bearer"] = client.post("/extensions/example", headers={
                "Authorization": "Bearer " + os.environ["LUIGI_WEB_UI_TOKEN"],
            }).status_code
    result["mutations"] = len(calls)
    return result


def manager_csrf_probe(host) -> dict:
    os.environ.pop("LUIGI_WEB_MODULES", None)
    application = small_app()
    application.state.modules = module_registry.ModuleRegistry([synthetic_module()], "none")
    application.state.module_status = {}
    application.include_router(modules_routes.router, dependencies=[Depends(auth.require_auth)])
    application.middleware("http")(host.csrf_middleware)
    with mock.patch.object(modules_routes, "save_selection") as save, TestClient(application) as client:
        unauthenticated = client.post("/modules", data={"enabled": "example"}).status_code
        client.cookies.set(auth.COOKIE_NAME, os.environ["LUIGI_WEB_UI_TOKEN"])
        rendered = client.get("/modules")
        missing = client.post("/modules", data={"enabled": "example"}).status_code
        mismatch = client.post("/modules", data={"enabled": "example"}, headers={
            "X-CSRF-Token": "synthetic-mismatch",
        }).status_code
        rejected_saves = save.call_count
        accepted = client.post("/modules", data={"enabled": "example"}, headers={
            "X-CSRF-Token": client.cookies[auth.CSRF_COOKIE_NAME],
        })
    return {
        "unauthenticated": unauthenticated,
        "rendered": rendered.status_code,
        "missing_csrf": missing,
        "mismatched_csrf": mismatch,
        "rejected_saves": rejected_saves,
        "accepted": accepted.status_code,
        "save_calls": save.call_count,
        "correct_selection": save.call_args == mock.call(["example"]),
    }


def run_host_probe(case: str, directory: Path) -> None:
    attempts = install_probe_guards(directory)
    phase = "import"
    try:
        from luigi_web import db

        with ExitStack() as stack:
            watched = {name: stack.enter_context(mock.patch.object(
                db, name, side_effect=RuntimeError("Offline probe refused database use"),
            )) for name in (
                "get_engine", "check_schema_version", "ensure_web_columns",
                "reactivate_due_recurring", "find_tasks_by_name", "search_disciplines",
            )}
            hook = stack.enter_context(mock.patch.object(
                module_registry, "_call_hook", side_effect=RuntimeError("Disabled lifecycle hook was called"),
            ))
            host = importlib.import_module("luigi_web.application")
            phase = case
            result = {}
            if case in {"startup", "health-error"}:
                with TestClient(host.app, follow_redirects=False) as client:
                    if case == "health-error":
                        host._STARTUP_SCHEMA["error"] = "synthetic-private-backend-detail"
                        response = client.get("/healthz")
                        result["health"] = response.json()
                        result["private_error_hidden"] = "synthetic-private-backend-detail" not in response.text
                    else:
                        result["api_auth"] = client.get("/modules").status_code
                        browser = client.get("/modules", headers={"Accept": "text/html"})
                        result["browser_auth"] = [browser.status_code, browser.headers.get("location")]
                        client.headers["Authorization"] = "Bearer " + os.environ["LUIGI_WEB_UI_TOKEN"]
                        page = client.get("/modules")
                        result["manager"] = [page.status_code, page.headers.get("cache-control")]
                        result["manager_rendered"] = '<form method="post" action="/modules"' in page.text
                        result["private_token_hidden"] = os.environ["LUIGI_WEB_UI_TOKEN"] not in page.text
                        root = client.get("/")
                        result["landing"] = [root.status_code, root.headers.get("location")]
                        result["disabled_statuses"] = [client.get(path).status_code for path in (
                            "/tasks", "/discipline", "/cards", "/finance", "/characters", "/feedback", "/chat/send",
                        )]
                        result["search"] = client.get("/command-palette", params={"q": "synthetic"}).status_code
                        result["health"] = client.get("/healthz").json()
            elif case in {"external-csrf", "forged-bearer"}:
                result.update(external_security_probe(host, forged_bearer=case == "forged-bearer"))
            elif case == "manager-csrf":
                result.update(manager_csrf_probe(host))
            elif case != "import":
                raise ValueError("Unknown synthetic probe")
            result.update({
                "enabled_ids": [module.id for module in host.app.state.modules.enabled],
                "statuses": host.app.state.module_status,
                "disabled_imports": sorted(name for name in sys.modules if disabled_implementation(name)),
                "llm_state_present": any(name in vars(host) for name in ("_LLM_PROVIDER", "_LLM_TOOLS")),
                "database_calls": [name for name, guard in watched.items() if guard.called],
                "hook_calls": hook.call_count,
                "created_paths": sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*")),
                "guard_attempts": attempts,
            })
    except Exception as error:
        result = {
            "probe_error": type(error).__name__,
            "phase": phase,
            "frames": [f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in traceback.extract_tb(error.__traceback__)[-5:]],
        }
    print(json.dumps(result))


_HOST_PROBE_SCRIPT = """
import importlib.util
import pathlib
import sys
sys.path.insert(0, sys.argv[1])
source = pathlib.Path(sys.argv[1]) / "tests" / "test_module_runtime.py"
spec = importlib.util.spec_from_file_location("runtime_probe_tests", source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.run_host_probe(sys.argv[2], pathlib.Path(sys.argv[3]))
"""


class ColdHostRuntimeTests(IsolatedRuntimeTestCase):
    def probe(self, case: str) -> dict:
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", _HOST_PROBE_SCRIPT, str(_PROJECT_ROOT), case, str(self.directory)],
            cwd=self.directory,
            env=clean_environment(self.directory),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, "Isolated host probe did not complete safely")
        try:
            result = json.loads(completed.stdout)
        except ValueError:
            self.fail("Isolated host probe returned no structured result; raw output withheld")
        if "probe_error" in result:
            self.fail(f"Isolated host probe failed: {result['phase']} / {result['probe_error']} / {result['frames']}")
        self.assertEqual(result["guard_attempts"], [])
        self.assertEqual(result["database_calls"], [])
        self.assertEqual(result["disabled_imports"], [])
        self.assertEqual(result["hook_calls"], 0)
        self.assertFalse(result["llm_state_present"])
        self.assertEqual(result["enabled_ids"], [])
        self.assertEqual(result["statuses"], {})
        self.assertEqual(result["created_paths"], [])
        return result

    def test_cold_none_import_excludes_private_implementations_providers_and_storage(self) -> None:
        self.probe("import")

    def test_core_only_startup_needs_no_db_or_disabled_initializers_and_renders_authenticated_manager(self) -> None:
        result = self.probe("startup")
        self.assertEqual(result["api_auth"], 401)
        self.assertEqual(result["browser_auth"], [303, "/login"])
        self.assertEqual(result["manager"], [200, "no-store"])
        self.assertTrue(result["manager_rendered"])
        self.assertTrue(result["private_token_hidden"])
        self.assertEqual(result["landing"], [303, "/modules"])
        self.assertEqual(result["disabled_statuses"], [404] * 7)
        self.assertEqual(result["search"], 200)
        self.assertEqual(result["health"], {"status": "ok", "schema_version": None, "error": None})

    def test_mounted_external_mutations_use_current_global_csrf_for_all_unsafe_methods(self) -> None:
        result = self.probe("external-csrf")
        self.assertEqual(result["asset_mount_calls"], 1)
        self.assertEqual(result["unauthenticated"], 401)
        self.assertEqual(result["missing_csrf"], [403] * 4)
        self.assertEqual(result["mismatched_csrf"], [403] * 4)
        self.assertEqual(result["valid_csrf"], [200] * 4)
        self.assertEqual(result["valid_bearer"], 200)
        self.assertEqual(result["mutations"], 5)

    def test_cookie_authenticated_external_mutation_cannot_bypass_csrf_with_invalid_bearer(self) -> None:
        result = self.probe("forged-bearer")
        self.assertEqual(result["forged_bearer_statuses"], [403] * 3)
        self.assertEqual(result["mutations"], 0)

    def test_manager_form_uses_current_global_csrf_before_saving_synthetic_selection(self) -> None:
        result = self.probe("manager-csrf")
        self.assertEqual(result["unauthenticated"], 401)
        self.assertEqual(result["rendered"], 200)
        self.assertEqual(result["missing_csrf"], 403)
        self.assertEqual(result["mismatched_csrf"], 403)
        self.assertEqual(result["rejected_saves"], 0)
        self.assertEqual(result["accepted"], 200)
        self.assertEqual(result["save_calls"], 1)
        self.assertTrue(result["correct_selection"])

    def test_public_health_status_never_exposes_backend_error_details(self) -> None:
        result = self.probe("health-error")
        self.assertTrue(result["private_error_hidden"])
        self.assertEqual(result["health"], {
            "status": "degraded", "schema_version": None, "error": "Shared task storage unavailable",
        })


if __name__ == "__main__":
    unittest.main()