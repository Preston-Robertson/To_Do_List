"""Offline wheel contracts using disposable installs and synthetic settings."""
from __future__ import annotations

import importlib.util
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from email.parser import Parser
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "example-module"
SYSTEM_ENVIRONMENT = {
    key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ
}


def clean_environment(directory: Path, target: Path | None = None) -> dict[str, str]:
    environment = {
        **SYSTEM_ENVIRONMENT,
        "LUIGI_WEB_MODULES": "none",
        "LUIGI_WEB_MODULES_FILE": str(directory / "modules.json"),
        "LUIGI_WEB_DATA_DIR": str(directory / "data"),
        "LUIGI_WEB_UI_TOKEN": "synthetic-packaging-session",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        **{key: str(directory) for key in (
            "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_DATA_HOME",
        )},
    }
    if target is not None:
        environment["PYTHONPATH"] = str(target)
    return environment


def run_python(arguments: list[str], directory: Path, environment: dict[str, str]):
    completed = subprocess.run(
        [sys.executable, "-B", *arguments], cwd=directory, env=environment,
        capture_output=True, text=True, encoding="utf-8", timeout=180, check=False,
    )
    if completed.returncode:
        raise AssertionError(f"Isolated packaging command failed with exit code {completed.returncode}; raw output withheld")
    return completed


def build_wheels(directory: Path) -> tuple[Path, Path]:
    host = directory / "host-source"
    example = directory / "separate-example-repository"
    wheels = directory / "wheels"
    host.mkdir()
    wheels.mkdir()
    for filename in ("pyproject.toml", "requirements.txt"):
        shutil.copy2(ROOT / filename, host / filename)
    ignore = shutil.ignore_patterns(
        "__pycache__", "*.pyc", "*.pyo", ".env*", "LOCAL_*", "data",
        "*.db*", "*.sqlite*", "*credentials*", "*.egg-info", "build", "dist",
    )
    shutil.copytree(ROOT / "luigi_web", host / "luigi_web", ignore=ignore)
    shutil.copytree(EXAMPLE, example, ignore=ignore)
    environment = clean_environment(directory)
    for source in (host, example):
        run_python([
            "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "--no-index",
            "--no-cache-dir", "--wheel-dir", str(wheels),
        ], source, environment)
    return (
        next(wheels.glob("luigi_web-0.1.0-*.whl")),
        next(wheels.glob("luigi_example-0.1.0-*.whl")),
    )


