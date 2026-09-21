"""Fresh-process activation contracts using only disposable synthetic wheels."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_module_installer import WheelFixture, build_wheel

from luigi_web.core import module_installer as installer


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-activation-session"


HOST_PROBE = r'''
import importlib.abc
import json
import os
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

site, checkout = (Path(value).resolve() for value in sys.argv[1:3])
options = json.loads(sys.argv[3])
data = Path(os.environ["LUIGI_WEB_DATA_DIR"]).resolve()
prefix = Path(sys.prefix).resolve()
sys.path[:] = [str(site), *options.get("additional_sites", [])] + [entry for entry in sys.path if entry and (
    not Path(entry).resolve().is_relative_to(checkout)
    or Path(entry).resolve().is_relative_to(prefix)
)]
sys.meta_path[:] = [finder for finder in sys.meta_path
                   if not getattr(finder, "__module__", "").startswith("__editable__")]
sys.dont_write_bytecode = True

class NoDomainImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"sqlalchemy", "psycopg", "psycopg2", "gspread", "google", "pandas", "dotenv"}:
            raise AssertionError("Activation attempted a domain dependency")
        return None

def audit(event, arguments):
    if event in {"subprocess.Popen", "os.system"}:
        raise AssertionError("Activation attempted a process")
    if event == "sqlite3.connect":
        filename = os.fsdecode(arguments[0])
        path = Path(url2pathname(urlsplit(filename).path) if filename.startswith("file:") else filename).resolve()
        if path != data / "module-installations.db":
            raise AssertionError("Activation attempted non-fixture storage")
    if event == "open" and isinstance(arguments[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(arguments[0])).resolve()
        if path.name.lower().startswith(".env"):
            raise AssertionError("Activation attempted an environment file")
        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"} and not path.is_relative_to(data):
            raise AssertionError("Activation attempted non-fixture storage")
    if event in {"socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto"}:
        frame = sys._getframe()
        while frame is not None:
            if frame.f_code.co_name in {"socketpair", "_socketpair", "_fallback_socketpair"} and frame.f_globals.get("__name__") == "socket":
                return
            frame = frame.f_back
        raise AssertionError("Activation attempted network access")

sys.meta_path.insert(0, NoDomainImports())
sys.addaudithook(audit)

from fastapi.testclient import TestClient
try:
    from luigi_web.application import app
except Exception as error:
    if "error" not in options:
        raise
    assert type(error).__name__ == options["error_type"], type(error).__name__
    assert str(error) == options["error"], "unexpected public import error"
    print(json.dumps({"error": str(error)}), flush=True)
    raise SystemExit(0)
assert "error" not in options, "invalid staged code failed open"
from luigi_web import application
from luigi_web.core.module_bootstrap import staged_modules
assert Path(application.__file__).resolve().is_relative_to(site)
assert len(app.state.modules.catalog) == len({module.id for module in app.state.modules.catalog})
initial_records = staged_modules()
if options.get("expected_root"):
    from importlib import import_module
    for suffix in ("", ".manifest", ".routes"):
        imported = import_module(options["package"] + suffix)
        assert Path(imported.__file__).resolve().is_relative_to(Path(options["expected_root"]).resolve()), "wrong package origin"
if options.get("additional_sites"):
    from importlib import metadata
    distributions = list(metadata.distributions(path=[options["expected_root"], *options["additional_sites"]]))
    assert len([distribution for distribution in distributions
                if any(entry.name == options["module_id"] for entry in distribution.entry_points)]) == 2

class ModuleControls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.identifiers = []

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == "input" and values.get("type") == "checkbox" and values.get("name") == "enabled":
            self.identifiers.append(values.get("value"))

client = TestClient(app, follow_redirects=False)
try:
    assert client.get("/modules").status_code == 401
    route = options["route"]
    active = options.get("active", True)
    anonymous = client.get(route)
    assert anonymous.status_code == (401 if active else 404), ("route authentication", anonymous.status_code)
    client.headers["Authorization"] = "Bearer " + os.environ["LUIGI_WEB_UI_TOKEN"]
    response = client.get(route)
    assert response.status_code == (200 if active else 404), ("route status", response.status_code)
    if active:
        assert options["marker"] in response.text, "wrong release rendered"
    asset = client.get(options["asset"])
    assert asset.status_code == (200 if active else 404), ("asset status", asset.status_code)
    if active:
        assert options["marker"] in asset.text, "wrong release asset"
        if options["module_id"] == "media":
            legacy_asset = client.get("/static/js/media_insights.js")
            assert legacy_asset.status_code == 200, ("legacy asset status", legacy_asset.status_code)
            assert options["marker"] in legacy_asset.text, "legacy asset came from a different release"
    modules = client.get("/modules")
    assert modules.status_code == 200
    assert modules.headers["cache-control"] == "no-store"
    controls = ModuleControls()
    controls.feed(modules.text)
    assert controls.identifiers == [module.id for module in app.state.modules.catalog], "duplicate or missing Modules UI entry"
    if any(module.id == options["module_id"] for module in app.state.modules.catalog):
        assert options["source"] in modules.text, "module source missing"
    assert client.get("/finance").status_code == 404
    assert client.get("/tasks").status_code == 404
    print(json.dumps({
        "enabled": [module.id for module in app.state.modules.enabled],
        "catalog": [module.id for module in app.state.modules.catalog],
        "sources": {module.id: module.source for module in app.state.modules.catalog},
        "marker": options["marker"],
        "versions": {record["module_id"]: record["version"] for record in initial_records},
    }), flush=True)
    if options.get("keep_alive"):
        for command in sys.stdin:
            if command.strip() == "stop":
                break
            assert command.strip() == "recheck", "unknown synthetic probe command"
            from luigi_web.core.module_registry import build_registry
            assert staged_modules() is initial_records, "bootstrap cache changed in a running process"
            assert build_registry().enabled == app.state.modules.enabled
            response = client.get(route)
            assert response.status_code == 200 and options["marker"] in response.text, "running route hot-loaded"
            asset = client.get(options["asset"])
            assert asset.status_code == 200 and options["marker"] in asset.text, "running resource hot-loaded"
            print(json.dumps({"marker": options["marker"], "versions": {
                record["module_id"]: record["version"] for record in staged_modules()
            }}), flush=True)
finally:
    client.close()
'''


def synthetic_wheel(*, module_id="example", version="1.0", marker="EXTERNAL_V1", broken="", package_error="", distribution=None):
    package = f"luigi_web.modules.{module_id}" if module_id == "media" else f"luigi_web_extensions.{module_id}"
    directory = package.replace(".", "/")
    route = "/games" if module_id == "media" else f"/extensions/{module_id}"
    manifest = (
        "from luigi_web.core.module_registry import Module, NavigationItem\n"
        f"module = Module(id={module_id!r}, label='Synthetic activation', description='Synthetic module', "
        f"package={package!r}, router={package + '.routes:router'!r}, "
        f"navigation=(NavigationItem({module_id!r}, 'Synthetic activation', {route!r}, 'blocks'),))\n"
    )
    routes = (
        "from fastapi import APIRouter, Request\n"
        "from luigi_web.core.templating import create_templates\n"
        "from . import SOURCE\n"
        f"templates = create_templates(package={package!r}, namespace={module_id!r})\n"
        "router = APIRouter()\n"
        f"@router.get({route!r})\n"
        "def page(request: Request):\n"
        f"    return templates.TemplateResponse(request=request, name={module_id + '/activation.html'!r}, context={{'source': SOURCE}})\n"
    )
    initializer = (
        "import os\nfrom pathlib import Path\n"
        f"SOURCE = {marker!r}\n"
        "with Path(os.environ['ACTIVATION_EXECUTION_LOG']).open('a', encoding='utf-8') as stream:\n"
        "    stream.write(SOURCE + '\\n')\n"
    )
    files = {
        f"{directory}/__init__.py": (initializer + package_error).encode(),
        f"{directory}/manifest.py": manifest.encode(),
        f"{directory}/routes.py": (broken or routes).encode(),
        f"{directory}/templates/activation.html": b"<!doctype html><title>Synthetic activation</title><main>{{ source }}</main>",
        f"{directory}/static/activation.css": f"/* {marker} */\nmain {{ color: #123456; }}\n".encode(),
    }
    if module_id == "media":
        files[f"{directory}/static/legacy/js/media_insights.js"] = f"globalThis.syntheticMediaSource = {json.dumps(marker)};\n".encode()
    return build_wheel(
        version=version, package=package, distribution=distribution or f"luigi-web-{module_id}", module_id=module_id,
        requirements=("luigi-web>=0.2",) if module_id == "media" else (), extra_files=files,
    )


class ModuleActivationTests(WheelFixture):
    def setUp(self):
        super().setUp()
        self.site = self.root / "host"
        self.data = self.root / "data"
        self.data.mkdir()
        self.execution_log = self.root / "executed.txt"
        self.environment = {
            key: value for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR"}
        }
        self.environment.update({key: str(self.root) for key in (
            "TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR", "APPDATA", "LOCALAPPDATA", "HOME", "USERPROFILE",
        )})
        self.environment.update(
            LUIGI_WEB_DATA_DIR=str(self.data),
            LUIGI_WEB_MODULES_FILE=str(self.data / "modules.json"),
            LUIGI_WEB_MODULE_REPOSITORY_OWNERS="example-owner",
            LUIGI_WEB_UI_TOKEN=TOKEN,
            ACTIVATION_EXECUTION_LOG=str(self.execution_log),
            PYTHON_DOTENV_DISABLED="1",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONWARNINGS="error::ResourceWarning",
        )
        self.start_patch(patch.dict(os.environ, self.environment, clear=True))
        from luigi_web.core import module_repositories
        self.start_patch(patch.object(module_repositories, "DATA_DIR", self.data))
        self.register()
        package = self.site / "luigi_web"
        package.mkdir(parents=True)
        for name in ("__init__.py", "application.py", "auth.py", "clock.py", "paths.py"):
            shutil.copy2(ROOT / "luigi_web" / name, package / name)
        shutil.copytree(ROOT / "luigi_web/core", package / "core", ignore=shutil.ignore_patterns("__pycache__"))
        (package / "modules").mkdir()
        shutil.copy2(ROOT / "luigi_web/modules/__init__.py", package / "modules/__init__.py")
        shutil.copy2(ROOT / "pyproject.toml", self.site / "pyproject.toml")

    def stage(self, *, module_id="example", version="1.0", marker="EXTERNAL_V1", broken="", package_error=""):
        if module_id != self.release_source:
            self.select_module(module_id)
        self.release_version = version
        self.content = synthetic_wheel(module_id=module_id, version=version, marker=marker, broken=broken, package_error=package_error)
        before = self.execution_log.read_bytes() if self.execution_log.exists() else b""
        job = self.queued_wheel()
        result = installer.apply_job(job["id"])
        self.assertEqual(result["state"], "installed")
        self.assertTrue(result["restart_required"])
        after = self.execution_log.read_bytes() if self.execution_log.exists() else b""
        self.assertEqual(before, after, "staging executed module code")
        record = next(record for record in installer.installed_modules() if record["module_id"] == module_id)
        return self.data / record["site_path"]

    def probe_arguments(self, *, selection="example", marker="EXTERNAL_V1", module_id="example", environment=None, **overrides):
        variables = dict(self.environment)
        if selection is not None:
            variables["LUIGI_WEB_MODULES"] = selection
        variables.update(environment or {})
        options = {
            "route": "/games" if module_id == "media" else f"/extensions/{module_id}",
            "asset": f"/module-assets/{module_id}/activation.css",
            "marker": marker,
            "source": "Built-in" if module_id == "media" else "External package",
            "module_id": module_id,
            "package": "luigi_web.modules.media" if module_id == "media" else "luigi_web_extensions.example",
        }
        options.update(overrides)
        return [sys.executable, "-I", "-B", "-c", HOST_PROBE, str(self.site), str(ROOT), json.dumps(options)], variables

    def probe(self, **options):
        command, environment = self.probe_arguments(**options)
        result = subprocess.run(
            command, cwd=self.root, env=environment, capture_output=True, text=True, timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ResourceWarning", result.stderr)
        return json.loads(result.stdout)

    def fallback(self, *, module_id="media", installed=False, competing=False):
        marker = "OLD_INSTALLED" if installed else "SOURCE_CHECKOUT"
        content = synthetic_wheel(module_id=module_id, version="0.1", marker=marker,
                                  distribution=f"legacy-{module_id}-provider" if competing else None)
        if installed:
            root = self.root / "old-site"
        else:
            (self.site / "app.py").write_text("", encoding="utf-8")
            root = self.site / "module-repos" / module_id / "src"
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for name in archive.namelist():
                if installed or ".dist-info/" not in name:
                    destination = root / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(name))
        return root

    def test_staged_external_wheel_activates_real_host_routes_templates_and_assets(self):
        root = self.stage()
        result = self.probe(expected_root=str(root))
        self.assertEqual(result["enabled"], ["example"])
        self.assertEqual(result["catalog"], ["example"])
        self.assertEqual(result["sources"], {"example": "External package"})
        self.assertEqual(self.execution_log.read_text(encoding="utf-8").splitlines(), ["EXTERNAL_V1"])
        self.assertEqual(len(self.requests), 2)

    def test_external_approval_does_not_enable_it_by_default_or_override_none(self):
        self.stage()
        for selection in (None, "none"):
            with self.subTest(selection=selection):
                result = self.probe(selection=selection, active=False)
                self.assertEqual(result["enabled"], [])
                self.assertEqual(result["catalog"], ["example"])

    def test_only_installed_known_features_are_enabled_by_default(self):
        self.stage()
        root = self.stage(module_id="media", version="0.2", marker="STAGED_MEDIA")
        result = self.probe(selection=None, module_id="media", marker="STAGED_MEDIA", expected_root=str(root))
        self.assertEqual(result["enabled"], ["media"])
        self.assertEqual(result["catalog"], ["media", "example"])
        self.assertEqual(result["sources"], {"media": "Built-in", "example": "External package"})
        result = self.probe(selection="none", module_id="media", active=False)
        self.assertEqual(result["enabled"], [])

    def test_staged_known_feature_overrides_fixed_checkout_source(self):
        self.fallback()
        root = self.stage(module_id="media", version="0.2", marker="STAGED_MEDIA")
        result = self.probe(selection="media", module_id="media", marker="STAGED_MEDIA", expected_root=str(root))
        self.assertEqual(result["catalog"], ["media"])
        self.assertEqual(self.execution_log.read_text(encoding="utf-8").splitlines(), ["STAGED_MEDIA"])

    def test_staged_known_feature_overrides_old_installed_distribution(self):
        old = self.fallback(installed=True)
        root = self.stage(module_id="media", version="0.2", marker="STAGED_MEDIA")
        result = self.probe(selection="media", module_id="media", marker="STAGED_MEDIA", expected_root=str(root), additional_sites=[str(old)])
        self.assertEqual(result["catalog"], ["media"])
        self.assertEqual(self.execution_log.read_text(encoding="utf-8").splitlines(), ["STAGED_MEDIA"])

    def test_staged_external_overrides_old_installed_distribution(self):
        old = self.fallback(module_id="example", installed=True)
        root = self.stage()
        result = self.probe(expected_root=str(root), additional_sites=[str(old)])
        self.assertEqual(result["catalog"], ["example"])
        self.assertEqual(self.execution_log.read_text(encoding="utf-8").splitlines(), ["EXTERNAL_V1"])

    def test_staged_record_filters_competing_entry_points_by_distribution_root(self):
        old = self.fallback(module_id="example", installed=True, competing=True)
        root = self.stage()
        result = self.probe(expected_root=str(root), additional_sites=[str(old)])
        self.assertEqual(result["catalog"], ["example"])
        self.assertEqual(self.execution_log.read_text(encoding="utf-8").splitlines(), ["EXTERNAL_V1"])

    def test_staged_known_feature_filters_competing_reserved_entry_points(self):
        old = self.fallback(installed=True, competing=True)
        root = self.stage(module_id="media", version="0.2", marker="STAGED_MEDIA")
        result = self.probe(selection="media", module_id="media", marker="STAGED_MEDIA", expected_root=str(root), additional_sites=[str(old)])
        self.assertEqual(result["catalog"], ["media"])
        self.assertEqual(self.execution_log.read_text(encoding="utf-8").splitlines(), ["STAGED_MEDIA"])

    def test_revoked_owner_excludes_external_code_and_discovery(self):
        self.stage()
        result = self.probe(selection=None, active=False, environment={"LUIGI_WEB_MODULE_REPOSITORY_OWNERS": ""})
        self.assertEqual(result["catalog"], [])
        self.assertEqual(result["enabled"], [])
        self.assertFalse(self.execution_log.exists())

    def test_revoked_owner_can_fall_back_to_known_checkout_source(self):
        source = self.fallback()
        self.stage(module_id="media", version="0.2", marker="STAGED_MEDIA")
        result = self.probe(selection="media", module_id="media", marker="SOURCE_CHECKOUT", expected_root=str(source),
                            environment={"LUIGI_WEB_MODULE_REPOSITORY_OWNERS": ""})
        self.assertEqual(result["enabled"], ["media"])
        self.assertEqual(self.execution_log.read_text(encoding="utf-8").splitlines(), ["SOURCE_CHECKOUT"])

    def test_tampered_known_feature_fails_closed_before_checkout_fallback(self):
        self.fallback()
        root = self.stage(module_id="media", version="0.2", marker="STAGED_MEDIA")
        with (root / "luigi_web/modules/media/__init__.py").open("a", encoding="utf-8") as stream:
            stream.write("\nSOURCE = 'TAMPERED'\n")
        self.probe(selection="media", module_id="media", error_type="ModuleInstallError", error="Installed modules could not be verified")
        self.assertFalse(self.execution_log.exists())

    def test_tampered_external_fails_closed_before_old_installed_fallback(self):
        old = self.fallback(module_id="example", installed=True)
        root = self.stage()
        with (root / "luigi_web_extensions/example/routes.py").open("a", encoding="utf-8") as stream:
            stream.write("\nraise RuntimeError('SYNTHETIC_PRIVATE_IMPORT_DETAIL')\n")
        self.probe(additional_sites=[str(old)], error_type="ModuleInstallError", error="Installed modules could not be verified")
        self.assertFalse(self.execution_log.exists())

    def test_active_external_package_import_failure_is_generic(self):
        self.stage(package_error="raise RuntimeError('SYNTHETIC_PRIVATE_IMPORT_DETAIL')\n")
        self.probe(error_type="ModuleConfigurationError", error="Unable to load approved module example")

    def test_active_external_router_import_failure_is_generic(self):
        self.stage(broken="raise RuntimeError('SYNTHETIC_PRIVATE_IMPORT_DETAIL')\n")
        self.probe(error_type="ModuleConfigurationError", error="Unable to load routes for example")

    def test_release_update_requires_a_fresh_process_for_code_and_resources(self):
        old_root = self.stage()
        executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(executor.shutdown, wait=True)
        command, environment = self.probe_arguments(expected_root=str(old_root), keep_alive=True)
        process = subprocess.Popen(command, cwd=self.root, env=environment, text=True,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def cleanup():
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

        self.addCleanup(cleanup)
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None

        def receive():
            line = executor.submit(process.stdout.readline).result(timeout=45)
            if not line:
                self.fail(process.stderr.read())
            return json.loads(line)

        before = receive()
        self.assertEqual(before["versions"], {"example": "1.0"})
        self.assertEqual(before["marker"], "EXTERNAL_V1")
        new_root = self.stage(version="2.0", marker="EXTERNAL_V2")
        self.assertNotEqual(new_root, old_root)
        process.stdin.write("recheck\n")
        process.stdin.flush()
        running = receive()
        self.assertEqual(running["versions"], {"example": "1.0"})
        self.assertEqual(running["marker"], "EXTERNAL_V1")
        fresh = self.probe(marker="EXTERNAL_V2", expected_root=str(new_root))
        self.assertEqual(fresh["versions"], {"example": "2.0"})
        self.assertEqual(fresh["marker"], "EXTERNAL_V2")
        self.assertEqual(self.execution_log.read_text(encoding="utf-8").splitlines(), ["EXTERNAL_V1", "EXTERNAL_V2"])
        self.assertEqual(len(self.requests), 4)
        output, errors = process.communicate("stop\n", timeout=30)
        self.assertEqual(process.returncode, 0, errors)
        self.assertNotIn("ResourceWarning", errors)
        self.assertEqual(output, "")