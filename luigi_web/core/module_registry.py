"""Declarative feature selection without importing feature implementations."""
from __future__ import annotations

import inspect
import logging
import os
import re
from dataclasses import dataclass, replace
from importlib import import_module, metadata
from typing import Any, Iterable

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

MODULE_API_VERSION = 1
ENTRY_POINT_GROUP = "luigi_web.modules"
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
BUILTIN_MODULE_IDS = (
    "tasks", "discipline", "planning", "media", "cards", "characters", "finance",
    "assistant", "admin", "preview", "feedback",
)
logger = logging.getLogger("luigi_web.modules")


class ModuleConfigurationError(ValueError):
    """The configured module set cannot be mounted safely."""


@dataclass(frozen=True)
class NavigationItem:
    key: str
    label: str
    href: str
    icon: str
    group: str = "Workspace"
    searchable: bool = True

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.key) or not _IDENTIFIER.fullmatch(self.icon):
            raise ModuleConfigurationError("Navigation keys and icons must be identifiers")
        if not self.href.startswith("/") or self.href.startswith("//"):
            raise ModuleConfigurationError("Module navigation must use local absolute paths")
        if any(character in self.href for character in ("?", "#", "\\", "\r", "\n")):
            raise ModuleConfigurationError("Module navigation cannot contain URL parameters")


@dataclass(frozen=True)
class Module:
    id: str
    label: str
    description: str
    router: str
    navigation: tuple[NavigationItem, ...] = ()
    requires: tuple[str, ...] = ()
    startup: str = ""
    shutdown: str = ""
    template_setup: str = ""
    package: str = ""
    api_version: int = MODULE_API_VERSION
    source: str = "Built-in"

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.id):
            raise ModuleConfigurationError("Module IDs must be lowercase identifiers")
        if self.api_version != MODULE_API_VERSION:
            raise ModuleConfigurationError(f"Unsupported module API version for {self.id}")
        for reference in (self.router, self.startup, self.shutdown, self.template_setup):
            if reference and (reference.count(":") != 1 or not all(reference.split(":"))):
                raise ModuleConfigurationError(f"Invalid entry point for {self.id}")


class ModuleRegistry:
    def __init__(self, modules: Iterable[Module], selection: str | None = None) -> None:
        catalog: dict[str, Module] = {}
        for module in modules:
            if module.id in catalog:
                raise ModuleConfigurationError(f"Duplicate module ID: {module.id}")
            catalog[module.id] = module
        self.catalog = tuple(catalog.values())
        if selection is None:
            selected = set(catalog)
        elif selection.strip().lower() == "none":
            selected = set()
        else:
            names = [name.strip() for name in selection.split(",")]
            if any(not _IDENTIFIER.fullmatch(name) for name in names):
                raise ModuleConfigurationError("Use comma-separated module IDs or 'none'")
            if len(set(names)) != len(names):
                raise ModuleConfigurationError("A module was selected more than once")
            selected = set(names)
        unknown = selected.difference(catalog)
        if unknown:
            raise ModuleConfigurationError(f"Unknown modules: {', '.join(sorted(unknown))}")

        ordered: list[Module] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(module_id: str) -> None:
            if module_id in visiting:
                raise ModuleConfigurationError(f"Circular module dependency: {module_id}")
            if module_id in visited:
                return
            visiting.add(module_id)
            module = catalog[module_id]
            for dependency in module.requires:
                if dependency not in selected:
                    raise ModuleConfigurationError(f"Module {module_id} requires {dependency}")
                visit(dependency)
            visiting.remove(module_id)
            visited.add(module_id)
            ordered.append(module)

        for module_id in catalog:
            if module_id in selected:
                visit(module_id)

        keys: set[str] = set()
        paths: set[str] = set()
        for module in ordered:
            for item in module.navigation:
                if item.key in keys or item.href in paths:
                    raise ModuleConfigurationError("Enabled modules have conflicting navigation")
                keys.add(item.key)
                paths.add(item.href)
        self.enabled = tuple(ordered)
        self._enabled_ids = frozenset(module.id for module in ordered)

    def is_enabled(self, module_id: str) -> bool:
        return module_id in self._enabled_ids

    @property
    def landing_path(self) -> str:
        if self.is_enabled("planning"):
            return "/home"
        for module in self.enabled:
            if module.navigation:
                return module.navigation[0].href
        return "/modules"

    def navigation(self) -> tuple[tuple[str, tuple[NavigationItem, ...]], ...]:
        groups: dict[str, list[NavigationItem]] = {}
        for module in self.enabled:
            for item in module.navigation:
                groups.setdefault(item.group, []).append(item)
        return tuple((label, tuple(items)) for label, items in groups.items())


def _load_reference(reference: str) -> Any:
    module_name, attribute = reference.split(":", 1)
    return getattr(import_module(module_name), attribute)


