"""Shared shell context with namespaced templates and local module assets."""
from __future__ import annotations

from importlib import resources
from importlib import import_module
from pathlib import Path
import sys
from typing import Any
from weakref import WeakSet

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader, PrefixLoader

from ..paths import PACKAGE_DIR, STATIC_DIR, TEMPLATES_DIR
from .module_registry import BUILTIN_MODULE_IDS, ModuleRegistry, NavigationItem

_TEMPLATES: WeakSet[Jinja2Templates] = WeakSet()


def asset_version() -> str:
    roots = [STATIC_DIR, *(PACKAGE_DIR / "modules" / name / "static" for name in BUILTIN_MODULE_IDS)]
    return str(int(max((
        path.stat().st_mtime for root in roots if root.exists()
        for path in root.rglob("*") if path.is_file()
    ), default=0)))


def shell_context(request: Request) -> dict[str, Any]:
    application = request.scope.get("app")
    if application is None:
        application = getattr(sys.modules.get("luigi_web.application"), "app", None)
    registry = getattr(getattr(application, "state", None), "modules", None)
    if registry is None:
        registry = ModuleRegistry([])
    groups = [(label, list(items)) for label, items in registry.navigation()]
    system = next((items for label, items in groups if label == "System"), None)
    modules_item = NavigationItem("modules", "Modules", "/modules", "blocks", "System")
    if system is None:
        groups.append(("System", [modules_item]))
    else:
        system.insert(0, modules_item)
    order = {"Focus": 0, "Planning": 1, "Library": 2, "Private": 3, "Workspace": 4, "System": 5}
    groups.sort(key=lambda group: order.get(group[0], 4))
    return {
        "navigation_groups": groups,
        "module_enabled": registry.is_enabled,
        "module_count": len(registry.enabled),
        "landing_path": registry.landing_path,
        "shell_asset_version": _ASSET_VERSION,
    }


_ASSET_VERSION = asset_version()


def create_templates(*, package: str = "", namespace: str = "") -> Jinja2Templates:
    directories = [str(TEMPLATES_DIR)]
    directories.extend(str(PACKAGE_DIR / "modules" / name / "templates") for name in BUILTIN_MODULE_IDS)
    loaders: list[Any] = [FileSystemLoader(directories)]
    if package:
        if not namespace:
            raise ValueError("External templates require a namespace")
        loaders.append(PrefixLoader({
            namespace: FileSystemLoader(str(resources.files(package).joinpath("templates"))),
        }))
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[shell_context])
    templates.env.loader = ChoiceLoader(loaders)
    templates.env.globals["asset_version"] = _ASSET_VERSION
    _TEMPLATES.add(templates)
    return templates


def configure_module_templates(registry: ModuleRegistry) -> None:
    for module in registry.enabled:
        if module.template_setup:
            module_name, attribute = module.template_setup.split(":", 1)
            setup = getattr(import_module(module_name), attribute)
            for templates in _TEMPLATES:
                setup(templates.env)


def mount_module_assets(app: FastAPI, registry: ModuleRegistry) -> None:
    for module in registry.enabled:
        if not module.package:
            continue
        directory = Path(str(resources.files(module.package).joinpath("static")))
        if directory.is_dir():
            app.mount(
                f"/module-assets/{module.id}",
                StaticFiles(directory=str(directory)),
                name=f"module-assets-{module.id}",
            )