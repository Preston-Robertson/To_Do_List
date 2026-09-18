"""Atomic module selection storage, separate from secrets and feature data."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from ..paths import DATA_DIR
from .module_registry import ModuleConfigurationError

_LOCK = threading.Lock()
_MAX_BYTES = 16384


def settings_path() -> Path:
    value = os.environ.get("LUIGI_WEB_MODULES_FILE", "").strip()
    return Path(value).expanduser() if value else DATA_DIR / "modules.json"


def read_selection() -> str | None:
    path = settings_path()
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise ModuleConfigurationError("Module settings are not readable") from None
    try:
        if len(raw) > _MAX_BYTES:
            raise ValueError
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError
        enabled = payload["enabled"]
        if not isinstance(enabled, list) or any(not isinstance(item, str) for item in enabled):
            raise ValueError
        return ",".join(enabled) if enabled else "none"
    except (ValueError, KeyError, TypeError):
        raise ModuleConfigurationError("Module settings are invalid") from None


def save_selection(enabled: list[str]) -> None:
    from .module_registry import ModuleRegistry, discover_modules

    if "LUIGI_WEB_MODULES" in os.environ:
        raise ModuleConfigurationError("Module selection is managed by the deployment environment")
    registry = ModuleRegistry(discover_modules(), ",".join(enabled) if enabled else "none")
    selected = [module.id for module in registry.enabled]
    payload = json.dumps({"version": 1, "enabled": selected}, indent=2) + "\n"
    path = settings_path()
    temporary_path: Path | None = None
    with _LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                temporary_path = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            if read_selection() != (",".join(selected) if selected else "none"):
                raise OSError("Selection verification failed")
        except OSError:
            raise ModuleConfigurationError("Module settings could not be saved") from None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)