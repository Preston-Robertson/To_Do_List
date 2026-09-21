"""Availability and compatibility imports for the known optional features."""
from __future__ import annotations

from importlib import import_module, util
from types import ModuleType
from typing import Any

FEATURE_MODULE_IDS = (
    "tasks", "discipline", "planning", "media", "cards", "characters", "finance",
    "assistant", "admin", "preview", "feedback",
)


class ModuleUnavailable(ImportError):
    """A requested optional feature package is not installed."""


def module_available(module_name: str) -> bool:
    parts = module_name.split(".")
    if len(parts) < 3 or parts[:2] != ["luigi_web", "modules"] or parts[2] not in FEATURE_MODULE_IDS:
        raise ValueError("Expected a known feature module")
    for length in range(2, len(parts) + 1):
        if util.find_spec(".".join(parts[:length])) is None:
            return False
    return True


def require_module(module_name: str) -> ModuleType:
    if not module_available(module_name):
        feature = module_name.split(".")[2]
        raise ModuleUnavailable(f"Optional module {feature} is unavailable; install luigi-web-{feature}")
    return import_module(module_name)


class ModuleProxy:
    """Resolve a missing compatibility import only when its API is requested."""

    def __init__(self, module_name: str) -> None:
        self._module_name = module_name

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(require_module(self._module_name), name)


def optional_module(module_name: str) -> ModuleType | ModuleProxy:
    if module_available(module_name):
        return import_module(module_name)
    return ModuleProxy(module_name)