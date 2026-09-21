"""Pokemon TCG API catalog refresh for the isolated card database."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from . import repository as cards

logger = logging.getLogger("luigi_web.cards.pokemon")

API_URL = "https://api.pokemontcg.io/v2/cards"
PAGE_SIZE = 250
MAX_PAGES = 1_000
MAX_CARDS = PAGE_SIZE * MAX_PAGES
MAX_PAGE_BYTES = 25_000_000

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


def _headers() -> dict[str, str]:
    headers = {
        "User-Agent": "LuigiWeb/1.0 (+https://github.com/Preston-Robertson/To_Do_List)",
        "Accept": "application/json",
    }
    api_key = os.environ.get("LUIGI_WEB_CARDS_POKEMON_API_KEY", "").strip()
    if api_key:
        headers["X-Api-Key"] = api_key
    return headers


def _fetch_page(page: int) -> dict[str, Any]:
    if page < 1 or page > MAX_PAGES:
        raise ValueError("Pokemon catalog page exceeds the safety limit")
    with httpx.Client(headers=_headers(), timeout=60, follow_redirects=False) as client:
        with client.stream(
            "GET", API_URL, params={"page": page, "pageSize": PAGE_SIZE}
        ) as response:
            response.raise_for_status()
            advertised = int(response.headers.get("content-length", "0") or 0)
            if advertised > MAX_PAGE_BYTES:
                raise ValueError("Pokemon API page exceeds the response safety limit")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > MAX_PAGE_BYTES:
                    raise ValueError("Pokemon API page exceeds the response safety limit")
    payload = json.loads(content)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Pokemon TCG API returned an invalid response")
    total = int(payload.get("totalCount") or len(payload["data"]))
    if total < 0 or total > MAX_CARDS:
        raise ValueError("Pokemon catalog exceeds the safety limit")
    return payload


def refresh_state() -> dict[str, Any]:
    with _state_lock:
        return dict(_state)


def _update_state(**values: Any) -> None:
    with _state_lock:
        _state.update(values)


def refresh_pokemon(
    progress: Callable[[int, int | None], None] | None = None,
) -> dict[str, Any]:
    """Fetch every provider page in bounded batches and snapshot prices."""
    if not _refresh_lock.acquire(blocking=False):
        raise RuntimeError("a Pokemon catalog refresh is already running")
    started = time.monotonic()
    refresh_id: int | None = None
    imported = 0
    snapshots = 0
    error: str | None = None
    try:
        refresh_id = cards.refresh_start("pokemon", "pokemontcg.io:v2")
        page = 1
        total: int | None = None
        while page <= MAX_PAGES:
            payload = _fetch_page(page)
            rows = payload["data"]
            total = int(payload.get("totalCount") or len(rows))
            if not rows:
                break
            imported += cards.upsert_pokemon_cards(rows)
            if progress:
                progress(imported, total)
            if imported >= total:
                break
            page += 1
        else:  # pragma: no cover - safety limit guard
            raise ValueError("Pokemon catalog exceeded the page safety limit")
        snapshots = cards.snapshot_prices("pokemon")
    except Exception as exc:  # noqa: BLE001 - persisted and surfaced in Card Data
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("Pokemon catalog refresh failed")
    finally:
        try:
            if refresh_id is not None:
                cards.refresh_finish(refresh_id, imported, error)
        except Exception as exc:  # noqa: BLE001 - lock must still release
            logger.exception("Could not finish Pokemon refresh audit")
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


def _refresh_worker() -> None:
    _update_state(
        running=True,
        phase="import",
        current=0,
        total=None,
        message="Starting Pokemon catalog refresh",
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        finished_at=None,
        result=None,
    )

    def progress(current: int, total: int | None) -> None:
        _update_state(current=current, total=total)

    try:
        result = refresh_pokemon(progress)
        message = (
            f"Imported {result['cards_upserted']:,} cards"
            if not result.get("error") else str(result["error"])
        )
        _update_state(result=result, message=message)
    except Exception as exc:
        _update_state(
            result={"error": f"{type(exc).__name__}: {exc}"},
            message=f"{type(exc).__name__}: {exc}",
        )
    finally:
        _update_state(
            running=False,
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )


def start_refresh() -> bool:
    with _state_lock:
        if _state["running"]:
            return False
        _state["running"] = True
    worker = threading.Thread(
        target=_refresh_worker,
        name="luigi-cards-pokemon-refresh",
        daemon=True,
    )
    try:
        worker.start()
    except Exception:
        _update_state(running=False)
        raise
    return True