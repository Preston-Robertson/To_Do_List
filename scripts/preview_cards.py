"""Disposable, loopback-only Trading Cards preview using synthetic records."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
import uvicorn

from luigi_web import auth, cards, cards_routes
from luigi_web.paths import STATIC_DIR


def seed_cards() -> None:
    cards.init_db()
    cards.upsert_scryfall_cards([
        {
            "id": f"preview-card-{index}",
            "name": f"Example Card {index:03d}",
            "set": "TST",
            "set_name": "Synthetic Set",
            "collector_number": str(index),
            "type_line": "Artifact" if index % 4 else "Land",
            "mana_cost": "{2}",
            "prices": {"usd": "1.00"},
        }
        for index in range(1, 101)
    ])
    for name, count in (("Example Deck", 12), ("Hundred Card Deck", 100)):
        deck_id = cards.create_deck("mtg", name)
        for index in range(1, count + 1):
            card = cards.find_card("mtg", f"Example Card {index:03d}")
            assert card is not None
            cards.add_card_to_deck(
                deck_id, card["id"],
                board="commander" if index == 1 else "main",
                category="" if index == 1 else ("Engine", "Draw", "Interaction", "Lands")[index % 4],
            )
    cards.create_deck("pokemon", "Example Standard Deck", "standard")
    cards.create_deck("riftbound", "Example Casual Deck", "casual")


def preview_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(cards_routes.router)

    @app.middleware("http")
    async def preview_security(request: Request, call_next):
        if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
            return JSONResponse({"detail": "Loopback only"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if "/refresh" in request.url.path:
                return JSONResponse({"detail": "Catalog refresh is disabled in preview"}, status_code=403)
            if not auth.csrf_matches(
                request.cookies.get(auth.CSRF_COOKIE_NAME),
                request.headers.get("x-csrf-token"),
            ):
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.cookies.get(auth.CSRF_COOKIE_NAME):
            response.set_cookie(auth.CSRF_COOKIE_NAME, auth.csrf_token(), samesite="strict")
        return response

    @app.get("/")
    def start() -> RedirectResponse:
        response = RedirectResponse("/cards/mtg/decks", status_code=303)
        response.set_cookie(
            auth.COOKIE_NAME, os.environ["LUIGI_WEB_UI_TOKEN"],
            httponly=True, samesite="strict",
        )
        return response

    @app.get("/reminders/count", dependencies=[Depends(auth.require_auth)])
    def reminders() -> HTMLResponse:
        return HTMLResponse("0")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="luigi-cards-preview-") as temporary:
        os.environ.update({
            "LUIGI_WEB_UI_TOKEN": secrets.token_urlsafe(32),
            "LUIGI_WEB_CARDS_DB": str(Path(temporary) / "cards.db"),
            "LUIGI_WEB_CARDS_REFRESH_HOURS": "0",
            "LUIGI_WEB_CARDS_DRAWIO_URL": "",
            "LUIGI_WEB_SECURE_COOKIES": "0",
        })
        seed_cards()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", arguments.port))
            port = listener.getsockname()[1]
            print(f"Synthetic Trading Cards preview: http://127.0.0.1:{port}/", flush=True)
            server = uvicorn.Server(uvicorn.Config(
                preview_app(), host="127.0.0.1", port=port,
                lifespan="off", access_log=False, log_level="warning",
            ))
            server.run(sockets=[listener])


if __name__ == "__main__":
    main()