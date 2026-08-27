"""Authenticated HTTP routes for catalogs, decks, and card collections."""
from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import (
    cards,
    cards_importer,
    cards_pokemon,
    cards_scryfall,
    cards_sparkline,
    cards_templating,
)
from .auth import require_auth
from .paths import STATIC_DIR, TEMPLATES_DIR

router = APIRouter(prefix="/cards", dependencies=[Depends(require_auth)])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
cards_templating.register_filters(templates.env)

_BOARD_LABELS = {
    "commander": "Commander",
    "main": "Mainboard",
    "side": "Sideboard",
    "maybe": "Maybeboard",
}


def _asset_version() -> str:
    paths = [STATIC_DIR / "css" / "cards.css", STATIC_DIR / "js" / "cards.js"]
    return str(int(max((path.stat().st_mtime for path in paths if path.exists()), default=0)))


def _drawio_url() -> str:
    value = os.environ.get("LUIGI_WEB_CARDS_DRAWIO_URL", "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    try:
        parsed.port
    except ValueError:
        return ""
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return ""
    return value


def _ctx(request: Request, game_code: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "active_nav": "cards",
        "asset_version": _asset_version(),
        "games": cards.list_games(),
        "current_game": game_code,
        **extra,
    }


def _render(
    template_name: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> Response:
    response = templates.TemplateResponse(template_name, context, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _redirect(request: Request, url: str) -> Response:
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _refresh(request: Request, url: str) -> Response:
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return RedirectResponse(url, status_code=303)


def _form_data(form: Any) -> dict[str, Any]:
    return {str(key): value for key, value in dict(form).items()}


def _require_game(game_code: str) -> dict[str, Any]:
    game = cards.get_game(game_code)
    if not game:
        raise HTTPException(404, "Card game not found")
    return game


def _require_deck(game_code: str, deck_id: int) -> dict[str, Any]:
    deck = cards.get_deck(deck_id, game_code)
    if not deck:
        raise HTTPException(404, "Deck not found")
    return deck


def _deck_state(game_code: str, deck_id: int) -> dict[str, Any]:
    deck = _require_deck(game_code, deck_id)
    rows = cards.list_deck_cards(deck_id)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        board = _BOARD_LABELS.get(str(row["board"]), "Other")
        category = str(row.get("category") or "").strip()
        label = f"{board} · {category}" if category else board
        grouped.setdefault(label, []).append(row)
    return {
        "deck": deck,
        "deck_cards": rows,
        "by_category": grouped,
        "total_qty": sum(int(row["qty"]) for row in rows),
        "total_price_minor": sum(
            int(row["qty"]) * int(row.get("price_usd_minor") or 0) for row in rows
        ),
        "tags": cards.deck_tags(deck_id),
        "all_tags": cards.list_tags(),
    }


def _deck_cards_partial(request: Request, game_code: str, deck_id: int) -> Response:
    state = _deck_state(game_code, deck_id)
    return _render(
        "cards/partials/deck_cards.html",
        _ctx(request, game_code, page_title=state["deck"]["name"], **state),
    )


def _value_error(exc: ValueError) -> HTTPException:
    return HTTPException(422, str(exc))


@router.get("")
def cards_root() -> RedirectResponse:
    return RedirectResponse("/cards/mtg/decks", status_code=303)


@router.get("/{game_code}")
def game_root(game_code: str) -> RedirectResponse:
    _require_game(game_code)
    return RedirectResponse(f"/cards/{game_code}/decks", status_code=303)


@router.get("/{game_code}/catalog", response_class=HTMLResponse)
def catalog_page(
    request: Request,
    game_code: str,
    q: str = "",
    set_code: str = "",
    page: int = 1,
) -> Response:
    game = _require_game(game_code)
    try:
        catalog = cards.browse_catalog(
            game_code, query=q, set_code=set_code, page=page
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _render(
        "cards/catalog.html",
        _ctx(
            request,
            game_code,
            page_title=f"{game['name']} Catalog",
            game=game,
            catalog=catalog,
            sets=cards.list_sets(game_code),
            stats=cards.catalog_stats(game_code),
            q=q,
            set_code=set_code,
        ),
    )


@router.post("/{game_code}/catalog/manual")
async def catalog_manual_create(request: Request, game_code: str) -> Response:
    _require_game(game_code)
    try:
        form = await request.form()
        card_id = cards.create_manual_card(game_code, _form_data(form))
        card = cards.get_card(card_id, game_code)
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(
        request,
        f"/cards/{game_code}/catalog?q={quote(str(card['name']))}",
    )


@router.get("/{game_code}/decks", response_class=HTMLResponse)
def decks_page(
    request: Request,
    game_code: str,
    q: str = "",
    tag: str = "",
    include_archived: bool = False,
) -> Response:
    game = _require_game(game_code)
    decks = cards.list_decks(game_code, include_archived=include_archived)
    if q.strip():
        needle = q.strip().casefold()
        decks = [row for row in decks if needle in str(row["name"]).casefold()]
    if tag.strip():
        wanted = tag.strip().casefold()
        decks = [
            row for row in decks
            if wanted in {item["name"].casefold() for item in cards.deck_tags(row["id"])}
        ]
    return _render(
        "cards/decks.html",
        _ctx(
            request,
            game_code,
            page_title=f"{game['name']} Decks",
            game=game,
            decks=decks,
            all_tags=cards.list_tags(),
            q=q,
            active_tag=tag,
            include_archived=include_archived,
        ),
    )


@router.post("/{game_code}/decks")
def deck_create(
    request: Request,
    game_code: str,
    name: str = Form(...),
    format_: str = Form("commander"),
) -> Response:
    _require_game(game_code)
    try:
        deck_id = cards.create_deck(game_code, name, format_)
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(request, f"/cards/{game_code}/decks/{deck_id}")


@router.post("/{game_code}/decks/import")
def deck_import_new(
    request: Request,
    game_code: str,
    name: str = Form(...),
    format_: str = Form("commander"),
    text: str = Form(""),
) -> Response:
    _require_game(game_code)
    try:
        report = cards_importer.preview(game_code, text)
        with cards.transaction() as conn:
            deck_id = cards.create_deck(game_code, name, format_, conn=conn)
            added = cards_importer.apply(deck_id, report, conn=conn)
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(
        request,
        f"/cards/{game_code}/decks/{deck_id}?imported={added}"
        f"&missing={report.unmatched_count}&unparsed={len(report.unparsed)}",
    )


@router.get("/{game_code}/decks/{deck_id}", response_class=HTMLResponse)
def deck_detail_page(
    request: Request,
    game_code: str,
    deck_id: int,
    imported: int = 0,
    missing: int = 0,
    unparsed: int = 0,
) -> Response:
    _require_game(game_code)
    state = _deck_state(game_code, deck_id)
    flash = None
    if imported or missing or unparsed:
        flash = {"imported": imported, "missing": missing, "unparsed": unparsed}
    return _render(
        "cards/deck_detail.html",
        _ctx(
            request,
            game_code,
            page_title=state["deck"]["name"],
            drawio_url=_drawio_url(),
            flash=flash,
            **state,
        ),
    )


@router.post("/{game_code}/decks/{deck_id}")
async def deck_update(request: Request, game_code: str, deck_id: int) -> Response:
    _require_deck(game_code, deck_id)
    try:
        form = await request.form()
        if not cards.update_deck(deck_id, game_code, _form_data(form)):
            raise HTTPException(404, "Deck not found")
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(request, f"/cards/{game_code}/decks/{deck_id}")


@router.post("/{game_code}/decks/{deck_id}/delete")
def deck_delete(request: Request, game_code: str, deck_id: int) -> Response:
    if not cards.delete_deck(deck_id, game_code):
        raise HTTPException(404, "Deck not found")
    return _redirect(request, f"/cards/{game_code}/decks")


@router.post("/{game_code}/decks/{deck_id}/tags")
def deck_tags_update(
    request: Request,
    game_code: str,
    deck_id: int,
    tags: str = Form(""),
) -> Response:
    _require_deck(game_code, deck_id)
    try:
        cards.set_deck_tags(deck_id, tags.split(","))
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(request, f"/cards/{game_code}/decks/{deck_id}")


@router.post("/{game_code}/decks/{deck_id}/import")
def deck_import_existing(
    request: Request,
    game_code: str,
    deck_id: int,
    text: str = Form(...),
) -> Response:
    _require_deck(game_code, deck_id)
    try:
        result = cards_importer.import_text(game_code, deck_id, text)
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(
        request,
        f"/cards/{game_code}/decks/{deck_id}?imported={result['added']}"
        f"&missing={result['counts']['unmatched']}"
        f"&unparsed={result['counts']['unparsed']}",
    )


@router.get("/{game_code}/decks/{deck_id}/export.txt")
def deck_export(game_code: str, deck_id: int) -> Response:
    deck = _require_deck(game_code, deck_id)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(deck["name"])).strip("-.") or "deck"
    return Response(
        cards.deck_export_text(deck_id),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}.txt"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/{game_code}/decks/{deck_id}/cards")
def deck_card_add(
    request: Request,
    game_code: str,
    deck_id: int,
    card_id: int = Form(...),
    qty: int = Form(1),
    board: str = Form("main"),
    category: str = Form(""),
    set_as_commander: int = Form(0),
) -> Response:
    _require_deck(game_code, deck_id)
    if not cards.get_card(card_id, game_code):
        raise HTTPException(404, "Card not found")
    try:
        cards.add_card_to_deck(
            deck_id,
            card_id,
            qty=qty,
            board="commander" if set_as_commander else board,
            category=category,
        )
    except ValueError as exc:
        raise _value_error(exc) from exc
    if request.headers.get("HX-Request") == "true":
        return _deck_cards_partial(request, game_code, deck_id)
    return _redirect(request, f"/cards/{game_code}/decks/{deck_id}")


@router.post("/{game_code}/decks/{deck_id}/cards/{deck_card_id}")
def deck_card_update(
    request: Request,
    game_code: str,
    deck_id: int,
    deck_card_id: int,
    qty: int = Form(...),
    category: str = Form(""),
) -> Response:
    _require_deck(game_code, deck_id)
    try:
        if not cards.update_deck_card(deck_id, deck_card_id, qty, category):
            raise HTTPException(404, "Deck card not found")
    except ValueError as exc:
        raise _value_error(exc) from exc
    if request.headers.get("HX-Request") == "true":
        return _deck_cards_partial(request, game_code, deck_id)
    return _redirect(request, f"/cards/{game_code}/decks/{deck_id}")


@router.post("/{game_code}/decks/{deck_id}/cards/{deck_card_id}/delete")
def deck_card_delete(
    request: Request,
    game_code: str,
    deck_id: int,
    deck_card_id: int,
) -> Response:
    _require_deck(game_code, deck_id)
    if not cards.remove_deck_card(deck_id, deck_card_id):
        raise HTTPException(404, "Deck card not found")
    if request.headers.get("HX-Request") == "true":
        return _deck_cards_partial(request, game_code, deck_id)
    return _redirect(request, f"/cards/{game_code}/decks/{deck_id}")


@router.get("/{game_code}/decks/{deck_id}/notes")
def deck_notes_load(game_code: str, deck_id: int) -> JSONResponse:
    deck = _require_deck(game_code, deck_id)
    return JSONResponse(
        {"xml": deck.get("notes_xml") or ""},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/{game_code}/decks/{deck_id}/notes")
async def deck_notes_save(request: Request, game_code: str, deck_id: int) -> JSONResponse:
    _require_deck(game_code, deck_id)
    if int(request.headers.get("content-length") or 0) > 1_050_000:
        raise HTTPException(413, "Diagram notes are too large")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        size = cards.save_deck_notes(deck_id, game_code, body.get("xml"))
    except ValueError as exc:
        raise _value_error(exc) from exc
    return JSONResponse(
        {"ok": True, "bytes": size}, headers={"Cache-Control": "no-store"}
    )


@router.post("/{game_code}/import/preview")
async def import_preview(request: Request, game_code: str) -> JSONResponse:
    _require_game(game_code)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        report = cards_importer.preview(game_code, str(body.get("text") or ""))
    except ValueError as exc:
        raise _value_error(exc) from exc
    return JSONResponse(report.as_dict(), headers={"Cache-Control": "no-store"})


@router.get("/{game_code}/search", response_class=HTMLResponse)
def card_search(
    request: Request,
    game_code: str,
    q: str = "",
    for_deck: int | None = None,
    for_collection: bool = False,
) -> Response:
    _require_game(game_code)
    if for_deck is not None:
        _require_deck(game_code, for_deck)
    try:
        results = cards.search_cards(game_code, q)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _render(
        "cards/partials/search_results.html",
        _ctx(
            request,
            game_code,
            page_title="Card Search",
            results=results,
            for_deck=for_deck,
            for_collection=for_collection,
        ),
    )


@router.get("/{game_code}/cards/{card_id}/sparkline.svg")
def card_sparkline(game_code: str, card_id: int, days: int = Query(90)) -> Response:
    if not cards.get_card(card_id, game_code):
        raise HTTPException(404, "Card not found")
    rows = cards.card_price_history(card_id, days=days)
    series = [
        (row["price_usd_minor"], row["price_usd_foil_minor"]) for row in rows
    ]
    return Response(
        cards_sparkline.sparkline(series),
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=1800"},
    )


@router.get("/{game_code}/decks/{deck_id}/value-trend.svg")
def deck_value_trend(game_code: str, deck_id: int, days: int = Query(180)) -> Response:
    _require_deck(game_code, deck_id)
    rows = cards.deck_price_series(deck_id, days=days)
    series = [(row["value_usd_minor"], None) for row in rows]
    return Response(
        cards_sparkline.sparkline(series, width=600, height=140),
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=900"},
    )


@router.get("/{game_code}/collection", response_class=HTMLResponse)
def collection_page(request: Request, game_code: str, q: str = "") -> Response:
    game = _require_game(game_code)
    try:
        rows = cards.list_collection(game_code, q)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _render(
        "cards/collection.html",
        _ctx(
            request,
            game_code,
            page_title=f"{game['name']} Collection",
            game=game,
            rows=rows,
            totals=cards.collection_totals(game_code),
            q=q,
        ),
    )


@router.post("/{game_code}/collection")
def collection_add(
    request: Request,
    game_code: str,
    card_id: int = Form(...),
    qty: int = Form(1),
    foil: int = Form(0),
    condition: str = Form("NM"),
    acquired_date: str = Form(""),
    acquired_price: str = Form(""),
    acquired_currency: str = Form("USD"),
    notes: str = Form(""),
) -> Response:
    _require_game(game_code)
    if not cards.get_card(card_id, game_code):
        raise HTTPException(404, "Card not found")
    try:
        cards.add_to_collection(
            card_id,
            qty=qty,
            foil=bool(foil),
            condition=condition,
            acquired_date=acquired_date,
            acquired_price=acquired_price,
            acquired_currency=acquired_currency,
            notes=notes,
        )
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _refresh(request, f"/cards/{game_code}/collection")


@router.post("/{game_code}/collection/{collection_id}/delete")
def collection_delete(
    request: Request,
    game_code: str,
    collection_id: int,
    qty: int | None = Form(None),
) -> Response:
    _require_game(game_code)
    try:
        deleted = cards.remove_from_collection(
            collection_id, qty, game_code=game_code
        )
    except ValueError as exc:
        raise _value_error(exc) from exc
    if not deleted:
        raise HTTPException(404, "Collection entry not found")
    return _refresh(request, f"/cards/{game_code}/collection")


@router.get("/{game_code}/data/status", response_class=HTMLResponse)
def card_data_status(request: Request, game_code: str) -> Response:
    game = _require_game(game_code)
    return _render(
        "cards/partials/data_status.html",
        _ctx(
            request,
            game_code,
            page_title="Card Data",
            game=game,
            stats=cards.catalog_stats(game_code),
            last_refresh=cards.last_refresh(game_code),
            refresh_state=(
                cards_scryfall.refresh_state()
                if game_code == "mtg" else cards_pokemon.refresh_state()
            ),
            refresh_hours=cards_scryfall.refresh_hours(),
            bulk_kind=cards_scryfall.bulk_kind(),
            pokemon_api_key_set=bool(
                os.environ.get("LUIGI_WEB_CARDS_POKEMON_API_KEY", "").strip()
            ),
        ),
    )


@router.get("/{game_code}/data", response_class=HTMLResponse)
def card_data_page(request: Request, game_code: str) -> Response:
    game = _require_game(game_code)
    return _render(
        "cards/data.html",
        _ctx(
            request,
            game_code,
            page_title=f"{game['name']} Data",
            game=game,
        ),
    )


@router.post("/{game_code}/data/scryfall/refresh")
def scryfall_refresh(
    request: Request,
    game_code: str,
    kind: str = Form("default_cards"),
) -> Response:
    if game_code != "mtg" or not cards.get_game(game_code):
        raise HTTPException(404, "Scryfall refresh is only available for MTG")
    try:
        started = cards_scryfall.start_refresh(kind)
    except ValueError as exc:
        raise _value_error(exc) from exc
    if not started:
        raise HTTPException(409, "A Scryfall refresh is already running")
    return _refresh(request, f"/cards/{game_code}/data")


@router.post("/{game_code}/data/pokemon/refresh")
def pokemon_refresh(request: Request, game_code: str) -> Response:
    if game_code != "pokemon" or not cards.get_game(game_code):
        raise HTTPException(404, "Pokemon catalog refresh is only available for Pokemon")
    if not cards_pokemon.start_refresh():
        raise HTTPException(409, "A Pokemon catalog refresh is already running")
    return _refresh(request, f"/cards/{game_code}/data")