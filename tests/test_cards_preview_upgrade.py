"""Offline checks for the disposable Cards preview and its browser boundary."""
from contextlib import ExitStack
from html.parser import HTMLParser
import os
from pathlib import Path
from importlib.resources import files as module_files
import socket
import sqlite3
import sys
import unittest
from unittest.mock import patch

from fastapi import Depends, Request
from fastapi.testclient import TestClient

from scripts.preview_cards import preview_app, preview_context, seed_cards


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://localhost:58110"
SESSION_NAME = "luigi_cards_preview_58110"
INTERNAL_TOKEN = "synthetic-preview-internal-token"


class StylesheetLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.urls.add(attributes["href"])


class CardsPreviewUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = self.stack.enter_context(preview_context())
        self.stack.enter_context(patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": INTERNAL_TOKEN}))
        seed_cards()
        self.app = preview_app()
        self.client = self.stack.enter_context(TestClient(self.app, base_url=ORIGIN))

    def active_lots(self):
        from luigi_web.modules.cards import purchases
        from luigi_web.modules.cards import repository as cards

        return sorted([lot for holding in cards.list_collection("mtg")
                       for lot in purchases.lots("mtg", holding["id"])], key=lambda lot: lot["id"])

    def test_root_bootstraps_without_reading_or_overwriting_production_cookies(self):
        from luigi_web import auth

        self.client.cookies.set(auth.COOKIE_NAME, "synthetic-production-cookie")
        self.client.cookies.set("luigi_media_preview_session_58107", "synthetic-media-cookie")
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/cards/mtg/decks")
        cookies = response.headers.get_list("set-cookie")
        self.assertTrue(any(cookie.startswith(SESSION_NAME + "=") and "HttpOnly" in cookie
                            and "SameSite=strict" in cookie for cookie in cookies))
        self.assertFalse(any(cookie.startswith(auth.COOKIE_NAME + "=") for cookie in cookies))
        self.assertEqual(self.client.cookies.get(auth.COOKIE_NAME), "synthetic-production-cookie")
        self.assertNotIn(INTERNAL_TOKEN, " ".join(cookies) + response.text)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_cards_module_stylesheets_are_mounted_and_no_store(self):
        directory = Path(str(module_files("luigi_web.modules.cards"))) / "static"
        stylesheets = list(directory.rglob("*.css"))
        self.assertTrue(stylesheets)
        for stylesheet in stylesheets:
            path = "/module-assets/cards/" + stylesheet.relative_to(directory).as_posix()
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/css", response.headers["content-type"])
                self.assertEqual(response.headers["cache-control"], "no-store")

    def test_composed_pages_and_their_stylesheets_load_without_a_login_redirect(self):
        from luigi_web.modules.cards import repository as cards

        deck = next(deck for deck in cards.list_decks("mtg") if deck["name"] == "Hundred Card Deck")
        holding = cards.list_collection("mtg")[0]
        stylesheets = StylesheetLinks()
        for path in (
            "/cards/mtg/catalog", f"/cards/mtg/decks/{deck['id']}",
            f"/cards/mtg/decks/{deck['id']}/build", f"/cards/mtg/decks/{deck['id']}/analysis.json",
            f"/cards/mtg/decks/{deck['id']}/versions", "/cards/mtg/collection",
            f"/cards/mtg/collection/{holding['id']}/lots", "/reminders/count",
            holding["image_normal"],
        ):
            with self.subTest(path=path):
                self.client.cookies.clear()
                response = self.client.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertNotIn(INTERNAL_TOKEN, response.text)
                if "text/html" in response.headers["content-type"]:
                    stylesheets.feed(response.text)
        self.assertTrue(any(url.startswith("/module-assets/cards/") for url in stylesheets.urls))
        for url in stylesheets.urls:
            self.assertTrue(url.startswith(("/static/", "/module-assets/cards/")))
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertEqual([module.id for module in self.app.state.modules.enabled], ["cards"])

    def test_mutation_needs_prior_scoped_cookie_even_with_internal_credentials(self):
        from luigi_web import auth

        for headers in (
            {"Cookie": f"{auth.COOKIE_NAME}={INTERNAL_TOKEN}; {auth.CSRF_COOKIE_NAME}=synthetic-csrf"},
            {"Authorization": f"Bearer {INTERNAL_TOKEN}", "Cookie": f"{auth.CSRF_COOKIE_NAME}=synthetic-csrf"},
        ):
            with self.subTest(source="cookie" if "Authorization" not in headers else "bearer"):
                response = self.client.post("/cards/mtg/decks/new", headers={**headers, "x-csrf-token": "synthetic-csrf"})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertNotIn("set-cookie", response.headers)

    def test_session_cannot_be_reused_on_another_port_or_app(self):
        from luigi_web import auth

        self.client.get("/")
        cookie = f"{SESSION_NAME}={self.client.cookies.get(SESSION_NAME)}; {auth.CSRF_COOKIE_NAME}=synthetic-csrf"
        for application, origin in ((self.app, "http://localhost:58111"), (preview_app(), ORIGIN)):
            with TestClient(application, base_url=origin) as client:
                response = client.post("/cards/mtg/decks", headers={"Cookie": cookie, "x-csrf-token": "synthetic-csrf"})
                self.assertEqual(response.status_code, 401)

    def test_remote_clients_untrusted_hosts_and_unmounted_features_are_rejected(self):
        for host, peer in (("http://example.invalid", "testclient"), (ORIGIN, "192.0.2.10"),
                           ("http://testserver", "127.0.0.1"), ("http://localhost:invalid", "testclient")):
            async def client_app(scope, receive, send):
                await self.app({**scope, "client": (peer, 51000)}, receive, send)

            with TestClient(client_app, base_url=ORIGIN) as client:
                response = client.get("/", headers={"Host": host.removeprefix("http://")}, follow_redirects=False)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("set-cookie", response.headers)
        for path in ("/admin", "/finance", "/games", "/modules", "/docs", "/openapi.json", "/login",
                     "/module-assets/media/css/media.css", "/cards/mtg/decks/1/notes"):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(response.headers["cache-control"], "no-store")
        for path in ("/cards/mtg/data/scryfall/refresh", "/cards/pokemon/data/pokemon/refresh",
                     "/cards/mtg/decks/1/notes"):
            self.assertEqual(self.client.post(path).status_code, 403)

    def test_real_collection_and_version_mutations_work_with_forwarded_csrf(self):
        from luigi_web import auth
        from luigi_web.modules.cards import repository as cards

        self.client.get("/")
        headers = {"Origin": ORIGIN, "Referer": ORIGIN + "/cards/mtg/collection",
                   "x-csrf-token": self.client.cookies.get(auth.CSRF_COOKIE_NAME), "Authorization": "Bearer invalid"}
        card = cards.find_card("mtg", "Example Card 001")
        self.assertIsNotNone(card)
        response = self.client.post("/cards/mtg/collection", headers=headers, data={
            "card_id": card["id"], "qty": 1, "acquired_price": "3.03", "acquired_date": "2026-04-10",
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        lot = next(lot for lot in self.active_lots() if lot["acquired_date"] == "2026-04-10")
        self.assertEqual(lot["unit_price_minor"], 303)
        response = self.client.post("/cards/mtg/decks/1/versions", headers=headers, json={"label": "Example baseline"})
        self.assertEqual(response.status_code, 200)
        version_id = response.json()["version_id"]
        preview = self.client.get(f"/cards/mtg/decks/1/versions/{version_id}/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.headers["cache-control"], "no-store")

    def test_fixtures_preserve_decks_and_separate_actual_estimated_unknown_costs(self):
        from luigi_web.modules.cards import collection
        from luigi_web.modules.cards import repository as cards

        decks = {deck["name"]: deck for deck in cards.list_decks("mtg")}
        self.assertEqual({name: deck["card_count"] for name, deck in decks.items()}, {
            "Example Deck": 12, "Hundred Card Deck": 100, "Forty Card Category": 40,
        })
        forty = cards.list_deck_cards(decks["Forty Card Category"]["id"])
        self.assertEqual({card["category"] for card in forty}, {"Forty Card Category"})
        self.assertTrue(all(card["image_normal"].startswith("/__preview__/cards/") for card in forty))
        with cards._connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cards").fetchone()[0], 100)
        holdings = cards.list_collection("mtg")
        self.assertEqual(len(holdings), 3)
        self.assertEqual(sorted(row["qty"] for row in holdings), [1, 2, 3])
        lots = self.active_lots()
        self.assertEqual(len(lots), 4)
        actual = sorted((lot for lot in lots if lot["price_source"] == "entered"), key=lambda lot: lot["acquired_date"])
        self.assertEqual(len({lot["original_card_id"] for lot in actual}), 1)
        self.assertEqual([(lot["qty"], lot["unit_price_minor"], lot["acquired_date"]) for lot in actual],
                         [(2, 101, "2026-01-10"), (1, 202, "2026-02-10")])
        estimate = next(lot for lot in lots if lot["price_source"] == "market_estimate")
        self.assertEqual((estimate["qty"], estimate["unit_price_minor"], estimate["snapshot_date"]), (1, 125, "2026-02-10"))
        self.assertEqual(estimate["snapshot_source"], "price_history:scryfall")
        unknown = next(lot for lot in lots if lot["price_source"] == "unknown")
        self.assertEqual(unknown["qty"], 2)
        self.assertIsNone(unknown["unit_price_minor"])
        totals = collection.page("mtg", {})["totals"]
        self.assertEqual((totals["actual_cost_minor"], totals["estimate_cost_minor"], totals["unknown_cost_qty"]), (404, 125, 2))

    def test_requested_price_date_is_exact_and_market_changes_preserve_purchase_baseline(self):
        from luigi_web import auth
        from luigi_web.modules.cards import repository as cards

        self.client.get("/")
        headers = {"Origin": ORIGIN, "x-csrf-token": self.client.cookies.get(auth.CSRF_COOKIE_NAME)}
        card = cards.find_card("mtg", "Example Card 002")
        self.assertIsNotNone(card)
        before = self.active_lots()
        for day, expected in (("2026-01-10", 80), ("2026-02-10", 125), ("2026-03-10", 175), ("2026-02-11", None)):
            response = self.client.post("/cards/mtg/collection/estimate", headers=headers,
                                        data={"card_id": card["id"], "acquired_date": day})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["unit_price_minor"], expected)
            self.assertEqual(response.json()["snapshot_date"], day)
        with cards.transaction() as connection:
            connection.execute("UPDATE cards SET price_usd_minor = 999")
            connection.execute("UPDATE price_history SET price_usd_minor = 888")
        self.assertEqual(self.active_lots(), before)

    def test_forwarded_cookie_preserves_csrf_and_wrong_origin_cannot_bypass_it(self):
        from luigi_web import auth

        @self.app.post("/cards/__preview__/auth-check", dependencies=[Depends(auth.require_auth)])
        def auth_check(request: Request):
            return {
                "csrf_matches": auth.csrf_matches(request.cookies.get(auth.CSRF_COOKIE_NAME), request.headers.get("x-csrf-token")),
                "cookie_names": sorted(request.cookies),
                "authorization_present": "authorization" in request.headers,
            }

        self.client.get("/")
        headers = {"x-csrf-token": self.client.cookies.get(auth.CSRF_COOKIE_NAME), "Origin": ORIGIN}
        response = self.client.post("/cards/__preview__/auth-check", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"csrf_matches": True, "cookie_names": sorted([auth.COOKIE_NAME, auth.CSRF_COOKIE_NAME]),
                                           "authorization_present": False})
        for extra in (
            {"Origin": "http://localhost:58107"},
            {"Origin": "http://example.invalid", "Authorization": "Bearer invalid"},
            {"Referer": "http://example.invalid/cards"},
            {"x-csrf-token": "wrong"},
        ):
            response = self.client.post("/cards/__preview__/auth-check", headers={**headers, **extra})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.headers["cache-control"], "no-store")


class CardsPreviewIsolationTests(unittest.TestCase):
    def test_context_blocks_inherited_settings_external_storage_network_and_dotenv(self):
        from luigi_web.modules.cards import repository as cards
        import dotenv

        host_before = sys.modules.get("luigi_web.application")
        with patch.dict(os.environ, {"LUIGI_WEB_CARDS_DB": "must-not-open.db", "SYNTHETIC_SENTINEL": "hidden"}):
            with preview_context() as directory:
                self.assertNotIn("SYNTHETIC_SENTINEL", os.environ)
                self.assertEqual(cards.db_path(), directory / "cards.db")
                self.assertEqual(Path(os.environ["SQLITE_TMPDIR"]), directory)
                self.assertFalse(dotenv.load_dotenv())
                seed_cards()
                with self.assertRaisesRegex(RuntimeError, "temporary storage"):
                    sqlite3.connect("must-not-open.db")
                with self.assertRaisesRegex(RuntimeError, "temporary storage"):
                    sqlite3.connect(f"file:{directory / 'cards.db'}", uri=True)
                for address in (("example.invalid", 443), ("127.0.0.1", 5432)):
                    with self.assertRaisesRegex(RuntimeError, "outbound network disabled"):
                        socket.create_connection(address)
                    with socket.socket() as connection:
                        with self.assertRaisesRegex(RuntimeError, "outbound network disabled"):
                            connection.connect(address)
                        with self.assertRaisesRegex(RuntimeError, "outbound network disabled"):
                            connection.connect_ex(address)
                with self.assertRaisesRegex(RuntimeError, "external DNS disabled"):
                    socket.getaddrinfo("example.invalid", 443)
            self.assertFalse(directory.exists())
            self.assertEqual(os.environ["LUIGI_WEB_CARDS_DB"], "must-not-open.db")
            self.assertEqual(os.environ["SYNTHETIC_SENTINEL"], "hidden")
        self.assertIs(sys.modules.get("luigi_web.application"), host_before)


if __name__ == "__main__":
    unittest.main()