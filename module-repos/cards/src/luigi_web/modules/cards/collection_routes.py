"""Collection endpoints. Mount collection_router alongside the existing cards router."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ... import auth
from . import collection, purchases
from . import repository as cards


def protect(request: Request) -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "same-origin request required", headers={"Cache-Control": "no-store"})
    if auth.is_authenticated(None, request.headers.get("authorization"), None):
        return
    if not auth.csrf_matches(request.cookies.get(auth.CSRF_COOKIE_NAME), request.headers.get("x-csrf-token")):
        raise HTTPException(403, "CSRF validation failed", headers={"Cache-Control": "no-store"})


collection_router = APIRouter(prefix="/cards", dependencies=[Depends(auth.require_auth), Depends(protect)])
router = collection_router


def _bad_request(error: ValueError) -> HTTPException:
    return HTTPException(400, str(error), headers={"Cache-Control": "no-store"})


def _json(value: Any) -> JSONResponse:
    return JSONResponse(value, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _game(game_code: str) -> dict[str, Any]:
    game = cards.get_game(game_code)
    if not game:
        raise HTTPException(404, "Card game not found")
    return game


def collection_page(request: Request, game_code: str, values: dict[str, Any] | None = None) -> Response:
    from . import routes

    game = _game(game_code)
    try:
        state = collection.page(game_code, dict(request.query_params) if values is None else values)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return routes._render("cards/collection.html", routes._ctx(
        request, game_code, page_title=f"{game['name']} Collection", game=game, **state))


@collection_router.post("/{game_code}/collection/query")
async def collection_query(request: Request, game_code: str) -> Response:
    return collection_page(request, game_code, dict(await request.form()))


@collection_router.post("/{game_code}/collection/search")
def collection_search(request: Request, game_code: str, q: str = Form("")) -> Response:
    _game(game_code)
    try:
        rows = cards.search_cards(game_code, q, limit=50)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    if request.headers.get("HX-Target") == "collection-search-results":
        from . import routes

        return routes._render("cards/partials/collection_search.html", routes._ctx(request, game_code, rows=rows))
    return _json(rows)


@collection_router.post("/{game_code}/collection/estimate")
def collection_estimate(game_code: str, card_id: int = Form(...), acquired_date: str = Form(...),
                        currency: str = Form("USD"), foil: int = Form(0)) -> Response:
    _game(game_code)
    if foil not in (0, 1):
        raise HTTPException(400, "invalid foil variant")
    if not cards.get_card(card_id, game_code):
        raise HTTPException(404, "Card not found")
    try:
        with cards._connect() as conn:
            result = purchases.estimate(conn, card_id, acquired_date, currency, bool(foil))
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return _json(result)


@collection_router.get("/{game_code}/collection/{collection_id}/lots")
def collection_lots(request: Request, game_code: str, collection_id: int) -> Response:
    from . import routes

    _game(game_code)
    holding = cards.get_collection_entry(collection_id, game_code)
    if not holding:
        raise HTTPException(404, "Collection entry not found")
    return routes._render("cards/partials/collection_lots.html", routes._ctx(
        request, game_code, holding=holding, lots=purchases.lots(game_code, collection_id)))


@collection_router.get("/{game_code}/collection/removed/history")
def collection_removed(request: Request, game_code: str) -> Response:
    from . import routes

    _game(game_code)
    return routes._render("cards/partials/collection_lots.html", routes._ctx(
        request, game_code, holding=None, lots=purchases.lots(game_code, deleted=True)))


@collection_router.post("/{game_code}/collection/import/preview")
async def collection_import_preview(request: Request, game_code: str,
                                    upload: UploadFile, duplicate_mode: str = Form(...)) -> Response:
    from . import routes

    _game(game_code)
    try:
        raw = await upload.read(collection.MAX_CSV_BYTES + 1)
        preview = collection.preview_csv(game_code, raw, duplicate_mode)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    finally:
        await upload.close()
    return routes._render("cards/partials/collection_import.html", routes._ctx(request, game_code, preview=preview))


@collection_router.post("/{game_code}/collection/import/apply")
def collection_import_apply(game_code: str, token: str = Form(...), confirm_estimates: bool = Form(False)) -> Response:
    _game(game_code)
    try:
        return _json(collection.apply_csv(game_code, token, confirm_estimates=confirm_estimates))
    except ValueError as exc:
        raise _bad_request(exc) from exc


@collection_router.post("/{game_code}/collection/export")
async def collection_export(request: Request, game_code: str) -> Response:
    _game(game_code)
    try:
        stream = collection.export_csv(game_code, dict(await request.form()))
        first = next(stream)
    except ValueError as exc:
        raise _bad_request(exc) from exc

    async def content():
        try:
            yield first
            for chunk in stream:
                yield chunk
        finally:
            stream.close()

    return StreamingResponse(content(), media_type="text/csv; charset=utf-8", headers={
        "Cache-Control": "no-store", "Pragma": "no-cache",
        "Content-Disposition": 'attachment; filename="collection.csv"',
        "X-Content-Type-Options": "nosniff",
    })