INSTALLED_PROBE = r'''
import contextlib
import importlib.metadata
import io
import json
import os
import sys
from pathlib import Path

target = Path(sys.argv[1]).resolve()
case = sys.argv[2]
attempts = []

def audit(event, arguments):
    if event == "socket.connect":
        frame = sys._getframe()
        while frame is not None:
            if frame.f_code.co_name == "_fallback_socketpair":
                return
            frame = frame.f_back
        attempts.append("network")
        raise RuntimeError("Packaging probe refused network access")
    if event == "sqlite3.connect":
        attempts.append("database")
        raise RuntimeError("Packaging probe refused database access")
    if event == "open" and isinstance(arguments[0], (str, bytes, os.PathLike)):
        candidate = Path(os.fsdecode(arguments[0])).resolve()
        if candidate.name.startswith((".env", "LOCAL_")) or "data" in candidate.parts:
            attempts.append("private-file")
            raise RuntimeError("Packaging probe refused private file access")

sys.addaudithook(audit)
import luigi_web
assert Path(luigi_web.__file__).resolve().is_relative_to(target)
assert not Path.cwd().is_relative_to(target)
if case == "data-defaults":
    os.environ.pop("LUIGI_WEB_DATA_DIR")
from luigi_web import paths
assert paths.STATIC_DIR == target / "luigi_web" / "core" / "static"
assert paths.TEMPLATES_DIR == target / "luigi_web" / "core" / "templates"
assert not paths.DATA_DIR.is_relative_to(target)
assert paths.TASK_METADATA_PATH.parent == paths.DATA_DIR
assert paths.GNW_CREDENTIALS_PATH.parent == paths.DATA_DIR

entries = importlib.metadata.entry_points(group="luigi_web.modules")
assert len([entry for entry in entries if entry.name == "example"]) == 1
assert "luigi_example" not in sys.modules
result = {"case": case}

if case == "data-defaults":
    if sys.platform == "win32":
        expected = Path(os.environ["LOCALAPPDATA"]) / "luigi-web"
    elif sys.platform == "darwin":
        expected = Path.home() / "Library" / "Application Support" / "luigi-web"
    else:
        expected = Path(os.environ["XDG_DATA_HOME"]) / "luigi-web"
    assert paths.DATA_DIR == expected
    from luigi_web import db, gnw
    assert Path(db._WEB_META_PATH) == expected / "task-web-metadata.json"
    assert Path(gnw.DEFAULT_CREDS_PATH) == expected / "gnw-credentials.json"
    assert not expected.exists()
elif case == "help":
    command = next(entry for entry in importlib.metadata.distribution("luigi-web").entry_points
                   if entry.group == "console_scripts" and entry.name == "luigi-web")
    with contextlib.redirect_stdout(io.StringIO()) as output:
        try:
            command.load()(["--help"])
        except SystemExit as exit_status:
            assert exit_status.code == 0
    assert "--host" in output.getvalue() and "--port" in output.getvalue()
    assert "luigi_web.application" not in sys.modules
    assert "uvicorn" not in sys.modules
    assert "luigi_example" not in sys.modules
elif case in {"default", "unapproved"}:
    from luigi_web.core.module_registry import build_registry, ModuleConfigurationError
    if case == "default":
        os.environ.pop("LUIGI_WEB_MODULES", None)
        registry = build_registry()
        assert "example" in {module.id for module in registry.catalog}
        assert not registry.is_enabled("example")
        assert "luigi_example.routes" not in sys.modules
    else:
        try:
            build_registry("example")
        except ModuleConfigurationError:
            pass
        else:
            raise AssertionError("Unapproved entry point was enabled")
        assert "luigi_example" not in sys.modules
elif case == "all-templates":
    from luigi_web.core.module_registry import BUILTIN_MODULE_IDS
    os.environ["LUIGI_WEB_MODULES"] = ",".join((*BUILTIN_MODULE_IDS, "example"))
    from luigi_web import application
    from luigi_example.routes import templates
    names = [name for name in application.templates.env.list_templates() if name.endswith(".html")]
    for name in names:
        application.templates.get_template(name)
    templates.get_template("example/status.html")
    registrations = [(method, route.path) for route in application.app.routes
                     for method in getattr(route, "methods", ()) or ()]
    assert len(registrations) == len(set(registrations))
    result.update(templates=len(names) + 1, registrations=len(registrations))
else:
    from fastapi.testclient import TestClient
    from luigi_web import application
    from luigi_web.core.module_registry import Module
    assert Path(application.__file__).resolve().is_relative_to(target)
    enabled = case == "enabled"
    registry = application.app.state.modules
    assert registry.is_enabled("example") == enabled
    if enabled:
        from luigi_example.manifest import module
        assert isinstance(module, Module)
        assert module.api_version == 1 and module.package == "luigi_example"
        assert Path(sys.modules["luigi_example"].__file__).resolve().is_relative_to(target)
        assert module.navigation[0].icon == "blocks"
    else:
        assert "luigi_example.routes" not in sys.modules
        if case == "core":
            assert "luigi_example" not in sys.modules
    with TestClient(application.app, follow_redirects=False) as client:
        assert client.get("/login").status_code == 200
        assert client.get("/modules").status_code == 401
        assert client.get("/extensions/example").status_code == (401 if enabled else 404)
        if enabled:
            denied = client.get("/extensions/example", headers={"Accept": "text/html"})
            assert denied.status_code == 303 and denied.headers["location"] == "/login"
        client.headers["Authorization"] = "Bearer synthetic-packaging-session"
        manager = client.get("/modules")
        assert manager.status_code == 200 and "module-page" in manager.text
        assert manager.headers["cache-control"] == "no-store"
        assert client.get("/command-palette", params={"q": "Example"}).status_code == 200
        assert client.get("/healthz").json()["status"] == "ok"
        root = client.get("/")
        assert root.status_code == 303
        assert root.headers["location"] == ("/extensions/example" if enabled else "/modules")
        page = client.get("/extensions/example")
        asset = client.get("/module-assets/example/status.css")
        assert page.status_code == (200 if enabled else 404)
        assert asset.status_code == (200 if enabled else 404)
        if enabled:
            assert "luigi_example" in page.text and "Ready" in page.text
            assert "example-details" in page.text
            assert "/module-assets/example/status.css" in page.text
            assert "External package" in manager.text
            assert ".example-module" in asset.text
            assert client.post("/extensions/example").status_code == 405
        resources = [resource for resource in paths.STATIC_DIR.rglob("*") if resource.is_file()]
        for resource in resources:
            url = "/static/" + resource.relative_to(paths.STATIC_DIR).as_posix()
            response = client.get(url)
            assert response.status_code == 200 and response.content, url
            if resource.suffix == ".woff2":
                assert response.content.startswith(b"wOF2"), url
            if resource.suffix == ".svg":
                assert "image/svg+xml" in response.headers["content-type"], url
        result["static_files"] = len(resources)
    assert not list(Path(os.environ["LUIGI_WEB_DATA_DIR"]).glob("**/*"))

assert not attempts, "Probe attempted private storage or network access"
result["passed"] = True
print(json.dumps(result))
'''


class DataDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="luigi-data-paths-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.enterContext(mock.patch.dict(os.environ, clean_environment(self.directory), clear=True))

    def load_paths(self, *, installed: bool = False):
        with mock.patch.object(Path, "is_file", return_value=not installed):
            return runpy.run_path(str(ROOT / "luigi_web" / "paths.py"))

    def test_source_defaults_preserve_legacy_locations(self) -> None:
        os.environ.pop("LUIGI_WEB_DATA_DIR")
        paths = self.load_paths()
        self.assertEqual(paths["PROJECT_ROOT"], ROOT)
        self.assertEqual(paths["DATA_DIR"], ROOT / "data")
        self.assertEqual(paths["TASK_METADATA_PATH"], ROOT / "task-web-metadata.json")
        self.assertEqual(paths["GNW_CREDENTIALS_PATH"], ROOT / "gnw-credentials.json")

    def test_explicit_data_directory_controls_source_and_installed_defaults(self) -> None:
        for installed in (False, True):
            with self.subTest(installed=installed):
                paths = self.load_paths(installed=installed)
                self.assertEqual(paths["DATA_DIR"], self.directory / "data")
                self.assertEqual(paths["TASK_METADATA_PATH"].parent, paths["DATA_DIR"])
                self.assertEqual(paths["GNW_CREDENTIALS_PATH"].parent, paths["DATA_DIR"])

    def test_explicit_metadata_file_takes_precedence(self) -> None:
        os.environ["LUIGI_WEB_TASK_METADATA_FILE"] = str(self.directory / "synthetic-metadata.json")
        self.assertEqual(self.load_paths()["TASK_METADATA_PATH"], self.directory / "synthetic-metadata.json")

    def test_installed_platform_defaults_are_per_user(self) -> None:
        os.environ.pop("LUIGI_WEB_DATA_DIR")
        for platform, expected in (
            ("win32", self.directory / "luigi-web"),
            ("linux", self.directory / "luigi-web"),
            ("darwin", self.directory / "Library" / "Application Support" / "luigi-web"),
        ):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                paths = self.load_paths(installed=True)
                self.assertEqual(paths["DATA_DIR"], expected)
                self.assertEqual(paths["TASK_METADATA_PATH"].parent, expected)
                self.assertEqual(paths["GNW_CREDENTIALS_PATH"].parent, expected)

    def test_platform_defaults_without_environment_overrides(self) -> None:
        for key in ("LUIGI_WEB_DATA_DIR", "LOCALAPPDATA", "XDG_DATA_HOME"):
            os.environ.pop(key, None)
        for platform, relative in (
            ("win32", Path("AppData/Local/luigi-web")),
            ("linux", Path(".local/share/luigi-web")),
        ):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                self.assertEqual(self.load_paths(installed=True)["DATA_DIR"], self.directory / relative)


class RelocatedResourcePolicyTests(unittest.TestCase):
    def test_relocated_shell_files_keep_write_protection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luigi-resource-policy-") as temporary:
            directory = Path(temporary)
            with mock.patch.dict(os.environ, clean_environment(directory), clear=True):
                from luigi_web.modules.feedback.maintainer_agent import PermissionRequiredError, SafeWorkspace

                workspace = SafeWorkspace(directory)
                for filename in (
                    "static/js/app.js", "templates/base.html",
                    "luigi_web/core/static/js/app.js", "luigi_web/core/templates/base.html",
                ):
                    with self.subTest(filename=filename), self.assertRaises(PermissionRequiredError):
                        workspace.assert_writable_path(filename)

    def test_relocated_resources_still_require_browser_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luigi-resource-policy-") as temporary:
            with mock.patch.dict(os.environ, clean_environment(Path(temporary)), clear=True):
                from luigi_web.modules.feedback.maintainer_worker import _pr_body

                job = {
                    "request_text": "Synthetic display adjustment",
                    "acceptance_criteria": "Synthetic review criterion",
                    "feedback_uuid": "synthetic-feedback",
                    "policy_version": "synthetic-policy",
                }
                for filename in ("luigi_web/core/static/css/shell.css", "luigi_web/core/templates/base.html"):
                    with self.subTest(filename=filename):
                        self.assertIn("Desktop and mobile browser review is required", _pr_body(job, [filename]))


class WheelPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not all(importlib.util.find_spec(package) for package in ("setuptools", "wheel")):
            raise unittest.SkipTest("Wheel verification requires setuptools and wheel in the test environment")
        temporary = tempfile.TemporaryDirectory(prefix="luigi-wheel-tests-")
        cls.addClassCleanup(temporary.cleanup)
        cls.directory = Path(temporary.name)
        cls.host_wheel, cls.example_wheel = build_wheels(cls.directory)
        cls.target = cls.directory / "installed"
        run_python([
            "-m", "pip", "install", "--no-deps", "--no-index", "--no-compile", "--target", str(cls.target),
            str(cls.host_wheel), str(cls.example_wheel),
        ], cls.directory, clean_environment(cls.directory))

    def probe(self, case: str, *, allowlist: bool = False, selected: bool = False):
        environment = clean_environment(self.directory, self.target)
        if allowlist:
            environment["LUIGI_WEB_EXTERNAL_MODULES"] = "example"
        if selected:
            environment["LUIGI_WEB_MODULES"] = "example"
        completed = run_python(["-c", INSTALLED_PROBE, str(self.target), case], self.directory, environment)
        result = json.loads(completed.stdout)
        self.assertTrue(result["passed"])
        return result

    def test_host_wheel_contains_exact_resources_and_only_runtime_files(self) -> None:
        with zipfile.ZipFile(self.host_wheel) as wheel:
            names = wheel.namelist()
            self.assertTrue(all(name.startswith(("luigi_web/", "luigi_web-0.1.0.dist-info/")) for name in names))
            resource_names = set()
            roots = [ROOT / "luigi_web" / "core" / name for name in ("static", "templates")]
            roots.extend(path for name in ("static", "templates") for path in (ROOT / "luigi_web" / "modules").glob(f"*/{name}"))
            for resource_root in roots:
                for resource in resource_root.rglob("*"):
                    if resource.is_file():
                        relative = resource.relative_to(ROOT).as_posix()
                        resource_names.add(relative)
                        self.assertEqual(wheel.read(relative), resource.read_bytes(), relative)
            packaged_resources = {
                name for name in names if name.startswith("luigi_web/") and not name.endswith(".py")
            }
            self.assertEqual(packaged_resources, resource_names)
            self.assertEqual(sum(name.endswith(".html") for name in resource_names), 82)
            for name in names:
                parts = Path(name).parts
                self.assertFalse(any(part.lower().startswith((".env", "local_", "_extract", "_validate")) for part in parts))
                self.assertFalse(set(parts) & {"data", "tests", "scripts", "__pycache__", "examples"})
                self.assertFalse(name.endswith((".db", ".sqlite", ".sqlite3", ".pyc")))
            metadata = Parser().parsestr(wheel.read("luigi_web-0.1.0.dist-info/METADATA").decode())
            requirements = {line.strip() for line in (ROOT / "requirements.txt").read_text().splitlines() if line.strip() and not line.startswith("#")}
            self.assertEqual(set(metadata.get_all("Requires-Dist") or []), requirements)
            self.assertEqual(metadata["Requires-Python"], ">=3.11")

    def test_example_wheel_is_independent_and_has_real_entry_point(self) -> None:
        with zipfile.ZipFile(self.example_wheel) as wheel:
            files = {name for name in wheel.namelist() if not ".dist-info/" in name}
            self.assertEqual(files, {
                "luigi_example/__init__.py", "luigi_example/manifest.py", "luigi_example/routes.py",
                "luigi_example/templates/status.html", "luigi_example/static/status.css",
            })
            metadata = Parser().parsestr(wheel.read("luigi_example-0.1.0.dist-info/METADATA").decode())
            self.assertEqual(metadata.get_all("Requires-Dist"), ["luigi-web<0.2,>=0.1"])
            entries = wheel.read("luigi_example-0.1.0.dist-info/entry_points.txt").decode()
            self.assertIn("[luigi_web.modules]", entries)
            self.assertIn("example = luigi_example.manifest:module", entries)

    def test_installed_help_does_not_import_application_or_run_jobs(self) -> None:
        self.probe("help")

    def test_installed_defaults_and_domain_path_consumers_use_per_user_storage(self) -> None:
        self.probe("data-defaults")

    def test_core_only_host_renders_all_shared_resources(self) -> None:
        self.probe("core")

    def test_unapproved_installed_entry_point_cannot_be_selected(self) -> None:
        self.probe("unapproved")

    def test_allowlist_without_selection_does_not_mount_routes_or_assets(self) -> None:
        self.probe("disabled", allowlist=True)

    def test_missing_selection_keeps_external_module_disabled(self) -> None:
        self.probe("default", allowlist=True)

    def test_selected_installed_example_requires_auth_and_renders_local_assets(self) -> None:
        self.probe("enabled", allowlist=True, selected=True)

    def test_all_installed_templates_compile_and_route_declarations_are_unique(self) -> None:
        result = self.probe("all-templates", allowlist=True)
        self.assertEqual(result["templates"], 83)


if __name__ == "__main__":
    unittest.main()