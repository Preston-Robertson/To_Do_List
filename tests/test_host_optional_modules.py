"""Optional-package contracts using synthetic manifests and disposable host copies."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import APIRouter, FastAPI

from luigi_web.core import module_registry, optional_modules

ROOT = Path(__file__).resolve().parents[1]


def isolated_environment(directory: Path) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH"}
    }
    environment.update({key: str(directory) for key in (
        "TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR", "APPDATA", "LOCALAPPDATA",
        "HOME", "USERPROFILE", "LUIGI_WEB_DATA_DIR",
    )})
    environment.update(
        LUIGI_WEB_MODULES_FILE=str(directory / "modules.json"),
        LUIGI_WEB_UI_TOKEN="synthetic-optional-module-session",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return environment


def feature_manifest(module_id: str = "tasks", **overrides) -> module_registry.Module:
    package = f"luigi_web.modules.{module_id}"
    values = dict(id=module_id, label="Synthetic feature", description="Synthetic manifest",
                  package=package, router=package + ".routes:router")
    values.update(overrides)
    return module_registry.Module(**values)


def feature_entry(module_id: str = "tasks", **overrides) -> SimpleNamespace:
    values = dict(
        name=module_id, value=f"luigi_web.modules.{module_id}.manifest:module",
        dist=SimpleNamespace(metadata={"Name": f"luigi-web-{module_id}"}),
        load=mock.Mock(return_value=feature_manifest(module_id)),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class OptionalImportTests(unittest.TestCase):
    def test_absent_parent_returns_proxy_without_importing_or_creating_storage(self) -> None:
        name = "luigi_web.modules.tasks.repository"
        with (
            mock.patch.object(optional_modules.util, "find_spec", return_value=None),
            mock.patch.object(optional_modules, "import_module") as load,
        ):
            proxy = optional_modules.optional_module(name)
            self.assertIsInstance(proxy, optional_modules.ModuleProxy)
            with self.assertRaisesRegex(optional_modules.ModuleUnavailable, "install luigi-web-tasks"):
                proxy.check_schema_version()
        load.assert_not_called()

    def test_installed_module_is_returned_by_identity(self) -> None:
        expected = SimpleNamespace(check_schema_version=mock.Mock())
        with (
            mock.patch.object(optional_modules.util, "find_spec", return_value=object()),
            mock.patch.object(optional_modules, "import_module", return_value=expected),
        ):
            self.assertIs(optional_modules.optional_module("luigi_web.modules.tasks.repository"), expected)

    def test_proxy_resolves_on_access_after_package_becomes_available(self) -> None:
        proxy = optional_modules.ModuleProxy("luigi_web.modules.tasks.repository")
        expected = SimpleNamespace(marker=object())
        with (
            mock.patch.object(optional_modules.util, "find_spec", return_value=object()),
            mock.patch.object(optional_modules, "import_module", return_value=expected),
        ):
            self.assertIs(proxy.marker, expected.marker)

    def test_installed_package_dependency_errors_are_not_treated_as_absence(self) -> None:
        failure = ModuleNotFoundError("Synthetic dependency missing", name="synthetic_dependency")
        with (
            mock.patch.object(optional_modules.util, "find_spec", return_value=object()),
            mock.patch.object(optional_modules, "import_module", side_effect=failure),
        ):
            with self.assertRaises(ModuleNotFoundError) as caught:
                optional_modules.optional_module("luigi_web.modules.tasks.repository")
        self.assertIs(caught.exception, failure)
        with mock.patch.object(optional_modules.util, "find_spec", side_effect=failure):
            with self.assertRaises(ModuleNotFoundError) as caught:
                optional_modules.module_available("luigi_web.modules.tasks.manifest")
        self.assertIs(caught.exception, failure)

    def test_only_known_canonical_features_can_be_requested(self) -> None:
        for name in ("os", "luigi_web.core.auth", "luigi_web.modules.unapproved"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                optional_modules.module_available(name)


class OptionalDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="luigi-optional-registry-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.enterContext(mock.patch.dict(os.environ, isolated_environment(self.directory), clear=True))
        self.available = self.enterContext(mock.patch.object(module_registry, "module_available", return_value=False))
        self.entries = self.enterContext(mock.patch.object(module_registry.metadata, "entry_points", return_value=[]))

    def test_empty_installation_defaults_to_core_only_without_settings_write(self) -> None:
        registry = module_registry.build_registry()
        self.assertEqual(registry.catalog, ())
        self.assertEqual(registry.enabled, ())
        self.assertEqual(registry.landing_path, "/modules")
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(module_registry.MODULE_API_VERSION, 1)

    def test_explicit_missing_selection_and_dependency_are_clear(self) -> None:
        with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "Unknown modules: tasks.*Available: none"):
            module_registry.build_registry("tasks")
        with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "requires tasks.*not installed"):
            module_registry.ModuleRegistry([feature_manifest("planning", requires=("tasks",))], "planning")

    def test_installed_known_feature_is_discovered_and_enabled_by_default(self) -> None:
        entry = feature_entry("admin", dist=SimpleNamespace(metadata={"Name": "Luigi_Web.Admin"}))
        self.entries.return_value = [entry]
        self.available.side_effect = lambda name: name == "luigi_web.modules.admin.manifest"
        registry = module_registry.build_registry()
        self.assertEqual([module.id for module in registry.enabled], ["admin"])
        entry.load.assert_called_once_with()

    def test_installation_does_not_override_explicit_none(self) -> None:
        entry = feature_entry("admin")
        self.entries.return_value = [entry]
        self.available.return_value = True
        with mock.patch.object(module_registry, "BUILTIN_MODULE_IDS", ("admin",)):
            registry = module_registry.build_registry("none")
        self.assertEqual(registry.enabled, ())
        self.assertEqual([module.id for module in registry.catalog], ["admin"])

    def test_checkout_fallback_requires_the_exact_known_manifest_location(self) -> None:
        self.available.side_effect = lambda name: name == "luigi_web.modules.tasks.manifest"
        spec = ModuleSpec("luigi_web.modules.tasks.manifest", loader=None,
                          origin=str(ROOT / "module-repos/tasks/src/luigi_web/modules/tasks/manifest.py"))
        manifest = SimpleNamespace(module=feature_manifest(), __spec__=spec)
        with (
            mock.patch.object(module_registry.util, "find_spec", return_value=spec),
            mock.patch.object(module_registry, "import_module", return_value=manifest) as load,
        ):
            self.assertEqual([module.id for module in module_registry.discover_modules()], ["tasks"])
            load.reset_mock()
            spec.origin = str(self.directory / "manifest.py")
            with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "distribution entry point"):
                module_registry.discover_modules()
            load.assert_not_called()

    def test_spoofed_reserved_entry_points_are_rejected_before_load(self) -> None:
        entries = (
            feature_entry(dist=None),
            feature_entry(dist=SimpleNamespace(metadata={"Name": "untrusted-feature"})),
            feature_entry(value="untrusted_feature.manifest:module"),
            feature_entry(value="luigi_web.modules.tasks.routes:module"),
        )
        for entry in entries:
            with self.subTest(entry=entry.value):
                self.entries.return_value = [entry]
                with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "Untrusted reserved"):
                    module_registry.discover_modules()
                entry.load.assert_not_called()

    def test_duplicate_feature_entry_points_are_rejected_before_load(self) -> None:
        entries = [feature_entry(), feature_entry()]
        self.entries.return_value = entries
        with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "Expected one installed entry point"):
            module_registry.discover_modules()
        for entry in entries:
            entry.load.assert_not_called()

    def test_external_allowlist_cannot_claim_an_absent_reserved_id(self) -> None:
        os.environ["LUIGI_WEB_EXTERNAL_MODULES"] = "tasks"
        with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "reserved feature"):
            module_registry.discover_modules()
        self.entries.assert_not_called()

    def test_feature_manifest_cannot_escape_its_canonical_package(self) -> None:
        self.available.side_effect = lambda name: name == "luigi_web.modules.tasks.manifest"
        for overrides in (
            {"package": "other_package"}, {"router": "other_package.routes:router"},
            {"router": "luigi_web.modules.tasks_other.routes:router"},
            {"startup": "other_package.lifecycle:start"}, {"shutdown": "other_package.lifecycle:stop"},
            {"template_setup": "other_package.templates:configure"}, {"id": "admin"},
        ):
            with self.subTest(overrides=overrides):
                entry = feature_entry(load=mock.Mock(return_value=feature_manifest(**overrides)))
                self.entries.return_value = [entry]
                with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "Invalid reserved feature"):
                    module_registry.discover_modules()

    def test_composition_router_is_allowed_for_known_feature(self) -> None:
        entry = feature_entry("finance", load=mock.Mock(return_value=feature_manifest(
            "finance", router="luigi_web.modules.finance.composition:router",
        )))
        self.entries.return_value = [entry]
        self.available.side_effect = lambda name: name == "luigi_web.modules.finance.manifest"
        self.assertEqual(module_registry.discover_modules()[0].router, "luigi_web.modules.finance.composition:router")

    def test_present_feature_import_failure_is_not_silently_skipped(self) -> None:
        entry = feature_entry(load=mock.Mock(side_effect=ModuleNotFoundError("Synthetic dependency")))
        self.entries.return_value = [entry]
        self.available.return_value = True
        with self.assertRaises(ModuleNotFoundError):
            module_registry.discover_modules()

    def test_source_label_alone_cannot_bypass_route_namespace(self) -> None:
        module = module_registry.Module(id="example", label="Synthetic", description="Synthetic",
                                        package="example", router="example.routes:router", source="Built-in")
        router = APIRouter()
        router.add_api_route("/outside", lambda: {})
        with mock.patch.object(module_registry, "_load_reference", return_value=router):
            with self.assertRaisesRegex(module_registry.ModuleConfigurationError, "extension namespace"):
                module_registry.mount_modules(FastAPI(), module_registry.ModuleRegistry([module]))


CORE_ONLY_PROBE = r'''
import importlib.abc
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, sys.argv[1])
sys.dont_write_bytecode = True
blocked = {"sqlalchemy", "psycopg", "psycopg2", "gspread", "google", "pandas", "dotenv"}
attempts = []

class NoFeatureDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            attempts.append(fullname)
            raise AssertionError("Core attempted a feature dependency")
        return None

def audit(event, arguments):
    if event == "sqlite3.connect" or event in {"subprocess.Popen", "os.system"}:
        raise AssertionError("Core attempted storage or a process")
    if event.startswith("socket."):
        frame = sys._getframe()
        while frame is not None:
            if frame.f_code.co_name == "_fallback_socketpair" and frame.f_globals.get("__name__") == "socket":
                return
            frame = frame.f_back
        if event in {"socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto"}:
            raise AssertionError("Core attempted network access")
    if event == "open" and isinstance(arguments[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(arguments[0]))
        if path.name.lower().startswith(".env") or path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            raise AssertionError("Core attempted private storage")

sys.meta_path.insert(0, NoFeatureDependencies())
sys.addaudithook(audit)
from fastapi.testclient import TestClient
from luigi_web.core.module_registry import ModuleRegistry
from luigi_web.core.optional_modules import ModuleProxy, ModuleUnavailable
from luigi_web import application as host

assert isinstance(host.db, ModuleProxy)
assert host.app.state.modules.catalog == ()
assert host.app.state.modules.enabled == ()
assert "reactivation_date" not in host.templates.env.globals
assert "completion_day_policy" not in host.templates.env.globals
for name in ("gnw", "finance", "operations", "task_backup", "tasks_page", "_LLM_PROVIDER", "_LLM_TOOLS"):
    try:
        getattr(host, name)
    except ModuleUnavailable as error:
        assert str(Path(sys.argv[1])) not in str(error)
        assert "Optional module" in str(error)
    else:
        raise AssertionError("Missing legacy feature did not fail explicitly")
try:
    host.db.check_schema_version()
except ModuleUnavailable:
    pass
else:
    raise AssertionError("Missing Tasks compatibility API unexpectedly resolved")

with TestClient(host.app, follow_redirects=False) as client:
    assert client.get("/healthz").json() == {"status": "ok", "schema_version": None, "error": None}
    assert client.get("/login").status_code == 200
    assert client.get("/modules").status_code == 401
    client.headers["Authorization"] = "Bearer " + os.environ["LUIGI_WEB_UI_TOKEN"]
    response = client.get("/modules")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/").headers["location"] == "/modules"
    assert client.get("/command-palette").status_code == 200
    for path in ("/tasks", "/finance", "/docs", "/openapi.json"):
        assert client.get(path).status_code == 404
    client.headers.pop("Authorization")
    client.cookies.set(host.COOKIE_NAME, os.environ["LUIGI_WEB_UI_TOKEN"])
    assert client.post("/modules", data={}).status_code == 403

with mock.patch.object(host, "_module_enabled", side_effect=lambda name: name == "discipline"):
    try:
        host._startup_schema_check()
    except ModuleUnavailable:
        pass
    else:
        raise AssertionError("Discipline startup accepted a missing Tasks package")
assert not attempts
assert not any(name.startswith("luigi_web.modules.") for name in sys.modules)
assert not Path(os.environ["LUIGI_WEB_MODULES_FILE"]).exists()
print("core-only startup, routes, privacy and compatibility passed")
'''


TASKS_IDENTITY_PROBE = r'''
import importlib
import sys
from unittest import mock
sys.path.insert(0, sys.argv[1])
from luigi_web import application as host
from luigi_web.core.optional_modules import ModuleUnavailable
repository = importlib.import_module("luigi_web.modules.tasks.repository")
assert host.db is repository is importlib.import_module("luigi_web.db")
with mock.patch.object(host.db, "check_schema_version", return_value=2) as check:
    assert repository.check_schema_version() == 2
    check.reset_mock()
    host._startup_schema_check()
    check.assert_not_called()
    with mock.patch.object(host, "_module_enabled", side_effect=lambda name: name == "discipline"), mock.patch.object(host.db, "ensure_web_columns") as ensure, mock.patch.object(host, "_reactivate_recurring") as generate:
        host._startup_schema_check()
        check.assert_called_once_with()
        ensure.assert_called_once_with()
        generate.assert_not_called()
        assert host._STARTUP_SCHEMA == {"version": 2, "error": None}
print("Tasks identity, monkeypatch and Discipline-only startup passed")
'''


class IsolatedHostTests(unittest.TestCase):
    def run_probe(self, script: str, root: Path, directory: Path, selection: str | None) -> None:
        environment = isolated_environment(directory)
        if selection is not None:
            environment["LUIGI_WEB_MODULES"] = selection
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(root)], cwd=directory,
            env=environment, capture_output=True, text=True, timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("passed", result.stdout)

    def test_copied_core_only_host_needs_no_feature_dependencies(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luigi-core-only-") as temporary:
            directory = Path(temporary)
            site = directory / "site"
            package = site / "luigi_web"
            package.mkdir(parents=True)
            for name in ("__init__.py", "application.py", "auth.py", "clock.py", "paths.py"):
                shutil.copy2(ROOT / "luigi_web" / name, package / name)
            shutil.copytree(ROOT / "luigi_web/core", package / "core", ignore=shutil.ignore_patterns("__pycache__"))
            (package / "modules").mkdir()
            shutil.copy2(ROOT / "luigi_web/modules/__init__.py", package / "modules/__init__.py")
            for selection in (None, "none"):
                with self.subTest(selection=selection):
                    self.run_probe(CORE_ONLY_PROBE, site, directory, selection)

    def test_installed_tasks_preserves_old_namespace_identity_and_monkeypatches(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luigi-tasks-identity-") as temporary:
            self.run_probe(TASKS_IDENTITY_PROBE, ROOT, Path(temporary), "none")


if __name__ == "__main__":
    unittest.main()