"""Offline checks for the fixed legacy asset compatibility mount."""
from importlib import resources
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from luigi_web.core.static_assets import ModuleStaticFiles, legacy_asset_path
from luigi_web.paths import STATIC_DIR


class ModuleStaticAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="module-static-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = FastAPI()
        self.app.mount("/static", ModuleStaticFiles(directory=self.root), name="static")
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_installed_cards_asset_preserves_file_responses(self):
        expected = resources.files("luigi_web.modules.cards").joinpath("static/legacy/css/cards.css")
        self.assertFalse((STATIC_DIR / "css/cards.css").exists())
        response = self.client.get("/static/css/cards.css")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, expected.read_bytes())
        self.assertIn("text/css", response.headers["content-type"])
        head = self.client.head("/static/css/cards.css")
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        self.assertEqual(head.headers["etag"], response.headers["etag"])
        cached = self.client.get("/static/css/cards.css", headers={"If-None-Match": response.headers["etag"]})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(self.client.post("/static/css/cards.css").status_code, 405)

    def test_host_file_takes_priority_without_resolving_a_module(self):
        directory = self.root / "css"
        directory.mkdir()
        (directory / "cards.css").write_text("synthetic host asset", encoding="utf-8")
        with patch("luigi_web.core.static_assets.resources.files") as files:
            self.assertEqual(self.client.get("/static/css/cards.css").text, "synthetic host asset")
        files.assert_not_called()

    def test_all_seven_assets_belong_to_their_installed_module(self):
        assets = {
            "cards": ("css/cards.css", "css/collection.css", "js/cards.js", "js/collection.js"),
            "characters": ("css/rpg.css", "js/rpg.js"),
            "media": ("js/media_insights.js",),
        }
        for owner, filenames in assets.items():
            directory = resources.files(f"luigi_web.modules.{owner}").joinpath("static", "legacy")
            for filename in filenames:
                with self.subTest(owner=owner, filename=filename):
                    expected = directory.joinpath(filename)
                    self.assertFalse((STATIC_DIR / filename).exists())
                    self.assertEqual(legacy_asset_path(filename), expected.resolve())
                    response = self.client.get("/static/" + filename)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, expected.read_bytes())

    def test_missing_package_or_parent_is_404(self):
        for missing in ("luigi_web.modules.cards", "luigi_web.modules"):
            with self.subTest(missing=missing), patch(
                "luigi_web.core.static_assets.resources.files",
                side_effect=ModuleNotFoundError(name=missing),
            ):
                self.assertEqual(self.client.get("/static/css/cards.css").status_code, 404)
                self.assertIsNone(legacy_asset_path("css/cards.css"))

    def test_installed_package_without_asset_is_404(self):
        with patch("luigi_web.core.static_assets.resources.files", return_value=self.root):
            self.assertEqual(self.client.get("/static/css/cards.css").status_code, 404)
            self.assertIsNone(legacy_asset_path("css/cards.css"))

    def test_feature_cache_versions_use_packaged_legacy_assets(self):
        from importlib import import_module

        for owner, basename in (("cards", "cards"), ("characters", "rpg")):
            with self.subTest(owner=owner):
                directory = resources.files(f"luigi_web.modules.{owner}").joinpath("static", "legacy")
                expected = str(int(max(directory.joinpath(f"{extension}/{basename}.{extension}").stat().st_mtime
                                       for extension in ("css", "js"))))
                routes = import_module(f"luigi_web.modules.{owner}.routes")
                self.assertEqual(routes._asset_version(), expected)
                self.assertNotEqual(expected, "0")

    def test_unrelated_import_errors_are_not_hidden(self):
        with patch("luigi_web.core.static_assets.resources.files", side_effect=ModuleNotFoundError(name="unexpected_dependency")):
            with self.assertRaises(ModuleNotFoundError):
                legacy_asset_path("css/cards.css")

    def test_only_exact_allowlisted_names_resolve_packages(self):
        with patch("luigi_web.core.static_assets.resources.files") as files:
            for path in ("css/Cards.css", "css/unknown.css", "cards/routes.py", "legacy/css/cards.css", "../css/cards.css"):
                with self.subTest(path=path):
                    self.assertIsNone(legacy_asset_path(path))
                    self.assertEqual(self.client.get("/static/" + path).status_code, 404)
        files.assert_not_called()

    def test_encoded_traversal_cannot_expose_package_source(self):
        for path in ("%2e%2e/application.py", "css/%2e%2e/%2e%2e/routes.py", "css/%2e%2e%5c%2e%2e%5croutes.py", "%252e%252e/routes.py"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get("/static/" + path).status_code, 404)


if __name__ == "__main__":
    unittest.main()