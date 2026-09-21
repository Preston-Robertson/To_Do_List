"""Offline wheel contracts using disposable installs and synthetic settings."""
from __future__ import annotations

import importlib.util
import json
import os
import runpy
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import unittest
import zipfile
from email.parser import Parser
from pathlib import Path
from unittest import mock
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "example-module"
FEATURES = ("tasks", "discipline", "planning", "media", "cards", "characters", "finance", "assistant", "admin", "preview", "feedback")
LEGACY_ASSETS = {
    "css/cards.css": "cards", "css/collection.css": "cards",
    "js/cards.js": "cards", "js/collection.js": "cards",
    "css/rpg.css": "characters", "js/rpg.js": "characters",
    "js/media_insights.js": "media",
}
SYSTEM_ENVIRONMENT = {
    key: os.environ[key] for key in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR") if key in os.environ
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
            "TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_DATA_HOME",
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


def run_installed_python(code: str, arguments: list[str], directory: Path,
                         environment: dict[str, str], target: Path):
    dependencies = sorted({sysconfig.get_path(name) for name in ("purelib", "platlib")})
    bootstrap = (
        "import json, sys; "
        "sys.path[:0] = json.loads(sys.argv.pop(1)); "
        "exec(compile(sys.argv.pop(1), '<installed-wheel-probe>', 'exec'))"
    )
    return run_python([
        "-I", "-S", "-c", bootstrap,
        json.dumps([str(target), *dependencies]), code, *arguments,
    ], directory, environment)


def build_wheels(directory: Path) -> tuple[Path, Path, dict[str, Path]]:
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
    repositories = []
    for name in FEATURES:
        repository = directory / "module-sources" / name
        shutil.copytree(ROOT / "module-repos" / name, repository, ignore=ignore)
        repositories.append(repository)
    environment = clean_environment(directory)
    for source in (host, example, *repositories):
        run_python([
            "-c", "import sys; from setuptools.build_meta import build_wheel; build_wheel(sys.argv[1])",
            str(wheels),
        ], source, environment)
    return (
        next(wheels.glob("luigi_web-0.2.0-*.whl")),
        next(wheels.glob("luigi_example-0.1.0-*.whl")),
        {name: next(wheels.glob(f"luigi_web_{name}-0.2.0-*.whl")) for name in FEATURES},
    )


INSTALLED_PROBE = "legacy_assets = " + repr(LEGACY_ASSETS) + "\n" + r'''
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
assert "site" not in sys.modules
if case == "data-defaults":
    os.environ.pop("LUIGI_WEB_DATA_DIR")
from luigi_web import paths
assert paths.STATIC_DIR == target / "luigi_web" / "core" / "static"
assert paths.TEMPLATES_DIR == target / "luigi_web" / "core" / "templates"
assert not paths.DATA_DIR.is_relative_to(target)
assert paths.TASK_METADATA_PATH.parent == paths.DATA_DIR
assert paths.GNW_CREDENTIALS_PATH.parent == paths.DATA_DIR

entries = importlib.metadata.entry_points(group="luigi_web.modules")
core_case = case in {"host-only", "help"}
feature_case = case.startswith("feature:")
assert len([entry for entry in entries if entry.name == "example"]) == (0 if core_case or feature_case else 1)
assert "luigi_example" not in sys.modules
result = {"case": case}

if case == "host-only":
    import importlib.util
    from fastapi.testclient import TestClient
    from luigi_web import application
    assert not entries
    assert not application.app.state.modules.catalog
    assert not application.app.state.modules.enabled
    for name in ("tasks", "discipline", "planning", "media", "cards", "characters", "finance", "assistant", "admin", "preview", "feedback"):
        assert importlib.util.find_spec("luigi_web.modules." + name) is None
    assert not any(name in sys.modules for name in ("sqlalchemy", "copilot", "gspread"))
    with TestClient(application.app, follow_redirects=False) as client:
        assert client.get("/login").status_code == 200
        assert client.get("/healthz").status_code == 200
        client.headers["Authorization"] = "Bearer synthetic-packaging-session"
        assert client.get("/modules").status_code == 200
        assert client.get("/tasks").status_code == 404
        assert client.get("/finance").status_code == 404
        assert client.get("/static/css/app.css").status_code == 200
        for filename in legacy_assets:
            assert client.get("/static/" + filename).status_code == 404, filename
elif feature_case:
    import importlib.util
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from luigi_web.core.static_assets import ModuleStaticFiles
    expected = set(json.loads(sys.argv[3]))
    assert {entry.name for entry in entries} == expected
    static_app = FastAPI()
    static_app.mount("/static", ModuleStaticFiles(directory=paths.STATIC_DIR))
    with TestClient(static_app) as static_client:
        for filename, owner in legacy_assets.items():
            response = static_client.get("/static/" + filename)
            assert response.status_code == (200 if owner in expected else 404), filename
    assert not any(name.endswith((".routes", ".repository", ".manifest"))
                   for name in sys.modules if name.startswith("luigi_web.modules."))
    assert not any(name in sys.modules for name in ("sqlalchemy", "psycopg", "ijson", "gspread", "copilot"))
    from luigi_web import application
    registry = application.app.state.modules
    assert {module.id for module in registry.catalog} == expected
    assert {module.id for module in registry.enabled} == expected
    for module_id in expected:
        distribution = importlib.metadata.distribution("luigi-web-" + module_id)
        assert Path(distribution.locate_file("")).resolve().is_relative_to(target)
    for module_id in ("tasks", "discipline", "planning", "media", "cards", "characters", "finance", "assistant", "admin", "preview", "feedback"):
        specification = importlib.util.find_spec("luigi_web.modules." + module_id)
        assert (specification is not None) == (module_id in expected), module_id
    if "tasks" not in expected:
        assert "sqlalchemy" not in sys.modules
        assert "psycopg" not in sys.modules
    for template in application.templates.env.list_templates():
        application.templates.get_template(template)
    registrations = [(method, route.path) for route in application.app.routes
                     for method in getattr(route, "methods", ()) or ()]
    assert len(registrations) == len(set(registrations))
    client = TestClient(application.app, follow_redirects=False)
    try:
        assert client.get("/login").status_code == 200
        assert client.get("/healthz").status_code == 200
        for module in registry.enabled:
            for item in module.navigation:
                assert client.get(item.href).status_code in {303, 401}, item.href
        client.headers["Authorization"] = "Bearer synthetic-packaging-session"
        assert client.get("/modules").status_code == 200
        assert client.get("/static/css/app.css").status_code == 200
        for filename, owner in legacy_assets.items():
            assert client.get("/static/" + filename).status_code == (200 if owner in expected else 404), filename
        from importlib import resources
        for module_id in expected:
            directory = Path(str(resources.files("luigi_web.modules." + module_id))) / "static"
            for resource in directory.rglob("*"):
                if resource.is_file():
                    response = client.get(f"/module-assets/{module_id}/" + resource.relative_to(directory).as_posix())
                    assert response.status_code == 200 and response.content == resource.read_bytes()
    finally:
        client.close()
    result["installed_features"] = sorted(expected)
elif case == "data-defaults":
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
    from importlib import resources
    from fastapi.testclient import TestClient
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
    client = TestClient(application.app, follow_redirects=False)
    static_files = 0
    try:
        for module_id in BUILTIN_MODULE_IDS:
            directory = Path(str(resources.files("luigi_web.modules." + module_id))) / "static"
            for resource in directory.rglob("*"):
                if resource.is_file():
                    url = f"/module-assets/{module_id}/" + resource.relative_to(directory).as_posix()
                    response = client.get(url)
                    assert response.status_code == 200 and response.content == resource.read_bytes(), url
                    static_files += 1
    finally:
        client.close()
    assert static_files > 0
    result.update(templates=len(names) + 1, registrations=len(registrations), module_static_files=static_files)
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
        for filename, owner in legacy_assets.items():
            response = client.get("/static/" + filename)
            expected_asset = target / "luigi_web/modules" / owner / "static/legacy" / filename
            assert response.status_code == 200 and response.content == expected_asset.read_bytes(), filename
            assert client.get(f"/module-assets/{owner}/legacy/" + filename).status_code == 404
    assert not list(Path(os.environ["LUIGI_WEB_DATA_DIR"]).glob("**/*"))

assert not attempts, "Probe attempted private storage or network access"
for name, imported in tuple(sys.modules.items()):
    if name == "luigi_web" or name.startswith("luigi_web."):
        origin = getattr(imported, "__file__", None)
        if origin:
            assert Path(origin).resolve().is_relative_to(target), name
        for package_path in getattr(imported, "__path__", ()):
            assert Path(package_path).resolve().is_relative_to(target), name
result["passed"] = True
print(json.dumps(result))
'''


class InstalledRunnerTests(unittest.TestCase):
    def test_runner_ignores_checkout_and_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luigi-wheel-runner-") as temporary:
            directory = Path(temporary)
            target = directory / "installed"
            target.mkdir()
            environment = clean_environment(directory)
            environment["PYTHONPATH"] = str(ROOT)
            result = run_installed_python(
                "import json, sys; print(json.dumps({'paths': sys.path, 'site': 'site' in sys.modules}))",
                [], directory, environment, target,
            )
            observed = json.loads(result.stdout)
            self.assertEqual(Path(observed["paths"][0]), target)
            self.assertNotIn(str(ROOT), observed["paths"])
            self.assertNotIn("", observed["paths"])
            self.assertFalse(observed["site"])
            self.assertIn(sysconfig.get_path("purelib"), observed["paths"])


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
        cls.host_wheel, cls.example_wheel, cls.module_wheels = build_wheels(cls.directory)
        cls.target = cls.directory / "installed"
        run_python([
            "-m", "pip", "install", "--no-deps", "--no-index", "--no-compile", "--target", str(cls.target),
            str(cls.host_wheel), str(cls.example_wheel), *(str(wheel) for wheel in cls.module_wheels.values()),
        ], cls.directory, clean_environment(cls.directory))
        cls.core_target = cls.directory / "host-only"
        run_python([
            "-m", "pip", "install", "--no-deps", "--no-index", "--no-compile", "--target", str(cls.core_target),
            str(cls.host_wheel),
        ], cls.directory, clean_environment(cls.directory))

    def probe(self, case: str, *, allowlist: bool = False, selected: bool = False):
        target = self.core_target if case in {"host-only", "help"} else self.target
        environment = clean_environment(self.directory, target)
        if case in {"host-only", "help"}:
            environment.pop("LUIGI_WEB_MODULES")
        if allowlist:
            environment["LUIGI_WEB_EXTERNAL_MODULES"] = "example"
        if selected:
            environment["LUIGI_WEB_MODULES"] = "example"
        completed = run_installed_python(
            INSTALLED_PROBE, [str(target), case], self.directory, environment, target,
        )
        result = json.loads(completed.stdout)
        self.assertTrue(result["passed"])
        return result

    def probe_feature(self, name: str):
        installed = set()

        def include(module_id: str) -> None:
            if module_id in installed:
                return
            installed.add(module_id)
            with (ROOT / "module-repos" / module_id / "pyproject.toml").open("rb") as stream:
                dependencies = tomllib.load(stream)["project"]["dependencies"]
            for value in dependencies:
                dependency = Requirement(value)
                if dependency.name.startswith("luigi-web-"):
                    include(dependency.name.removeprefix("luigi-web-"))

        include(name)
        target = self.directory / ("only-" + name)
        environment = clean_environment(self.directory)
        environment.pop("LUIGI_WEB_MODULES")
        run_python([
            "-m", "pip", "install", "--no-deps", "--no-index", "--no-compile", "--target", str(target),
            str(self.host_wheel), *(str(self.module_wheels[module_id]) for module_id in sorted(installed)),
        ], self.directory, environment)
        completed = run_installed_python(
            INSTALLED_PROBE, [str(target), "feature:" + name, json.dumps(sorted(installed))],
            self.directory, environment, target,
        )
        result = json.loads(completed.stdout)
        self.assertTrue(result["passed"])
        self.assertEqual(set(result["installed_features"]), installed)

    def test_host_wheel_contains_exact_resources_and_only_runtime_files(self) -> None:
        with zipfile.ZipFile(self.host_wheel) as wheel:
            names = wheel.namelist()
            for filename in LEGACY_ASSETS:
                self.assertNotIn("luigi_web/core/static/" + filename, names)
            self.assertIn("luigi_web/core/static/css/app.css", names)
            self.assertTrue(all(name.startswith(("luigi_web/", "luigi_web-0.2.0.dist-info/")) for name in names))
            resource_names = set()
            roots = [ROOT / "luigi_web" / "core" / name for name in ("static", "templates")]
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
            self.assertEqual([name for name in names if name.startswith("luigi_web/modules/")], ["luigi_web/modules/__init__.py"])
            for name in names:
                parts = Path(name).parts
                self.assertFalse(any(part.lower().startswith((".env", "local_", "_extract", "_validate")) for part in parts))
                self.assertFalse(set(parts) & {"data", "tests", "scripts", "__pycache__", "examples"})
                self.assertFalse(name.endswith((".db", ".sqlite", ".sqlite3", ".pyc")))
            metadata = Parser().parsestr(wheel.read("luigi_web-0.2.0.dist-info/METADATA").decode())
            with (ROOT / "pyproject.toml").open("rb") as stream:
                requirements = tomllib.load(stream)["project"]["dependencies"]
            self.assertEqual({Requirement(value) for value in metadata.get_all("Requires-Dist") or []},
                             {Requirement(value) for value in requirements})
            self.assertFalse(any(Requirement(value).name.startswith("luigi-web-") for value in requirements))
            self.assertEqual(metadata["Requires-Python"], ">=3.11")

    def test_feature_wheels_have_exact_canonical_resources_and_metadata(self) -> None:
        for name, filename in self.module_wheels.items():
            root = ROOT / "module-repos" / name
            source_root = root / "src"
            with (root / "pyproject.toml").open("rb") as stream:
                project = tomllib.load(stream)["project"]
            with self.subTest(module=name), zipfile.ZipFile(filename) as wheel:
                for asset, owner in LEGACY_ASSETS.items():
                    entry = f"luigi_web/modules/{owner}/static/legacy/{asset}"
                    self.assertEqual(entry in wheel.namelist(), owner == name, entry)
                expected = {source.relative_to(source_root).as_posix(): source
                            for source in (source_root / "luigi_web" / "modules" / name).rglob("*")
                            if source.is_file() and "__pycache__" not in source.parts and source.suffix != ".pyc"}
                self.assertEqual({entry for entry in wheel.namelist() if ".dist-info/" not in entry}, set(expected))
                for entry, source in expected.items():
                    self.assertEqual(wheel.read(entry), source.read_bytes(), entry)
                prefix = f"luigi_web_{name}-0.2.0.dist-info/"
                metadata = Parser().parsestr(wheel.read(prefix + "METADATA").decode())
                self.assertEqual(metadata["Name"], f"luigi-web-{name}")
                self.assertEqual(metadata["Requires-Python"], ">=3.11")
                self.assertEqual({Requirement(value) for value in metadata.get_all("Requires-Dist") or []},
                                 {Requirement(value) for value in project["dependencies"]})
                self.assertIn(f"{name} = luigi_web.modules.{name}.manifest:module",
                              wheel.read(prefix + "entry_points.txt").decode())

    def test_all_standalone_repository_smoke_suites_against_built_wheels(self) -> None:
        environment = clean_environment(self.directory)
        environment["LUIGI_MODULE_WHEEL_DIR"] = str(self.directory / "wheels")
        for name in FEATURES:
            with self.subTest(module=name):
                result = run_python(["-m", "unittest", "discover", "-s", "tests", "-v"],
                                    self.directory / "module-sources" / name, environment)
                self.assertIn("Ran 3 tests", result.stderr)
                self.assertNotIn("skipped", result.stderr)

    def test_host_only_install_has_no_feature_code_or_feature_imports(self) -> None:
        self.probe("host-only")

    def test_finance_only_install(self) -> None:
        self.probe_feature("finance")

    def test_cards_only_install(self) -> None:
        self.probe_feature("cards")

    def test_media_only_install(self) -> None:
        self.probe_feature("media")

    def test_admin_only_install(self) -> None:
        self.probe_feature("admin")

    def test_remaining_features_with_only_declared_feature_dependencies(self) -> None:
        for name in ("tasks", "discipline", "planning", "characters", "assistant", "preview", "feedback"):
            with self.subTest(module=name):
                self.probe_feature(name)

    def test_example_wheel_is_independent_and_has_real_entry_point(self) -> None:
        with zipfile.ZipFile(self.example_wheel) as wheel:
            files = {name for name in wheel.namelist() if not ".dist-info/" in name}
            self.assertEqual(files, {
                "luigi_example/__init__.py", "luigi_example/manifest.py", "luigi_example/routes.py",
                "luigi_example/templates/status.html", "luigi_example/static/status.css",
            })
            metadata = Parser().parsestr(wheel.read("luigi_example-0.1.0.dist-info/METADATA").decode())
            self.assertEqual(
                {Requirement(value) for value in metadata.get_all("Requires-Dist") or []},
                {Requirement("luigi-web>=0.2,<0.3")},
            )
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
        expected = 1
        for filename in (self.host_wheel, *self.module_wheels.values()):
            with zipfile.ZipFile(filename) as wheel:
                expected += sum(name.endswith(".html") for name in wheel.namelist())
        self.assertEqual(result["templates"], expected)


if __name__ == "__main__":
    unittest.main()