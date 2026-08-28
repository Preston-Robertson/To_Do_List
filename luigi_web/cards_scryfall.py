"""Scryfall bulk catalog refresh for the isolated trading-card database."""
from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

import httpx

from . import cards

logger = logging.getLogger("luigi_web.cards.scryfall")

BULK_INDEX_URL = "https://api.scryfall.com/bulk-data"
BULK_KINDS = ("oracle_cards", "default_cards", "unique_artwork", "all_cards")
_DOWNLOAD_HOSTS = {"data.scryfall.io"}
_MAX_BULK_BYTES = 2_000_000_000
_MAX_JSONL_LINE_BYTES = 10_000_000
_HEADERS = {
    "User-Agent": "LuigiWeb/1.0 (+https://github.com/Preston-Robertson/To_Do_List)",
    "Accept": "application/json;q=0.9,*/*;q=0.8",
}

_refresh_lock = threading.Lock()
_state_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,
    "phase": None,
    "current": 0,
    "total": None,
    "message": "",
    "started_at": None,
    "finished_at": None,
    "result": None,
}
_scheduler_stop = threading.Event()
_scheduler_thread: threading.Thread | None = None


def refresh_hours() -> int:
    try:
        return max(0, int(os.environ.get("LUIGI_WEB_CARDS_REFRESH_HOURS", "0")))
    except ValueError:
        return 0


def bulk_kind() -> str:
    configured = os.environ.get("LUIGI_WEB_CARDS_SCRYFALL_BULK", "default_cards").strip()
    return configured if configured in BULK_KINDS else "default_cards"


def _bulk_dir() -> Path:
    configured = os.environ.get("LUIGI_WEB_CARDS_BULK_DIR", "").strip()
    path = Path(configured).expanduser().resolve() if configured else cards.db_path().parent / "cards-bulk"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _trusted_url(value: Any, hosts: set[str]) -> str:
    url = str(value or "").strip()
    if not url or any(char in url for char in "\"'()\\\r\n\t"):
        raise ValueError("Scryfall returned an untrusted download URL")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Scryfall returned an untrusted download URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        raise ValueError("Scryfall returned an untrusted download URL")
    return url


def _bulk_meta(kind: str) -> dict[str, Any]:
    if kind not in BULK_KINDS:
        raise ValueError("unsupported Scryfall bulk dataset")
    with httpx.Client(headers=_HEADERS, timeout=30, follow_redirects=False) as client:
        response = client.get(BULK_INDEX_URL)
        response.raise_for_status()
        payload = response.json()
    for entry in payload.get("data", []):
        if entry.get("type") == kind:
            _download_spec(entry)
            return entry
    raise ValueError(f"Scryfall dataset {kind!r} was not found")


def _download_spec(metadata: dict[str, Any]) -> tuple[str, int | None, str]:
    """Normalize current JSONL and legacy JSON-array manifest entries."""
    if metadata.get("jsonl_download_uri"):
        url = _trusted_url(metadata["jsonl_download_uri"], _DOWNLOAD_HOSTS)
        size = int(metadata.get("compressed_size") or 0) or None
        suffix = ".jsonl.gz"
    elif metadata.get("download_uri"):
        url = _trusted_url(metadata["download_uri"], _DOWNLOAD_HOSTS)
        size = int(metadata.get("size") or 0) or None
        suffix = ".json"
    else:
        raise ValueError("Scryfall bulk metadata has no supported download URL")
    if size and size > _MAX_BULK_BYTES:
        raise ValueError("Scryfall bulk dataset exceeds the configured safety limit")
    return url, size, suffix


def _download(
    url: str,
    destination: Path,
    progress: Callable[[int, int | None], None] | None = None,
) -> None:
    trusted = _trusted_url(url, _DOWNLOAD_HOSTS)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    downloaded = 0
    try:
        with httpx.Client(headers=_HEADERS, timeout=None, follow_redirects=False) as client:
            with client.stream("GET", trusted) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length", "0") or 0) or None
                if total and total > _MAX_BULK_BYTES:
                    raise ValueError("Scryfall bulk download exceeds the safety limit")
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=1 << 20):
                        downloaded += len(chunk)
                        if downloaded > _MAX_BULK_BYTES:
                            raise ValueError("Scryfall bulk download exceeds the safety limit")
                        handle.write(chunk)
                        if progress:
                            progress(downloaded, total)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _iter_bulk(path: Path) -> Iterable[dict[str, Any]]:
    if path.name.endswith(".jsonl.gz"):
        with gzip.open(path, "rb") as handle:
            for raw_line in handle:
                if len(raw_line) > _MAX_JSONL_LINE_BYTES:
                    raise ValueError("Scryfall JSONL record exceeds the safety limit")
                if raw_line.strip():
                    payload = json.loads(raw_line)
                    if not isinstance(payload, dict):
                        raise ValueError("Scryfall JSONL record must be an object")
                    yield payload
        return
    try:
        import ijson
    except ImportError as exc:  # pragma: no cover - deployment dependency check
        raise RuntimeError("ijson is required for Scryfall catalog refreshes") from exc
    with path.open("rb") as handle:
        yield from ijson.items(handle, "item")


