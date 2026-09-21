"""Collection integration tests and an opt-in loopback synthetic UI preview."""
from __future__ import annotations

import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from luigi_web.core.static_assets import ModuleStaticFiles
from fastapi.testclient import TestClient

from luigi_web import auth
from luigi_web.modules.cards import collection, purchases, repository as cards
from luigi_web.modules.cards.collection_routes import collection_router
from luigi_web.modules.cards.routes import router
from luigi_web.paths import STATIC_DIR

SYNTHETIC_SESSION = "collection-synthetic-test-session"
SYNTHETIC_CSRF = "collection-synthetic-test-csrf"


def fixture_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    app.include_router(collection_router)
    app.mount("/static", ModuleStaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


def seed() -> int:
    cards.init_db()
    with cards.transaction() as conn:
        card_id = conn.execute("""
            INSERT INTO cards(game_code,external_id,source,name,set_code,collector_number,rarity,
                price_usd_minor,price_updated_at)
            VALUES ('mtg','synthetic-route','manual','Example collection card','TST','1','rare',250,'2026-01-02T12:00:00Z')
        """).lastrowid
        assert card_id is not None
        conn.execute("INSERT INTO price_history VALUES (?,'2026-01-02',102,204,99)", (card_id,))
    return int(card_id)


class CollectionRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            "LUIGI_WEB_CARDS_DB": str(Path(self.temp.name) / "cards.sqlite"),
            "LUIGI_WEB_UI_TOKEN": SYNTHETIC_SESSION,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.card_id = seed()
        self.app = fixture_app()
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.cookies.set(auth.COOKIE_NAME, SYNTHETIC_SESSION)
        self.client.cookies.set(auth.CSRF_COOKIE_NAME, SYNTHETIC_CSRF)
        self.headers = {"X-CSRF-Token": SYNTHETIC_CSRF, "HX-Request": "true"}

    def post(self, path, **kwargs):
        return self.client.post("/cards/mtg/collection" + path, headers=self.headers, **kwargs)

    def test_no_duplicate_routes_and_all_new_routes_require_auth(self):
        declared = [(route.path, method) for group in (router, collection_router) for route in group.routes for method in route.methods]
        self.assertEqual(len(declared), len(set(declared)))
        with TestClient(self.app) as anonymous:
            for path in ("/query", "/estimate", "/export", "/search", "/import/preview", "/import/apply"):
                response = anonymous.post("/cards/mtg/collection" + path)
                self.assertEqual(response.status_code, 401)
            self.assertEqual(anonymous.get("/cards/mtg/collection/1/lots").status_code, 401)
            self.assertEqual(anonymous.get("/cards/mtg/collection/removed/history").status_code, 401)

    def test_cookie_mutations_require_csrf_and_same_origin(self):
        data = {"card_id": self.card_id}
        self.assertEqual(self.client.post("/cards/mtg/collection", data=data).status_code, 403)
        self.assertEqual(self.client.post("/cards/mtg/collection", data=data, headers={**self.headers, "Origin": "https://example.invalid"}).status_code, 403)
        for path in ("/query", "/estimate", "/export", "/import/apply"):
            self.assertEqual(self.client.post("/cards/mtg/collection" + path).status_code, 403)
        self.assertEqual(cards.list_collection("mtg"), [])

    def test_bearer_client_without_cookie_csrf_can_record_purchase(self):
        with TestClient(self.app) as client:
            response = client.post("/cards/mtg/collection", data={"card_id": self.card_id},
                                   headers={"Authorization": "Bearer " + SYNTHETIC_SESSION, "HX-Request": "true"})
        self.assertEqual(response.status_code, 204)
        holding = cards.list_collection("mtg")[0]
        self.assertEqual(purchases.lots("mtg", holding["id"])[0]["price_source"], "unknown")

    def test_estimate_is_no_store_and_requires_explicit_confirmation(self):
        data = {"card_id": self.card_id, "acquired_date": "2026-01-02", "foil": 1, "currency": "USD"}
        response = self.post("/estimate", data=data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json()["unit_price_minor"], 204)
        self.assertEqual(self.post("/estimate", data={**data, "currency": "EUR"}).json()["available"], False)
        fields = {"card_id": self.card_id, "acquired_date": "2026-01-02", "price_source": "market_estimate"}
        self.assertEqual(self.post("", data=fields).status_code, 422)
        self.assertEqual(self.post("", data={**fields, "estimate_confirmed": "true"}).status_code, 204)
        self.assertEqual(collection.page("mtg", {})["totals"]["estimate_cost_minor"], 102)

    def test_cross_game_card_and_lot_are_rejected(self):
        self.assertEqual(self.client.post("/cards/pokemon/collection/estimate", data={"card_id": self.card_id, "acquired_date": "2026-01-02"}, headers=self.headers).status_code, 404)
        cards.add_to_collection(self.card_id)
        holding = cards.list_collection("mtg")[0]
        self.assertEqual(self.client.get(f"/cards/pokemon/collection/{holding['id']}/lots").status_code, 404)

    def test_missing_history_records_unknown_instead_of_blocking_purchase(self):
        fields = {"card_id": self.card_id, "acquired_date": "2020-01-01", "price_source": "market_estimate"}
        self.assertFalse(self.post("/estimate", data=fields).json()["available"])
        self.assertEqual(self.post("", data=fields).status_code, 204)
        holding = cards.list_collection("mtg")[0]
        lot = purchases.lots("mtg", holding["id"])[0]
        self.assertEqual((lot["price_source"], lot["priced_qty"]), ("unknown", 0))
        self.assertIsNone(lot["unit_price_minor"])
        self.assertIsNone(lot["snapshot_date"])

    def test_get_query_preserves_all_filters_and_export_selection(self):
        cards.add_to_collection(self.card_id, acquired_price="1.00")
        cards.add_to_collection(self.card_id, foil=True, condition="LP")
        query = {"foil": "1", "condition": "LP", "set_code": "TST", "currency": "EUR", "page_size": "100"}
        response = self.client.get("/cards/mtg/collection", params=query)
        self.assertIn("1 matching holdings / 2 total", response.text)
        self.assertIn('value="1" selected', response.text)
        self.assertIn('value="100" selected', response.text)
        self.assertIn("Market &middot; EUR", response.text)
        exported = self.post("/export", data=query)
        import csv
        import io
        rows = list(csv.DictReader(io.StringIO(exported.text)))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["foil"], rows[0]["condition"]), ("1", "LP"))

    def test_http_correction_never_uses_aggregate_compatibility_scope(self):
        cards.add_to_collection(self.card_id, qty=2)
        cards.add_to_collection(self.card_id, acquired_price="5.00")
        holding = cards.list_collection("mtg")[0]
        before = purchases.lots("mtg", holding["id"])
        fields = {"collection_id": holding["id"], "acquired_price": "4.00"}
        self.assertEqual(self.post("/acquisition", data=fields).status_code, 422)
        self.assertEqual(purchases.lots("mtg", holding["id"]), before)
        self.assertEqual(self.post("/acquisition", data={**fields, "lot_id": before[0]["id"]}).status_code, 204)
        self.assertEqual(cards.list_collection("mtg")[0]["acquired_qty"], 1)
        self.assertEqual(self.post("/acquisition", data={**fields, "lot_id": 99999}).status_code, 404)

    def test_bad_purchase_fields_leave_no_holding(self):
        for invalid in ({"foil": 2}, {"qty": 0}, {"qty": 10000}, {"acquired_price": "-1"},
                        {"acquired_price": "NaN"}, {"acquired_currency": "GBP"},
                        {"condition": "bad"}, {"acquired_date": "2026-02-30"}):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.post("", data={"card_id": self.card_id, **invalid}).status_code, 422)
                self.assertEqual(cards.list_collection("mtg"), [])

    def test_page_filters_and_drawer_render_no_store(self):
        cards.add_to_collection(self.card_id, acquired_price="1.01", acquired_date="2026-01-01")
        cards.add_to_collection(self.card_id, acquired_date="2026-01-02", price_source="market_estimate", estimate_confirmed=True)
        response = self.client.get("/cards/mtg/collection")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("1 matching holdings / 1 total", response.text)
        filtered = self.post("/query", data={"q": "absent"})
        self.assertIn("0 matching holdings / 1 total", filtered.text)
        holding = cards.list_collection("mtg")[0]
        drawer = self.client.get(f"/cards/mtg/collection/{holding['id']}/lots")
        self.assertEqual(drawer.status_code, 200)
        self.assertIn("Actual paid", drawer.text)
        self.assertIn("Market estimate", drawer.text)
        self.assertIn('name="lot_id"', drawer.text)
        self.assertIn("price_history:manual", drawer.text)

    def test_legacy_inspector_add_always_creates_a_lot(self):
        self.assertEqual(self.post("", data={"card_id": self.card_id, "qty": 2, "acquired_price": "1.01"}).status_code, 204)
        self.assertEqual(self.post("", data={"card_id": self.card_id, "qty": 1, "acquired_price": "1.02"}).status_code, 204)
        holding = cards.list_collection("mtg")[0]
        lots = purchases.lots("mtg", holding["id"])
        self.assertEqual(len(lots), 2)
        self.assertEqual(self.post("/acquisition", data={"collection_id": holding["id"], "acquired_price": "3.00"}).status_code, 422)
        self.assertEqual(self.post("/acquisition", data={"collection_id": holding["id"], "lot_id": lots[0]["id"], "acquired_price": "3.00"}).status_code, 204)
        self.assertEqual(purchases.lots("mtg", holding["id"])[1], lots[1])

    def test_csv_preview_export_safe_filename_and_idempotent_apply(self):
        raw = b"external_id,qty,unit_price\nsynthetic-route,2,1.01\n"
        response = self.post("/import/preview", files={"upload": ("untrusted.csv", raw, "text/csv")}, data={"duplicate_mode": "add"})
        self.assertEqual(response.status_code, 200)
        token = re.search(r'name="token" value="([^"]+)"', response.text).group(1)
        self.assertEqual(cards.list_collection("mtg"), [])
        self.assertEqual(self.post("/import/apply", data={"token": token}).json(), {"added": 1, "already_applied": False})
        self.assertTrue(self.post("/import/apply", data={"token": token}).json()["already_applied"])
        exported = self.post("/export", data={"q": "Example"})
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.headers["Cache-Control"], "no-store")
        self.assertEqual(exported.headers["Content-Disposition"], 'attachment; filename="collection.csv"')
        self.assertIn("synthetic-route", exported.text)

    def test_delete_removes_holding_but_history_remains_local(self):
        cards.add_to_collection(self.card_id)
        holding = cards.list_collection("mtg")[0]
        self.assertEqual(self.post(f"/{holding['id']}/delete").status_code, 204)
        history = self.client.get("/cards/mtg/collection/removed/history")
        self.assertEqual(history.status_code, 200)
        self.assertIn("0 held / 1 recorded", history.text)
        self.assertEqual(history.headers["Cache-Control"], "no-store")


def serve_synthetic() -> None:
    import uvicorn

    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
        "LUIGI_WEB_CARDS_DB": str(Path(directory) / "synthetic.sqlite"),
        "LUIGI_WEB_UI_TOKEN": SYNTHETIC_SESSION,
    }):
        card_id = seed()
        cards.add_to_collection(card_id, qty=2, acquired_price="1.01", acquired_date="2026-01-01", notes="Synthetic purchase")
        cards.add_to_collection(card_id, acquired_date="2026-01-02", price_source="market_estimate", estimate_confirmed=True)
        app = fixture_app()

        @app.get("/")
        def preview_login():
            response = RedirectResponse("/cards/mtg/collection")
            response.set_cookie(auth.COOKIE_NAME, SYNTHETIC_SESSION, httponly=True, samesite="strict")
            response.set_cookie(auth.CSRF_COOKIE_NAME, SYNTHETIC_CSRF, samesite="strict")
            return response

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            print(f"Synthetic collection preview: http://127.0.0.1:{port}/", flush=True)
            config = uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False)
            uvicorn.Server(config).run(sockets=[listener])


if __name__ == "__main__":
    if "--serve-synthetic" in sys.argv:
        serve_synthetic()
    else:
        unittest.main()