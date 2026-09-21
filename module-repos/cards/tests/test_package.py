"""Standalone, data-free package contracts; no host implementation imports."""
from __future__ import annotations

import ast
import configparser
import os
from pathlib import Path
import tomllib
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
with (ROOT / "pyproject.toml").open("rb") as stream:
    CONFIG = tomllib.load(stream)
MODULE_ID = CONFIG["project"]["name"].removeprefix("luigi-web-")
PACKAGE = f"luigi_web.modules.{MODULE_ID}"
SOURCE = ROOT / "src" / "luigi_web" / "modules" / MODULE_ID


class PackageContracts(unittest.TestCase):
    def test_manifest_and_namespace_contract(self):
        self.assertFalse((ROOT / "src/luigi_web/__init__.py").exists())
        self.assertFalse((ROOT / "src/luigi_web/modules/__init__.py").exists())
        project = CONFIG["project"]
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertIn("luigi-web>=0.2,<0.3", project["dependencies"])
        self.assertEqual(project["entry-points"]["luigi_web.modules"], {
            MODULE_ID: PACKAGE + ".manifest:module",
        })
        manifest = ast.parse((SOURCE / "manifest.py").read_text(encoding="utf-8"))
        declaration = next(node.value for node in manifest.body
                           if isinstance(node, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == "module"
                                   for target in node.targets))
        values = {keyword.arg: keyword.value for keyword in declaration.keywords}
        self.assertEqual(ast.literal_eval(values["id"]), MODULE_ID)
        self.assertEqual(ast.literal_eval(values["package"]), PACKAGE)

    def test_all_python_sources_parse_without_importing_application(self):
        sources = list(SOURCE.rglob("*.py"))
        self.assertTrue(sources)
        for source in sources:
            with self.subTest(source=source.relative_to(SOURCE).as_posix()):
                ast.parse(source.read_text(encoding="utf-8-sig"))
        self.assertTrue(list((SOURCE / "templates").rglob("*.html")))

    def test_built_wheel_contains_only_this_feature_and_exact_resources(self):
        wheel_directory = Path(os.environ.get("LUIGI_MODULE_WHEEL_DIR", ROOT / "dist"))
        wheels = list(wheel_directory.glob(CONFIG["project"]["name"].replace("-", "_") + "-*.whl"))
        if not wheels:
            self.skipTest("Build the wheel first to verify packaged resources")
        self.assertEqual(len(wheels), 1)
        expected = {source.relative_to(ROOT / "src").as_posix(): source
                    for source in SOURCE.rglob("*") if source.is_file()
                    and "__pycache__" not in source.parts and source.suffix != ".pyc"}
        with zipfile.ZipFile(wheels[0]) as wheel:
            packaged = {name for name in wheel.namelist() if ".dist-info/" not in name}
            self.assertEqual(packaged, set(expected))
            for name, source in expected.items():
                self.assertEqual(wheel.read(name), source.read_bytes(), name)
            entries = configparser.ConfigParser()
            entry_path = next(name for name in wheel.namelist() if name.endswith("/entry_points.txt"))
            entries.read_string(wheel.read(entry_path).decode())
            self.assertEqual(dict(entries["luigi_web.modules"]), {MODULE_ID: PACKAGE + ".manifest:module"})


if __name__ == "__main__":
    unittest.main()