"""Disposable, loopback-only Trading Cards preview using synthetic records."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.cookies import SimpleCookie
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import struct
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from luigi_web.core.static_assets import ModuleStaticFiles
import uvicorn

def seed_cards() -> None:
    from luigi_web import cards

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
            "cmc": 2 if index % 4 else 0,
            "colors": [],
            "color_identity": [],
            "oracle_id": f"preview-oracle-{index}",
            "legalities": {"commander": "legal", "standard": "legal"},
            "prices": {"usd": "1.00"},
        }
        for index in range(1, 101)
    ])
    with cards.transaction() as connection:
        connection.execute(
            "UPDATE cards SET image_small = '/__preview__/cards/' || id || '.png', "
            "image_normal = '/__preview__/cards/' || id || '.png'"
        )
    for name, count in (("Example Deck", 12), ("Hundred Card Deck", 100), ("Forty Card Category", 40)):
        deck_id = cards.create_deck("mtg", name)
        for index in range(1, count + 1):
            card = cards.find_card("mtg", f"Example Card {index:03d}")
            assert card is not None
            category = ("Engine", "Draw", "Interaction", "Lands")[index % 4]
            if name == "Forty Card Category" or (name == "Hundred Card Deck" and 2 <= index <= 41):
                category = "Forty Card Category"
            commander = index == 1 and name != "Forty Card Category"
            cards.add_card_to_deck(
                deck_id, card["id"],
                board="commander" if commander else "main",
                category="" if commander else category,
            )
    cards.create_deck("pokemon", "Example Standard Deck", "standard")
    cards.create_deck("riftbound", "Example Casual Deck", "casual")
    examples = [cards.find_card("mtg", f"Example Card {index:03d}") for index in range(1, 4)]
    assert all(examples)
    actual, estimated, unknown = [card["id"] for card in examples if card is not None]
    with cards.transaction() as connection:
        connection.executemany(
            "INSERT INTO price_history (card_id, snapshot_date, price_usd_minor, "
            "price_usd_foil_minor, price_eur_minor) VALUES (?, ?, ?, ?, ?)",
            [(card_id, day, price, price * 2, price - 10)
             for card_id in (actual, estimated)
             for day, price in (("2026-01-10", 80), ("2026-02-10", 125), ("2026-03-10", 175))],
        )
    cards.add_to_collection(actual, qty=2, acquired_price="1.01", acquired_date="2026-01-10")
    cards.add_to_collection(actual, qty=1, acquired_price="2.02", acquired_date="2026-02-10")
    cards.add_to_collection(estimated, qty=1, acquired_date="2026-02-10",
                            price_source="market_estimate", estimate_confirmed=True)
    cards.add_to_collection(unknown, qty=2, price_source="unknown")


def preview_app() -> FastAPI:
    from luigi_web import auth
    from luigi_web.core.module_registry import ModuleRegistry
    from luigi_web.core.templating import mount_module_assets
    from luigi_web.modules.cards import composition as cards_routes
    from luigi_web.modules.cards.manifest import module
    from luigi_web.paths import STATIC_DIR

    internal_token = os.environ["LUIGI_WEB_UI_TOKEN"]
    session_token = secrets.token_urlsafe(32)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.modules = ModuleRegistry([module])
    app.mount("/static", ModuleStaticFiles(directory=str(STATIC_DIR)), name="static")
    mount_module_assets(app, app.state.modules)
    app.include_router(cards_routes.router)

    @app.middleware("http")
    async def preview_security(request: Request, call_next):
        def deny(status, message):
            return JSONResponse({"detail": message}, status_code=status, headers={"Cache-Control": "no-store"})

        try:
            port = request.url.port or (443 if request.url.scheme == "https" else 80)
            hostname = request.url.hostname
        except ValueError:
            return deny(403, "Loopback only")
        testing = request.client is not None and request.client.host == "testclient"
        hosts = {"127.0.0.1", "localhost", "::1"} | ({"testserver"} if testing else set())
        if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"} or hostname not in hosts:
            return deny(403, "Loopback only")
        path = request.url.path
        bootstrap = request.method in {"GET", "HEAD"}
        cards_path = path == "/cards" or path.startswith("/cards/")
        read_path = (cards_path or path in {"/", "/reminders/count"}
                     or path.startswith(("/static/", "/module-assets/cards/", "/__preview__/cards/")))
        if (bootstrap and not read_path) or (not bootstrap and (request.method != "POST" or not cards_path)):
            return deny(403, "Unavailable in cards preview")
        if cards_path and any(part in {"refresh", "diagram", "notes"} for part in path.split("/")):
            return deny(403, "Catalog refresh and diagrams are disabled in preview")
        session_name = f"luigi_cards_preview_{port}"
        candidate = request.cookies.get(session_name, "")
        valid_session = secrets.compare_digest(candidate.encode(), session_token.encode())
        csrf_cookie = request.cookies.get(auth.CSRF_COOKIE_NAME)
        if csrf_cookie and not csrf_cookie.isascii():
            csrf_cookie = None
        if not bootstrap:
            if not valid_session:
                return deny(401, "Preview session required")
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            if origin is not None and origin != str(request.base_url).rstrip("/"):
                return deny(403, "Same-origin requests only")
            if referer is not None:
                try:
                    source = urlsplit(referer)
                    if source.username or source.password or (source.scheme, source.netloc) != (request.url.scheme, request.url.netloc):
                        return deny(403, "Same-origin requests only")
                except ValueError:
                    return deny(403, "Same-origin requests only")
            csrf_header = request.headers.get("x-csrf-token")
            if not (csrf_cookie and csrf_cookie.isascii() and csrf_header and csrf_header.isascii()
                    and auth.csrf_matches(csrf_cookie, csrf_header)):
                return deny(403, "CSRF validation failed")
        forwarded = SimpleCookie()
        forwarded[auth.COOKIE_NAME] = internal_token
        if csrf_cookie:
            forwarded[auth.CSRF_COOKIE_NAME] = csrf_cookie
        request.scope["headers"] = [
            (name, value) for name, value in request.scope["headers"]
            if name.lower() not in {b"cookie", b"authorization"}
        ] + [(b"cookie", forwarded.output(header="", sep=";").strip().encode("ascii"))]
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; font-src 'self'; "
            "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        )
        if bootstrap and not valid_session:
            response.set_cookie(session_name, session_token, httponly=True, samesite="strict")
        if bootstrap and not csrf_cookie:
            response.set_cookie(auth.CSRF_COOKIE_NAME, auth.csrf_token(), samesite="strict")
        return response

    @app.get("/")
    def start() -> RedirectResponse:
        return RedirectResponse("/cards/mtg/decks", status_code=303)

    @app.get("/reminders/count", dependencies=[Depends(auth.require_auth)])
    def reminders() -> HTMLResponse:
        return HTMLResponse("0")

    @app.get("/__preview__/cards/{card_id}.png", dependencies=[Depends(auth.require_auth)])
    def synthetic_image(card_id: int) -> Response:
        width, height = 189, 264
        color = ((40, 130, 105), (65, 105, 155), (160, 90, 70), (135, 115, 65))[card_id % 4]
        pixels = b"".join(
            b"\x00" + b"".join(
                bytes(color if 16 <= horizontal < 173 and 40 <= vertical < 175 else (28, 32, 30))
                for horizontal in range(width)
            ) for vertical in range(height)
        )

        def chunk(kind, content):
            return struct.pack("!I", len(content)) + kind + content + struct.pack("!I", zlib.crc32(kind + content))

        image = (b"\x89PNG\r\n\x1a\n"
                 + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
        return Response(image, media_type="image/png")

    return app


@contextmanager
def loopback_network_only():
    original_connect = socket.socket.connect
    original_resolve = socket.getaddrinfo
    socketpair_code = getattr(socket.socketpair, "__code__", None)

    def blocked(*args, **kwargs):
        raise RuntimeError("Preview outbound network disabled")

    def connect(connection, address):
        if (socketpair_code is not None and sys._getframe(1).f_code is socketpair_code
                and isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}):
            return original_connect(connection, address)
        return blocked()

    def resolve(host, *args, **kwargs):
        if host not in {"127.0.0.1", "::1", "localhost", None}:
            raise RuntimeError("Preview external DNS disabled")
        return original_resolve(host, *args, **kwargs)

    with (
        patch.object(socket.socket, "connect", connect),
        patch.object(socket.socket, "connect_ex", blocked),
        patch.object(socket, "create_connection", blocked),
        patch.object(socket, "getaddrinfo", resolve),
    ):
        yield


@contextmanager
def preview_context():
    with tempfile.TemporaryDirectory(prefix="luigi-cards-preview-") as temporary:
        directory = Path(temporary).resolve()
        environment = {key: os.environ[key] for key in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR") if key in os.environ}
        environment.update({
            "LUIGI_WEB_UI_TOKEN": secrets.token_urlsafe(32),
            "LUIGI_WEB_CARDS_DB": str(directory / "cards.db"),
            "LUIGI_WEB_DATA_DIR": temporary,
            "LUIGI_WEB_MODULES": "cards",
            "LUIGI_WEB_MODULES_FILE": str(directory / "absent-modules.json"),
            "LUIGI_WEB_LLM_PROVIDER": "disabled",
            "LUIGI_WEB_CARDS_REFRESH_HOURS": "0",
            "LUIGI_WEB_CARDS_DRAWIO_URL": "",
            "LUIGI_WEB_SECURE_COOKIES": "0",
            "LUIGI_WEB_TIMEZONE": "UTC",
            "TEMP": temporary,
            "TMP": temporary,
            "SQLITE_TMPDIR": temporary,
            "APPDATA": temporary,
            "LOCALAPPDATA": temporary,
        })
        original_connect = sqlite3.connect

        def temporary_sqlite(database, *args, **kwargs):
            if kwargs.get("uri") or not Path(database).resolve().is_relative_to(directory):
                raise RuntimeError("Preview database must use temporary storage")
            return original_connect(database, *args, **kwargs)

        with (
            patch.dict(os.environ, environment, clear=True),
            patch("dotenv.load_dotenv", return_value=False),
            patch.object(sqlite3, "connect", temporary_sqlite),
            loopback_network_only(),
        ):
            yield directory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    arguments = parser.parse_args()
    with preview_context():
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