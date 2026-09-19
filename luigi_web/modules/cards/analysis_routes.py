"""Authenticated read-only build/CSV/analysis endpoints; mount `router` once.

GET /cards/{game_code}/decks/{deck_id}/build and /build.csv accept match_mode
(exact|any), repeated board values, boards_set=1 for an explicit empty selection,
and repeated reserve_deck_id values in allocation priority order (maximum 20).
The HTML picker uses choices_page; it never automatically reserves another deck.
GET /cards/{game_code}/decks/{deck_id}/analysis.json returns deck_analysis v1.
No application import, startup hook, external lookup, or write operation is used.
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ...auth import require_auth
from ...core.templating import create_templates
from . import analysis, repository, templating

router = APIRouter(prefix="/cards", dependencies=[Depends(require_auth)])
templates = create_templates()
templating.register_filters(templates.env)
_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}


def _error(exc: analysis.AnalysisError) -> HTTPException:
    return HTTPException(404 if isinstance(exc, analysis.DeckNotFound) else 422,
                         str(exc), headers=_HEADERS)


def _checklist(request: Request, game_code: str, deck_id: int) -> dict:
    query = request.query_params
    reservations = query.getlist("reserve_deck_id")
    if len(reservations) > analysis.MAX_RESERVATIONS:
        raise HTTPException(422, "Select at most 20 competing decks.", headers=_HEADERS)
    try:
        identifiers = [int(value) for value in reservations if value]
    except ValueError:
        raise HTTPException(422, "Invalid deck selection.", headers=_HEADERS) from None
    boards = query.getlist("board") if query.get("boards_set") == "1" or "board" in query else None
    try:
        return analysis.build_checklist(game_code, deck_id, match_mode=query.get("match_mode", "exact"),
                                        boards=boards, reserve_deck_ids=identifiers)
    except analysis.AnalysisError as exc:
        raise _error(exc) from None


def _csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    stripped = text.lstrip(" \t\r\n\ufeff")
    if text.startswith(("\t", "\r", "\n")) or stripped.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def shopping_csv(checklist: dict) -> str:
    """CSV of missing items only; unknown money stays blank, formulas escaped."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("name", "missing_qty", "requested_card_ids", "identity_key", "currency",
                     "unit_price_minor", "missing_cost_minor", "price_basis", "price_as_of"))
    for item in checklist["items"]:
        if not item["missing"]:
            continue
        writer.writerow([_csv_cell(value) for value in (
            item["name"], item["missing"], "|".join(map(str, item["requested_card_ids"])), item["key"], "USD",
            item["est_unit_minor"], item["missing_cost_minor"], item["price_basis"], item["price_as_of"],
        )])
    return output.getvalue()


@router.get("/{game_code}/decks/{deck_id}/build", response_class=HTMLResponse)
def build_page(request: Request, game_code: str, deck_id: int) -> Response:
    build = _checklist(request, game_code, deck_id)
    try:
        page = int(request.query_params.get("choices_page", "1"))
    except ValueError:
        raise HTTPException(422, "Invalid deck choices page.", headers=_HEADERS) from None
    try:
        choices = analysis.reservation_choices(game_code, deck_id, page=page)
    except analysis.AnalysisError as exc:
        raise _error(exc) from None
    options = {choice["id"]: choice for choice in choices["items"]}
    for reservation in build["reservations"]:
        options.setdefault(reservation["deck_id"], {"id": reservation["deck_id"], "name": reservation["name"]})
    priorities = [reservation["deck_id"] for reservation in build["reservations"]]
    if len(priorities) < analysis.MAX_RESERVATIONS:
        priorities.append(None)
    context = {"request": request, "active_nav": "cards", "current_game": game_code,
               "games": repository.list_games(), "page_title": "Build checklist",
               "build": build, "choices": choices, "reservation_options": list(options.values()),
               "priorities": priorities, "board_options": repository.BOARDS}
    response = templates.TemplateResponse("cards/build.html", context)
    response.headers.update(_HEADERS)
    return response


@router.get("/{game_code}/decks/{deck_id}/build.csv")
def build_csv(request: Request, game_code: str, deck_id: int) -> Response:
    build = _checklist(request, game_code, deck_id)
    return Response(shopping_csv(build), media_type="text/csv; charset=utf-8",
                    headers={**_HEADERS, "Content-Disposition": f'attachment; filename="deck-{deck_id}-shopping.csv"'})


@router.get("/{game_code}/decks/{deck_id}/analysis.json", response_class=JSONResponse)
def analysis_json(game_code: str, deck_id: int) -> Response:
    try:
        result = analysis.deck_analysis(game_code, deck_id)
    except analysis.AnalysisError as exc:
        raise _error(exc) from None
    return JSONResponse(result, headers=_HEADERS)