def discover_modules() -> tuple[Module, ...]:
    modules = [
        import_module(f"luigi_web.modules.{module_id}.manifest").module
        for module_id in BUILTIN_MODULE_IDS
    ]
    approved = os.environ.get("LUIGI_WEB_EXTERNAL_MODULES", "").strip()
    if not approved:
        return tuple(modules)
    names = [name.strip() for name in approved.split(",")]
    if len(names) != len(set(names)) or any(not _IDENTIFIER.fullmatch(name) for name in names):
        raise ModuleConfigurationError("Invalid external module allow-list")
    available = metadata.entry_points(group=ENTRY_POINT_GROUP)
    for name in names:
        matches = [entry for entry in available if entry.name == name]
        if len(matches) != 1:
            raise ModuleConfigurationError(f"Expected one installed entry point for {name}")
        try:
            module = matches[0].load()
        except Exception:
            raise ModuleConfigurationError(f"Unable to load approved module {name}") from None
        if not isinstance(module, Module) or module.id != name:
            raise ModuleConfigurationError(f"Invalid module manifest for {name}")
        if not module.package:
            raise ModuleConfigurationError(f"External module {name} must declare its package")
        modules.append(replace(module, source="External package"))
    return tuple(modules)


def build_registry(selection: str | None = None) -> ModuleRegistry:
    from .module_settings import read_selection

    if selection is None:
        selection = os.environ.get("LUIGI_WEB_MODULES")
    if selection is None:
        selection = read_selection()
    if selection is None:
        selection = ",".join(BUILTIN_MODULE_IDS)
    return ModuleRegistry(discover_modules(), selection)


def _availability_dependency(module_id: str, app: FastAPI):
    def require_available() -> None:
        state = getattr(app.state, "module_status", {}).get(module_id)
        if state in {"unavailable", "blocked"}:
            raise HTTPException(503, "Module is unavailable")

    return require_available


def mount_modules(app: FastAPI, registry: ModuleRegistry) -> None:
    from ..auth import require_auth

    prepared: list[tuple[Module, APIRouter]] = []
    registered = [("core", route) for route in app.routes if getattr(route, "methods", None)]
    reserved = ("/login", "/logout", "/healthz", "/static", "/modules", "/module-assets", "/command-palette")
    for module in registry.enabled:
        try:
            router = _load_reference(module.router)
        except Exception:
            raise ModuleConfigurationError(f"Unable to load routes for {module.id}") from None
        if not isinstance(router, APIRouter):
            raise ModuleConfigurationError(f"Module {module.id} must export an APIRouter")
        if router.on_startup or router.on_shutdown:
            raise ModuleConfigurationError(f"Module {module.id} must use manifest lifecycle hooks")
        for route in router.routes:
            if not isinstance(route, APIRoute):
                raise ModuleConfigurationError("Modules must expose HTTP API routes only")
            if route.path == "/" or any(
                route.path == prefix or route.path.startswith(prefix + "/") for prefix in reserved
            ):
                raise ModuleConfigurationError(f"Module {module.id} claims a reserved route")
            if module.source != "Built-in":
                prefix = f"/extensions/{module.id}"
                if route.path != prefix and not route.path.startswith(prefix + "/"):
                    raise ModuleConfigurationError("External routes must use their extension namespace")
            for owner, existing in registered:
                if not set(route.methods or ()).intersection(existing.methods or ()):
                    continue
                same_pattern = re.sub(r"\{[^}]+\}", "{}", route.path) == re.sub(r"\{[^}]+\}", "{}", existing.path)
                if (
                    same_pattern
                    or (owner != module.id and (
                        route.path_regex.match(existing.path)
                        or existing.path_regex.match(route.path)
                        or route.path_regex.pattern == existing.path_regex.pattern
                    ))
                ):
                    raise ModuleConfigurationError(f"Conflicting route in module {module.id}")
            registered.append((module.id, route))
        prepared.append((module, router))

    app.state.modules = registry
    app.state.module_status = {module.id: "enabled" for module in registry.enabled}
    app.state.module_cleanup = []
    for module, router in prepared:
        app.include_router(router, dependencies=[
            Depends(require_auth), Depends(_availability_dependency(module.id, app)),
        ])
    from .templating import configure_module_templates, mount_module_assets
    configure_module_templates(registry)
    mount_module_assets(app, registry)


async def _call_hook(reference: str, app: FastAPI) -> None:
    hook = _load_reference(reference)
    if inspect.iscoroutinefunction(hook):
        await hook(app)
    else:
        result = await run_in_threadpool(hook, app)
        if inspect.isawaitable(result):
            await result


async def start_modules(app: FastAPI) -> None:
    for module in app.state.modules.enabled:
        if any(app.state.module_status.get(dependency) in {"unavailable", "blocked"} for dependency in module.requires):
            app.state.module_status[module.id] = "blocked"
            continue
        if module.shutdown:
            app.state.module_cleanup.append(module)
        try:
            if module.startup:
                await _call_hook(module.startup, app)
            app.state.module_status[module.id] = "ready"
        except Exception:
            app.state.module_status[module.id] = "unavailable"
            logger.error("Module startup failed: %s", module.id)


async def stop_modules(app: FastAPI) -> None:
    for module in reversed(app.state.module_cleanup):
        try:
            await _call_hook(module.shutdown, app)
        except Exception:
            logger.error("Module shutdown failed: %s", module.id)
    app.state.module_cleanup.clear()