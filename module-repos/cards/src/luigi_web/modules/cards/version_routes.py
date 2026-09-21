"""Authenticated deck version forms; mounted by the parent Cards router."""
from __future__ import annotations

import json
import sqlite3
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from ...auth import require_auth
from ...core.templating import create_templates
from . import repository as cards
from . import versions


class PrivateVersionRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                response = await original(request)
            except versions.VersionNotFound as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=404)
            except versions.VersionConflict as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=409)
            except versions.VersionError as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=422)
            except RequestValidationError:
                response = JSONResponse({"detail": "Invalid deck version request."}, status_code=422)
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
            except (sqlite3.Error, OSError, RuntimeError, OverflowError):
                response = JSONResponse({"detail": "Deck versions are unavailable. No success was confirmed."}, status_code=503)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return response

        return handle


router = APIRouter(prefix="/cards", dependencies=[Depends(require_auth)], route_class=PrivateVersionRoute)
templates = create_templates()


def _scope(game_code: str, deck_id: int) -> None:
    if not cards.get_game(game_code) or not cards.get_deck(deck_id, game_code):
        raise versions.VersionNotFound("Deck not found.")


def _json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "") or request.headers.get("content-type", "").split(";", 1)[0] == "application/json"


def _render(request: Request, game_code: str, state: dict[str, Any], *, preview: bool = False) -> Response:
    if _json(request):
        return JSONResponse(state)
    return templates.TemplateResponse("cards/versions.html", {
        "request": request, "active_nav": "cards", "current_game": game_code,
        "games": cards.list_games(), "page_title": "Deck versions",
        "preview": preview, "max_versions": versions.MAX_VERSIONS, **state,
    })


def _success(request: Request, payload: dict[str, Any], url: str) -> Response:
    if _json(request):
        return JSONResponse({"ok": True, **payload})
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


async def _payload(request: Request) -> dict[str, Any]:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 16_384:
            raise versions.VersionError("Deck version request is too large.")
    if not body:
        return {}
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    try:
        if content_type == "application/json":
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise versions.VersionError("Invalid deck version request.")
            return payload
        if content_type != "application/x-www-form-urlencoded":
            raise versions.VersionError("Use a form or JSON deck version request.")
        return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=12))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise versions.VersionError("Invalid deck version request.") from exc


def _selected_id(value: str) -> int | None:
    if not value or value == "current":
        return None
    if not value.isascii() or not value.isdigit() or len(value) > 19:
        raise versions.VersionError("Invalid version selection.")
    selected = int(value)
    if not 0 < selected <= 9_223_372_036_854_775_807:
        raise versions.VersionError("Invalid version selection.")
    return selected


@router.get("/{game_code}/decks/{deck_id}/versions")
def deck_versions(request: Request, game_code: str, deck_id: int, before: str = "", after: str = "") -> Response:
    _scope(game_code, deck_id)
    state = versions.version_page(game_code, deck_id, _selected_id(before), _selected_id(after))
    return _render(request, game_code, state)


@router.post("/{game_code}/decks/{deck_id}/versions")
async def save_deck_version(request: Request, game_code: str, deck_id: int) -> Response:
    await run_in_threadpool(_scope, game_code, deck_id)
    payload = await _payload(request)
    version = await run_in_threadpool(versions.save_version, game_code, deck_id, payload.get("label", ""))
    return _success(request, {"version_id": version["id"]}, f"/cards/{game_code}/decks/{deck_id}/versions?before={version['id']}")


@router.get("/{game_code}/decks/{deck_id}/versions/{version_id}/preview")
def restore_preview(request: Request, game_code: str, deck_id: int, version_id: int) -> Response:
    _scope(game_code, deck_id)
    return _render(request, game_code, versions.preview_restore(game_code, deck_id, version_id), preview=True)


@router.post("/{game_code}/decks/{deck_id}/versions/{version_id}/restore")
async def restore_deck_version(request: Request, game_code: str, deck_id: int, version_id: int) -> Response:
    await run_in_threadpool(_scope, game_code, deck_id)
    payload = await _payload(request)
    result = await run_in_threadpool(
        versions.restore_version,
        game_code, deck_id, version_id,
        expected_current_hash=payload.get("expected_current_hash", ""),
        expected_version_hash=payload.get("expected_version_hash", ""),
        confirm=payload.get("confirm") in (True, "1", "true", "on"),
    )
    return _success(request, result, f"/cards/{game_code}/decks/{deck_id}/versions?before={version_id}")


@router.post("/{game_code}/decks/{deck_id}/duplicate")
async def duplicate_deck(request: Request, game_code: str, deck_id: int) -> Response:
    await run_in_threadpool(_scope, game_code, deck_id)
    payload = await _payload(request)
    name = payload.get("name")
    if name is not None and not isinstance(name, str):
        raise versions.VersionError("Invalid deck name.")
    clone = await run_in_threadpool(versions.duplicate_deck, game_code, deck_id, name)
    return _success(request, {"deck_id": clone["id"]}, f"/cards/{game_code}/decks/{clone['id']}")