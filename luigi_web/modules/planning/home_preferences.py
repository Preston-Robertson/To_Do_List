"""Lazy, record-free Home layout preferences for this single-user application."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from threading import RLock
from typing import Any

from ... import paths

VERSION = 1
MAX_BYTES = 4096
WIDGET_IDS = (
    "overdue", "upcoming", "open-tasks", "disc-pending", "gnw-playing",
    "gnw-watching", "disc-streaks", "follow-ups", "recent-done", "weekly-review",
    "disc-week", "task-week", "activity",
)
_LOCK = RLock()


class LayoutValidationError(ValueError):
    def __init__(self) -> None:
        super().__init__("Invalid Home layout.")


class LayoutStorageError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Home layout is unavailable.")


def defaults() -> dict[str, Any]:
    return {"version": VERSION, "order": list(WIDGET_IDS), "hidden": [], "pinned": []}


def validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"version", "order", "hidden", "pinned"}:
        raise LayoutValidationError()
    if type(value["version"]) is not int or value["version"] != VERSION:
        raise LayoutValidationError()
    result: dict[str, Any] = {"version": VERSION}
    for field in ("order", "hidden", "pinned"):
        items = value[field]
        if not isinstance(items, list) or len(items) > len(WIDGET_IDS):
            raise LayoutValidationError()
        if any(not isinstance(item, str) or item not in WIDGET_IDS for item in items):
            raise LayoutValidationError()
        if len(set(items)) != len(items):
            raise LayoutValidationError()
        result[field] = list(items)
    result["order"].extend(item for item in WIDGET_IDS if item not in result["order"])
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LayoutValidationError()
        result[key] = value
    return result


def decode(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BYTES:
        raise LayoutValidationError()
    try:
        return validate(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object))
    except (ValueError, UnicodeError, RecursionError):
        raise LayoutValidationError() from None


def _read_bytes(path: Path) -> bytes | None:
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise LayoutStorageError()
        return raw
    except FileNotFoundError:
        return None
    except OSError:
        raise LayoutStorageError() from None


def _stored(raw: bytes) -> dict[str, Any]:
    try:
        return decode(raw)
    except LayoutValidationError:
        raise LayoutStorageError() from None


def load() -> dict[str, Any] | None:
    with _LOCK:
        raw = _read_bytes(paths.DATA_DIR / "home-layout.json")
        return None if raw is None else _stored(raw)


def _atomic_write(path: Path, raw: bytes) -> None:
    temporary: Path | None = None
    try:
        descriptor, filename = tempfile.mkstemp(prefix=".home-layout-", suffix=".tmp", dir=path.parent)
        temporary = Path(filename)
        with os.fdopen(descriptor, "wb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        if _read_bytes(temporary) != raw:
            raise LayoutStorageError()
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def save(value: Any) -> dict[str, Any]:
    layout = validate(value)
    raw = json.dumps(layout, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    with _LOCK:
        path = paths.DATA_DIR / "home-layout.json"
        try:
            previous = _read_bytes(path)
            if previous is not None:
                _stored(previous)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, raw)
            try:
                committed = _read_bytes(path)
                if committed is None or _stored(committed) != layout:
                    raise LayoutStorageError()
            except (OSError, LayoutStorageError):
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, previous)
                raise LayoutStorageError() from None
        except (OSError, LayoutStorageError):
            raise LayoutStorageError() from None
    return layout