def _update_state(**values: Any) -> None:
    with _state_lock:
        _state.update(values)


def refresh_state() -> dict[str, Any]:
    with _state_lock:
        return dict(_state)


def refresh_mtg(
    kind: str | None = None,
    progress: Callable[[str, int, int | None], None] | None = None,
) -> dict[str, Any]:
    """Download, stream-import, snapshot prices, and discard the raw bulk file."""
    selected = kind or bulk_kind()
    if selected not in BULK_KINDS:
        raise ValueError("unsupported Scryfall bulk dataset")
    if not _refresh_lock.acquire(blocking=False):
        raise RuntimeError("a Scryfall refresh is already running")

    started = time.monotonic()
    refresh_id: int | None = None
    destination: Path | None = None
    imported = 0
    snapshots = 0
    error: str | None = None
    try:
        refresh_id = cards.refresh_start("mtg", f"scryfall:{selected}")
        metadata = _bulk_meta(selected)
        download_url, size_hint, suffix = _download_spec(metadata)
        destination = _bulk_dir() / f"scryfall-{selected}{suffix}"
        if progress:
            progress("download", 0, size_hint)
        _download(
            download_url,
            destination,
            lambda current, total: progress("download", current, total or size_hint)
            if progress else None,
        )

        if progress:
            progress("import", 0, None)
        batch: list[dict[str, Any]] = []
        for payload in _iter_bulk(destination):
            batch.append(payload)
            if len(batch) >= 500:
                imported += cards.upsert_scryfall_cards(batch)
                batch.clear()
                if progress:
                    progress("import", imported, None)
        if batch:
            imported += cards.upsert_scryfall_cards(batch)
        snapshots = cards.snapshot_prices("mtg")
    except Exception as exc:  # noqa: BLE001 - persisted and surfaced in Admin
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("Scryfall refresh failed")
    finally:
        try:
            if destination is not None:
                for path in (
                    destination,
                    destination.with_suffix(destination.suffix + ".part"),
                ):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.exception("Could not remove Scryfall bulk file")
                        if error is None:
                            error = f"{type(exc).__name__}: temporary file cleanup failed"
            if refresh_id is not None:
                try:
                    cards.refresh_finish(refresh_id, imported, error)
                except Exception as exc:  # noqa: BLE001 - lock must still release
                    logger.exception("Could not finish Scryfall refresh audit")
                    if error is None:
                        error = f"{type(exc).__name__}: refresh audit failed"
        finally:
            _refresh_lock.release()
    return {
        "cards_upserted": imported,
        "prices_snapshotted": snapshots,
        "error": error,
        "seconds": round(time.monotonic() - started, 2),
    }


def _refresh_worker(kind: str) -> None:
    _update_state(
        running=True,
        phase="starting",
        current=0,
        total=None,
        message="Starting Scryfall refresh",
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        finished_at=None,
        result=None,
    )

    def progress(phase: str, current: int, total: int | None) -> None:
        _update_state(phase=phase, current=current, total=total)

    try:
        result = refresh_mtg(kind, progress)
        message = (
            f"Imported {result['cards_upserted']:,} cards"
            if not result.get("error") else str(result["error"])
        )
        _update_state(result=result, message=message)
    except Exception as exc:  # refresh lock contention or validation before audit starts
        _update_state(
            result={"error": f"{type(exc).__name__}: {exc}"},
            message=f"{type(exc).__name__}: {exc}",
        )
    finally:
        _update_state(
            running=False,
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )


def start_refresh(kind: str | None = None) -> bool:
    selected = kind or bulk_kind()
    if selected not in BULK_KINDS:
        raise ValueError("unsupported Scryfall bulk dataset")
    with _state_lock:
        if _state["running"]:
            return False
        _state["running"] = True
    worker = threading.Thread(
        target=_refresh_worker,
        args=(selected,),
        name="luigi-cards-scryfall-refresh",
        daemon=True,
    )
    worker.start()
    return True


def refresh_is_due(hours: int | None = None) -> bool:
    interval = refresh_hours() if hours is None else max(0, int(hours))
    if interval <= 0:
        return False
    latest = cards.last_refresh("mtg")
    if not latest or latest.get("status") != "ok" or not latest.get("finished_at"):
        return True
    try:
        finished = datetime.fromisoformat(str(latest["finished_at"])).replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - finished >= timedelta(hours=interval)


def _scheduler() -> None:
    while not _scheduler_stop.wait(60):
        if refresh_is_due():
            start_refresh()


def start_scheduler() -> bool:
    global _scheduler_thread
    if refresh_hours() <= 0:
        return False
    if _scheduler_thread and _scheduler_thread.is_alive():
        return False
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler,
        name="luigi-cards-scryfall-scheduler",
        daemon=True,
    )
    _scheduler_thread.start()
    if refresh_is_due():
        start_refresh()
    return True


def stop_scheduler() -> None:
    _scheduler_stop.set()