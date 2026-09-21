"""Source-deployment dependencies must cover the modular host contract."""
from pathlib import Path
import tomllib
import unittest

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


class SourceRequirementsTests(unittest.TestCase):
    def test_source_installs_all_host_dependencies_at_compatible_pins(self):
        root = Path(__file__).resolve().parents[1]
        requirements = {}
        for line in (root / "requirements.txt").read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                requirement = Requirement(line)
                requirements[canonicalize_name(requirement.name)] = requirement
        with (root / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)
        for value in project["project"]["dependencies"]:
            declared = Requirement(value)
            name = canonicalize_name(declared.name)
            with self.subTest(dependency=name):
                self.assertIn(name, requirements)
                installed = requirements[name]
                self.assertTrue(declared.extras.issubset(installed.extras))
                pins = list(installed.specifier)
                self.assertEqual(len(pins), 1)
                self.assertEqual(pins[0].operator, "==")
                self.assertIn(pins[0].version, declared.specifier)
