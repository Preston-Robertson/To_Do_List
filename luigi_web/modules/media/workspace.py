"""Confirmed library workflows over Sheets and isolated local media history."""
from __future__ import annotations

from collections import OrderedDict
import json
import random
import re
import secrets
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ...auth import require_auth
from . import history, service, steam

router = APIRouter(dependencies=[Depends(require_auth)])
_mutation_lock = threading.RLock()
_undo: OrderedDict[str, dict[str, Any]] = OrderedDict()
_picks: OrderedDict[str, float] = OrderedDict()
_NO_STORE = {"Cache-Control": "no-store"}


def _section(section: str) -> str:
    if section not in {"games", "shows"}:
        raise HTTPException(404, "Unknown media section", headers=_NO_STORE)
    return section


def _identity(form) -> tuple[str, str]:
    profile, title = str(form.get("profile") or "").strip(), str(form.get("title") or "").strip()
    if not profile or not title or len(profile) > 256 or len(title) > 512:
        raise ValueError("Invalid media identity")
    return profile, title


def _version(form) -> str:
    version = str(form.get("expected_version") or "")
    if not re.fullmatch(r"[a-f0-9]{64}", version):
        raise ValueError("A current media version is required")
    return version


def _current(section: str, profile: str, title: str):
    return service._matching_row(section, service._all_values(section, force=True), profile, title)[3]


def _prune() -> None:
    now = time.monotonic()
    for token in [token for token, entry in _undo.items() if entry["expires"] <= now]:
        _undo.pop(token, None)
    while len(_undo) >= 256:
        _undo.popitem(last=False)
    for key in [key for key, stamp in _picks.items() if now - stamp >= 86400]:
        _picks.pop(key, None)
    while len(_picks) >= 512:
        _picks.popitem(last=False)


def library_state(section: str, profile: str = "", *, force: bool = False):
    _section(section)
    if len(profile) > 256:
        raise ValueError("Invalid profile")
    with _mutation_lock:
        state = service.library_state(section, profile, force=force)
        if force:
            warnings = []
            for item in state["items"]:
                try:
                    recovered = history.reconcile(section, item)
                    if recovered.get("warning"):
                        warnings.append(recovered["warning"])
                except history.HistoryError:
                    warnings.append("Local media history is unavailable")
            state["history_warning"] = warnings[0] if warnings else None
        return state


def change(section: str, profile: str, title: str, fields: dict[str, Any],
           expected_version: str | None, *, kind: str = "update", restore=None,
           undo_event_id: int | None = None):
    _section(section)
    with _mutation_lock:
        headers, _, raw_row, before = service._matching_row(
            section, service._all_values(section, force=True), profile, title,
        )
        if expected_version is not None and before["version"] != expected_version:
            raise service.MediaConflict("Media changed")
        history.reconcile(section, before)
        if kind == "episode":
            if section != "shows" or before["status"] in {"completed", "dropped"}:
                raise ValueError("Update the show status before logging another episode")
            fields = {"current_episode": (before.get("current_episode") or 0) + 1}
        if kind == "replay":
            if before["status"] not in {"completed", "achievements", "dropped"}:
                raise ValueError("Only finished or dropped items can start another run")
            fields = {"status": service.ACTIVE_STATUS[section]}
            if section == "shows":
                fields.update(current_season=1, current_episode=0)
        editable = service.GAME_EDITABLE if section == "games" else service.SHOW_EDITABLE
        cells = service._validated_fields(section, fields) if restore is None else restore
        header_index = service._header_index(headers)
        if any(header not in header_index for header in cells):
            raise ValueError("A required media column is unavailable")
        if all(service._row_value(raw_row, header_index[header]) == value for header, value in cells.items()):
            return {"ok": True, "item": before, "undo_token": None,
                    "undo_ttl_ms": 12000, "history_warning": None}
        reverse = {value: key for key, value in editable.items()}
        reverse.update({"Date Started": "date_started", "Date Completed": "date_completed"})
        expected = {reverse[key]: value for key, value in cells.items()}
        if "tags" in expected:
            expected["tags"] = [tag.strip() for tag in expected["tags"].split(",") if tag.strip()]
        operation_id = history.prepare_change(section, before, kind, expected_fields=expected, undo_event_id=undo_event_id)
        try:
            result = service.write_item(section, profile, title, fields,
                                        expected_version=before["version"], restore_cells=restore)
        except Exception as exc:
            try:
                history.fail_change(operation_id, uncertain=not isinstance(exc, (ValueError, service.MediaConflict)))
            except history.HistoryError:
                pass
            raise
        warning, event_id = None, None
        try:
            if result["changed"]:
                event_id = history.finish_change(operation_id, result["item"])
            else:
                history.fail_change(operation_id)
        except history.HistoryError:
            warning = "Library saved; local history is unconfirmed. Refresh before another change."
        token = None
        if event_id is not None and kind != "undo":
            _prune()
            token = secrets.token_urlsafe(24)
            _undo[token] = {"section": section, "profile": profile, "title": title,
                            "version": result["item"]["version"], "cells": result["before_cells"],
                            "event_id": event_id, "expires": time.monotonic() + 12}
        return {"ok": True, "item": result["item"], "undo_token": token,
                "undo_ttl_ms": 12000, "history_warning": warning}


