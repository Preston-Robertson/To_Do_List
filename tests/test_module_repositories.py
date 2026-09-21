"""Source-layout discovery without installing or executing repository code."""
import importlib.util
from importlib import resources
from pathlib import Path
import tomllib
import unittest

from packaging.requirements import Requirement

from luigi_web import modules

ROOT = Path(__file__).resolve().parents[1]
FEATURES = ("tasks", "discipline", "planning", "media", "cards", "characters", "finance", "assistant", "admin", "preview", "feedback")


class ModuleRepositoryLayoutTests(unittest.TestCase):
    def test_host_and_editable_requirements_keep_features_separate(self):
        with (ROOT / "pyproject.toml").open("rb") as stream:
            configuration = tomllib.load(stream)
        project = configuration["project"]
        self.assertEqual(project["name"], "luigi-web")
        self.assertEqual(project["version"], "0.2.0")
        self.assertFalse(any(Requirement(value).name.startswith("luigi-web-")
                             for value in project["dependencies"]))
        requirements = (ROOT / "requirements-modules.txt").read_text(encoding="utf-8").splitlines()
        actual = [line.strip() for line in requirements if line.strip() and not line.startswith("#")]
        self.assertEqual(actual, ["-e .", *("-e ./module-repos/" + name for name in FEATURES)])

    def test_tasks_explicitly_declares_database_dependencies(self):
        with (ROOT / "module-repos/tasks/pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)["project"]
        dependencies = {requirement.name.lower(): requirement
                        for requirement in map(Requirement, project["dependencies"])}
        self.assertEqual(dependencies["sqlalchemy"], Requirement("sqlalchemy==2.0.34"))
        self.assertEqual(dependencies["psycopg"], Requirement("psycopg[binary]==3.2.13"))

    def test_feature_namespace_preserves_current_imports(self):
        for name in FEATURES:
            with self.subTest(module=name):
                specification = importlib.util.find_spec(f"luigi_web.modules.{name}")
                self.assertIsNotNone(specification)
                self.assertTrue(specification.submodule_search_locations)
                expected = ROOT / "module-repos" / name / "src" / "luigi_web" / "modules" / name
                self.assertEqual(Path(specification.origin).parent, expected)
                self.assertFalse((ROOT / "luigi_web" / "modules" / name).exists())

    def test_checkout_paths_are_only_explicit_feature_source_roots(self):
        root = Path(__file__).resolve().parents[1]
        for entry in modules.__path__:
            path = Path(entry).resolve()
            if path.is_relative_to(root / "module-repos"):
                self.assertEqual(path.parts[-3:], ("src", "luigi_web", "modules"))
                self.assertFalse(path.is_symlink())

    def test_each_repository_declares_only_its_own_namespace_and_resources(self):
        for name in FEATURES:
            with self.subTest(module=name):
                root = ROOT / "module-repos" / name
                with (root / "pyproject.toml").open("rb") as stream:
                    configuration = tomllib.load(stream)
                package = f"luigi_web.modules.{name}"
                project = configuration["project"]
                self.assertEqual(project["name"], f"luigi-web-{name}")
                self.assertEqual(project["version"], "0.2.0")
                self.assertEqual(project["requires-python"], ">=3.11")
                self.assertIn("luigi-web>=0.2,<0.3", project["dependencies"])
                self.assertEqual(project["entry-points"]["luigi_web.modules"], {name: package + ".manifest:module"})
                finding = configuration["tool"]["setuptools"]["packages"]["find"]
                self.assertTrue(finding["namespaces"])
                self.assertEqual(finding["include"], [package + "*"])
                self.assertEqual(set(configuration["tool"]["setuptools"]["package-data"]), {package})
                for parent in (root / "src/luigi_web", root / "src/luigi_web/modules"):
                    self.assertFalse((parent / "__init__.py").exists())
                for scaffold in ("README.md", ".gitignore", ".github/workflows/release.yml", "tests/test_package.py"):
                    self.assertTrue((root / scaffold).is_file(), scaffold)

    def test_distribution_dependencies_are_separate_from_ui_selection(self):
        expected = {
            "discipline": {"tasks"}, "planning": {"tasks", "discipline"},
            "assistant": {"tasks", "discipline", "media"},
        }
        for name in FEATURES:
            with (ROOT / "module-repos" / name / "pyproject.toml").open("rb") as stream:
                dependencies = tomllib.load(stream)["project"]["dependencies"]
            actual = {dependency.removeprefix("luigi-web-").split(">")[0]
                      for dependency in dependencies if dependency.startswith("luigi-web-")}
            self.assertEqual(actual, expected.get(name, set()), name)

    def test_shared_loader_finds_all_extracted_templates(self):
        from luigi_web.core.templating import create_templates

        templates = create_templates()
        actual = set(templates.env.list_templates())
        for name in FEATURES:
            directory = Path(str(resources.files(f"luigi_web.modules.{name}"))) / "templates"
            for template in directory.rglob("*.html"):
                relative = template.relative_to(directory).as_posix()
                self.assertIn(relative, actual)
                templates.env.parse(templates.env.loader.get_source(templates.env, relative)[0])

    def test_legacy_assets_are_owned_only_by_feature_packages(self):
        from luigi_web.core.static_assets import legacy_asset_path
        from luigi_web.paths import STATIC_DIR

        assets = {
            "cards": ("css/cards.css", "css/collection.css", "js/cards.js", "js/collection.js"),
            "characters": ("css/rpg.css", "js/rpg.js"),
            "media": ("js/media_insights.js",),
        }
        for owner, filenames in assets.items():
            directory = ROOT / "module-repos" / owner / "src/luigi_web/modules" / owner / "static/legacy"
            for filename in filenames:
                with self.subTest(owner=owner, filename=filename):
                    self.assertTrue((directory / filename).is_file())
                    self.assertEqual(legacy_asset_path(filename), directory / filename)
                    self.assertFalse((STATIC_DIR / filename).exists())
        self.assertTrue((STATIC_DIR / "css/app.css").is_file())