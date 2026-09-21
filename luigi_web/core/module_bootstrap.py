"""Select verified release paths once per process, before feature imports."""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from pkgutil import extend_path

_lock = threading.RLock()
_records: tuple[dict, ...] | None = None


def staged_modules() -> tuple[dict, ...]:
    global _records
    with _lock:
        if _records is None:
            from . import module_repositories

            verified = module_repositories.installed_modules()
            paths = [str(module_repositories._storage_path(*str(record["site_path"]).split("/"))) for record in verified]
            for path in reversed(paths):
                if path not in sys.path:
                    sys.path.insert(0, path)
            parent = sys.modules.get("luigi_web")
            if parent is not None:
                parent.__path__ = extend_path(parent.__path__, "luigi_web")
            _records = tuple(verified)
        return _records


def feature_paths() -> list[str]:
    from . import module_repositories

    paths = []
    for record in staged_modules():
        root = module_repositories._storage_path(*str(record["site_path"]).split("/"))
        directory = root / "luigi_web" / "modules"
        if directory.is_dir():
            paths.append(str(directory))
    return paths