def undo(section: str, token: str):
    with _mutation_lock:
        _prune()
        entry = _undo.get(token)
        if entry is None or entry["section"] != section:
            raise service.MediaConflict("Undo expired")
        result = change(section, entry["profile"], entry["title"], {}, entry["version"],
                        kind="undo", restore=entry["cells"], undo_event_id=entry["event_id"])
        _undo.pop(token, None)
        return result


def _response(call):
    try:
        return JSONResponse(call(), headers=_NO_STORE)
    except HTTPException:
        raise
    except (service.MediaConflict, history.HistoryConflict):
        raise HTTPException(409, "Media changed or a save is unresolved. Refresh before another change.", headers=_NO_STORE) from None
    except ValueError:
        raise HTTPException(422, "Invalid media request or unavailable field", headers=_NO_STORE) from None
    except Exception:
        raise HTTPException(503, "Media request could not be confirmed. Refresh before another change.", headers=_NO_STORE) from None


@router.get("/media/{section}/data")
def data(section: str, profile: str = ""):
    return _response(lambda: library_state(section, profile))


@router.post("/media/{section}/refresh")
async def refresh(section: str, request: Request):
    _section(section)
    form = await request.form()
    return await run_in_threadpool(_response, lambda: library_state(section, str(form.get("profile") or ""), force=True))


@router.post("/media/{section}/change")
@router.post("/media/{section}/episode")
@router.post("/media/{section}/runs")
async def mutate(section: str, request: Request):
    _section(section)
    form = await request.form()

    def perform():
        profile, title = _identity(form)
        raw = str(form.get("fields") or "{}")
        if len(raw) > 24000:
            raise ValueError("Oversized fields")
        fields = json.loads(raw)
        if not isinstance(fields, dict):
            raise ValueError("Invalid fields")
        kind = "episode" if request.url.path.endswith("/episode") else "replay" if request.url.path.endswith("/runs") else "update"
        return change(section, profile, title, fields, _version(form), kind=kind)

    return await run_in_threadpool(_response, perform)


@router.post("/media/{section}/undo")
async def undo_route(section: str, request: Request):
    _section(section)
    form = await request.form()
    return await run_in_threadpool(_response, lambda: undo(section, str(form.get("token") or "")))


@router.get("/media/{section}/detail")
def detail(section: str, profile: str, title: str):
    _section(section)

    def perform():
        with _mutation_lock:
            item = _current(section, *_identity({"profile": profile, "title": title}))
            try:
                recovered = history.reconcile(section, item)
                recorded = history.detail(section, profile, title)
                return {"item": item, **recorded, "history_warning": recovered.get("warning") or (
                    "Some earlier web changes could not be confirmed" if recorded["pending_count"] else None)}
            except history.HistoryError:
                return {"item": item, "activity": [], "runs": [], "history_warning": "Local media history is unavailable"}

    return _response(perform)


@router.post("/media/{section}/pick")
async def pick(section: str, request: Request):
    _section(section)
    form = await request.form()

    def perform():
        raw = str(form.get("keys") or "[]")
        if len(raw) > 700000:
            raise ValueError("Oversized selection")
        keys = json.loads(raw)
        if not isinstance(keys, list) or len(keys) > 10000 or any(not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key) for key in keys):
            raise ValueError("Invalid selection")
        with _mutation_lock:
            _prune()
            items = library_state(section, str(form.get("profile") or ""))["items"]
            selected = set(keys)
            pool = [item for item in items if item["key"] in selected and (form.get("exclude_recent") != "1" or item["key"] not in _picks)]
            if not pool:
                return {"item": None}
            item = random.choices(pool, weights=[service._random_pick_weight(section, item) for item in pool], k=1)[0]
            _picks[item["key"]] = time.monotonic()
            return {"item": item}

    return await run_in_threadpool(_response, perform)


def _steam_item(profile: str, title: str):
    item = _current("games", profile, title)
    if item.get("source") != "steam" or not str(item.get("external_id") or "").isdigit():
        raise ValueError("Steam source required")
    return item


@router.get("/media/games/steam")
def steam_view(profile: str, title: str):
    return _response(lambda: steam.get_snapshot(profile, title, _steam_item(profile, title)["external_id"]))


@router.post("/media/games/steam/refresh")
@router.post("/media/games/steam/save")
async def steam_action(request: Request):
    form = await request.form()

    def perform():
        profile, title = _identity(form)
        item = _steam_item(profile, title)
        if request.url.path.endswith("/refresh"):
            return steam.refresh(profile, title, item["external_id"])
        hours = steam.saved_hours(profile, title, item["external_id"], str(form.get("snapshot_id") or ""))
        return change("games", profile, title, {"hours_played": hours}, _version(form), kind="steam_save")

    return await run_in_threadpool(_response, perform)