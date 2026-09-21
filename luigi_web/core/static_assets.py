"""Compatibility URLs for a fixed set of installed feature assets."""
from __future__ import annotations

from importlib import resources
import os
from pathlib import Path

from starlette.staticfiles import StaticFiles

_LEGACY_ASSETS = {
    "css/cards.css": "cards",
    "css/collection.css": "cards",
    "js/cards.js": "cards",
    "js/collection.js": "cards",
    "css/rpg.css": "characters",
    "js/rpg.js": "characters",
    "js/media_insights.js": "media",
}


def _lookup_legacy_asset(path: str) -> tuple[str, os.stat_result | None]:
    owner = _LEGACY_ASSETS.get(path.replace(os.sep, "/"))
    if owner is None:
        return "", None
    package = f"luigi_web.modules.{owner}"
    try:
        directory = resources.files(package).joinpath("static", "legacy")
    except ModuleNotFoundError as error:
        if error.name == package or package.startswith(f"{error.name}."):
            return "", None
        raise
    if not isinstance(directory, Path):
        return "", None
    return StaticFiles(directory=directory, check_dir=False).lookup_path(path)


def legacy_asset_path(path: str) -> Path | None:
    full_path, stat_result = _lookup_legacy_asset(path)
    return Path(full_path) if stat_result is not None else None


class ModuleStaticFiles(StaticFiles):
    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        full_path, stat_result = super().lookup_path(path)
        if stat_result is not None:
            return full_path, stat_result
        return _lookup_legacy_asset(path)