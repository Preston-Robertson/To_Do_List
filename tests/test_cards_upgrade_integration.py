"""Actual-host Cards upgrade contracts, isolated from local data and startup."""
from __future__ import annotations

import csv
import functools
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
ISOLATED = "--isolated-case" in sys.argv
SESSION = "synthetic-upgrade-session"


class Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.stack = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        element = {"tag": tag, "attrs": dict(attrs), "parents": tuple(self.stack), "index": len(self.elements), "text": []}
        self.elements.append(element)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(element["index"])

    def handle_endtag(self, tag):
        for offset in range(len(self.stack) - 1, -1, -1):
            if self.elements[self.stack[offset]]["tag"] == tag:
                del self.stack[offset:]
                break

    def handle_data(self, data):
        self.text.append(data)
        for index in self.stack:
            self.elements[index]["text"].append(data)

    def select(self, tag=None, class_name=None, parent=None, **attrs):
        return [element for element in self.elements
                if (tag is None or element["tag"] == tag)
                and (class_name is None or class_name in element["attrs"].get("class", "").split())
                and (parent is None or parent["index"] in element["parents"])
                and all(element["attrs"].get(key) == value for key, value in attrs.items())]


def isolated_host(test):
    @functools.wraps(test)
    def run(self):
        if ISOLATED:
            return test(self)
        with tempfile.TemporaryDirectory(prefix="cards-upgrade-") as temporary:
            directory = Path(temporary)
            environment = {
                **{key: os.environ[key] for key in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR") if key in os.environ},
                **{key: temporary for key in (
                    "TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR", "HOME", "USERPROFILE",
                    "APPDATA", "LOCALAPPDATA", "XDG_DATA_HOME",
                )},
                "LUIGI_WEB_DATA_DIR": str(directory / "data"),
                "LUIGI_WEB_CARDS_DB": str(directory / "cards.sqlite"),
                "LUIGI_WEB_MODULES_FILE": str(directory / "modules.json"),
                "LUIGI_WEB_UI_TOKEN": SESSION,
                "PYTHON_DOTENV_DISABLED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
            }
            completed = subprocess.run(
                [sys.executable, "-B", str(Path(__file__).resolve()), "--isolated-case", test.__name__],
                cwd=directory, env=environment, capture_output=True, text=True,
                encoding="utf-8", timeout=90, check=False,
            )
            output = completed.stdout + completed.stderr
            output = re.sub(r'File "[^"\n]*[\\/]([^"\\/]+)"', r'File "\1"', output)
            for private_path in (temporary, str(ROOT), sys.prefix, sys.base_prefix):
                output = output.replace(private_path, "<isolated-path>")
            self.assertEqual(completed.returncode, 0, output)
    return run


class CardsUpgradeIntegrationTests(unittest.TestCase):
    def setUp(self):
        if not ISOLATED:
            return
        sys.path.insert(0, str(ROOT))
        directory = Path(os.environ["TEMP"]).resolve()

        def audit(event, arguments):
            if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname"}:
                frame = sys._getframe()
                while frame is not None:
                    if frame.f_code.co_name == "_fallback_socketpair" and Path(frame.f_code.co_filename).name == "socket.py":
                        return
                    frame = frame.f_back
                raise AssertionError("Host integration tests prohibit external networking")
            if event == "sqlite3.connect":
                if Path(arguments[0]).resolve() != Path(os.environ["LUIGI_WEB_CARDS_DB"]).resolve():
                    raise AssertionError("Only the synthetic Cards database may be opened")
            if event == "open" and isinstance(arguments[0], (str, bytes, os.PathLike)):
                candidate = Path(os.fsdecode(arguments[0])).resolve()
                if candidate.is_relative_to(directory):
                    return
                if candidate.name.startswith((".env", "LOCAL_")) or candidate.is_relative_to(ROOT / "data"):
                    raise AssertionError("Host integration tests prohibit private file access")

        sys.addaudithook(audit)
        self.enterContext(patch("dotenv.load_dotenv", return_value=False))
        self.enterContext(patch("dotenv.find_dotenv", return_value=""))
        from luigi_web.core.module_registry import BUILTIN_MODULE_IDS

        os.environ["LUIGI_WEB_MODULES"] = ",".join(BUILTIN_MODULE_IDS)
        from fastapi.testclient import TestClient
        from luigi_web import application, auth
        from luigi_web.modules.cards import repository

        self.app = application.app
        startup = self.enterContext(patch.object(application, "start_modules"))
        shutdown = self.enterContext(patch.object(application, "stop_modules"))
        self.addCleanup(startup.assert_not_called)
        self.addCleanup(shutdown.assert_not_called)
        self.cards = repository
        self.auth = auth
        self.builtin_ids = BUILTIN_MODULE_IDS
        self.cards.init_db()
        self.client = TestClient(self.app, follow_redirects=False)
        self.addCleanup(self.client.close)
        self.client.cookies.set(auth.COOKIE_NAME, SESSION)
        self.assert_private(self.client.get("/cards/mtg/collection"))
        self.csrf = self.client.cookies.get(auth.CSRF_COOKIE_NAME) or ""
        self.assertTrue(self.csrf)

    def assert_private(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text[:500])
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def post(self, url, *, headers=None, **kwargs):
        self.assertEqual(self.client.cookies.get(self.auth.CSRF_COOKIE_NAME), self.csrf)
        return self.client.post(url, headers={"X-CSRF-Token": self.csrf, **(headers or {})}, **kwargs)

    def seed_deck(self, count=1, game="mtg"):
        deck_id = self.cards.create_deck(game, "Example integration deck", "standard")
        card_ids = []
        with self.cards.transaction() as connection:
            for number in range(count):
                card_id = connection.execute(
                    "INSERT INTO cards(game_code,external_id,source,name,set_code,collector_number,"
                    "type_line,cmc,mana_cost,raw_json,price_usd_minor,price_usd_foil_minor,price_updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (game, f"synthetic-{deck_id}-{number}", "manual", f"Example card {number:03d}",
                     "TST", str(number), "Artifact", 2 if number < count - 1 else None,
                     "{2}" if number < count - 1 else "", json.dumps({}),
                     125 if number < count - 1 else None, None, "2026-01-02T12:00:00Z"),
                ).lastrowid
                card_ids.append(card_id)
            connection.executemany(
                "INSERT INTO deck_cards(deck_id,card_id,qty,board,category) VALUES (?,?,1,'main',?)",
                [(deck_id, card_id, "Example category") for card_id in card_ids],
            )
        return deck_id, card_ids

    def slots(self, deck_id):
        return sorted((row["card_id"], row["qty"], row["board"], row["category"])
                      for row in self.cards.list_deck_cards(deck_id))

    def database_state(self):
        with self.cards._connect() as connection:
            return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
                    for table in ("decks", "deck_cards", "deck_versions", "collection", "collection_lots", "collection_imports")}

    def protected_requests(self):
        from luigi_web.modules.cards import purchases

        deck_id, card_ids = self.seed_deck(2)
        version_id = self.save_version(deck_id)
        restore_url, fields = self.restore_form(deck_id, version_id)
        self.cards.add_to_collection(card_ids[0], acquired_price="1.11", acquired_date="2026-01-01")
        holding, = self.cards.list_collection("mtg")
        lot, = purchases.lots("mtg", holding["id"])
        deck_url = f"/cards/mtg/decks/{deck_id}"
        collection_url = "/cards/mtg/collection"
        raw = f"external_id,qty\nsynthetic-{deck_id}-1,1\n".encode("utf-8")
        preview = self.post(collection_url + "/import/preview", files={"upload": ("example.csv", raw, "text/csv")},
                            data={"duplicate_mode": "add"})
        self.assert_private(preview)
        token = Markup(preview.text).select("input", name="token")[0]["attrs"]["value"]
        gets = [deck_url + suffix for suffix in (
            "", "/build", "/build.csv", "/analysis.json", "/versions", f"/versions/{version_id}/preview",
        )] + [collection_url, collection_url + f"/{holding['id']}/lots", collection_url + "/removed/history"]
        posts = [
            (deck_url + "/versions", {"data": {"label": "Blocked example"}}),
            (restore_url, {"data": {**fields, "confirm": "1"}}),
            (deck_url + "/duplicate", {"data": {"name": "Blocked copy"}}),
            (collection_url, {"data": {"card_id": card_ids[0]}}),
            (collection_url + "/acquisition", {"data": {"collection_id": holding["id"], "lot_id": lot["id"], "acquired_price": "1.23"}}),
            (collection_url + f"/{holding['id']}/delete", {}),
            (collection_url + "/query", {"data": {"q": "Example"}}),
            (collection_url + "/search", {"data": {"q": "Example"}}),
            (collection_url + "/estimate", {"data": {"card_id": card_ids[0], "acquired_date": "2026-01-02"}}),
            (collection_url + "/export", {}),
            (collection_url + "/import/preview", {"data": {"duplicate_mode": "add"}, "files": {"upload": ("example.csv", raw, "text/csv")}}),
            (collection_url + "/import/apply", {"data": {"token": token}}),
        ]
        return gets, posts

    @isolated_host
    def test_new_pages_and_mutations_require_auth_but_assets_are_public(self):
        from fastapi.testclient import TestClient

        gets, posts = self.protected_requests()
        before = self.database_state()
        anonymous = TestClient(self.app, follow_redirects=False)
        self.addCleanup(anonymous.close)
        for url in gets:
            with self.subTest(method="GET", url=url):
                self.assert_private(anonymous.get(url), 401)
        for url, kwargs in posts:
            with self.subTest(method="POST", url=url):
                self.assert_private(anonymous.post(url, **kwargs), 401)
        browser = anonymous.get(gets[0], headers={"Accept": "text/html"})
        self.assert_private(browser, 303)
        self.assertEqual(browser.headers["location"], "/login")
        for asset in ("/module-assets/cards/analysis.css", "/module-assets/cards/versions.css",
                      "/static/css/collection.css", "/static/js/collection.js", "/static/js/cards.js"):
            with self.subTest(asset=asset):
                response = anonymous.get(asset)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.content)
        self.assertEqual(self.database_state(), before)

    @isolated_host
    def test_missing_mismatched_csrf_and_cross_origin_collection_mutations_do_not_write(self):
        _, posts = self.protected_requests()
        before = self.database_state()
        for url, kwargs in posts:
            for headers in ({}, {"X-CSRF-Token": "synthetic-wrong-csrf"}):
                with self.subTest(url=url, csrf="missing" if not headers else "mismatched"):
                    response = self.client.post(url, headers=headers, **kwargs)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(self.database_state(), before)
            if url.startswith("/cards/mtg/collection"):
                with self.subTest(url=url, origin="cross-origin"):
                    response = self.post(url, headers={"Origin": "https://example.invalid"}, **kwargs)
                    self.assert_private(response, 403)
                    self.assertEqual(self.database_state(), before)

    @isolated_host
    def test_bearer_mutations_remain_supported_without_session_or_csrf_cookie(self):
        from fastapi.testclient import TestClient

        deck_id, card_ids = self.seed_deck()
        bearer = TestClient(self.app, follow_redirects=False)
        self.addCleanup(bearer.close)
        headers = {"Authorization": "Bearer " + SESSION}
        response = bearer.post(f"/cards/mtg/decks/{deck_id}/versions", json={"label": "Example API version"}, headers=headers)
        self.assert_private(response)
        self.assertTrue(response.json()["version_id"])
        bearer.cookies.clear()
        purchase = bearer.post("/cards/mtg/collection", data={"card_id": card_ids[0]},
                               headers={**headers, "HX-Request": "true"})
        self.assert_private(purchase, 204)
        self.assertEqual(len(self.cards.list_collection("mtg")), 1)
        self.assertIsNone(bearer.cookies.get(self.auth.COOKIE_NAME))

    @isolated_host
    def test_cross_game_deck_version_and_collection_ids_are_rejected_without_writes(self):
        from luigi_web.modules.cards import purchases

        deck_id, card_ids = self.seed_deck()
        foreign_deck_id, foreign_card_ids = self.seed_deck(game="pokemon")
        sibling_deck_id = self.cards.create_deck("mtg", "Example other deck")
        version_id = self.save_version(deck_id)
        _, hashes = self.restore_form(deck_id, version_id)
        self.cards.add_to_collection(card_ids[0])
        self.cards.add_to_collection(foreign_card_ids[0])
        holding, = self.cards.list_collection("mtg")
        foreign_holding, = self.cards.list_collection("pokemon")
        foreign_lot, = purchases.lots("pokemon", foreign_holding["id"])
        before = self.database_state()
        wrong_deck = f"/cards/pokemon/decks/{deck_id}"
        for suffix in ("", "/build", "/build.csv", "/analysis.json", "/versions", f"/versions/{version_id}/preview"):
            with self.subTest(path=wrong_deck + suffix):
                self.assert_private(self.client.get(wrong_deck + suffix), 404)
        for game, other_id in (("pokemon", foreign_deck_id), ("mtg", sibling_deck_id)):
            url = f"/cards/{game}/decks/{other_id}/versions"
            with self.subTest(game=game, other_deck=other_id):
                self.assert_private(self.client.get(url, params={"before": version_id}), 404)
                self.assert_private(self.client.get(url + f"/{version_id}/preview"), 404)
                self.assert_private(self.post(url + f"/{version_id}/restore", data={**hashes, "confirm": "1"}), 404)
        for suffix, fields in (("/duplicate", {}), ("/versions", {"label": "Wrong game"}),
                               (f"/versions/{version_id}/restore", {**hashes, "confirm": "1"})):
            self.assert_private(self.post(wrong_deck + suffix, data=fields), 404)
        self.assert_private(self.client.get(f"/cards/mtg/decks/{deck_id}/build", params={"reserve_deck_id": foreign_deck_id}), 404)
        foreign_url = "/cards/pokemon/collection"
        self.assert_private(self.client.get(foreign_url + f"/{holding['id']}/lots"), 404)
        for url, fields in (
            (foreign_url, {"card_id": card_ids[0]}),
            (foreign_url + "/estimate", {"card_id": card_ids[0], "acquired_date": "2026-01-02"}),
            (foreign_url + "/acquisition", {"collection_id": holding["id"], "acquired_price": "1.23"}),
            (foreign_url + f"/{holding['id']}/delete", {}),
            ("/cards/mtg/collection/acquisition", {"collection_id": holding["id"], "lot_id": foreign_lot["id"], "acquired_price": "1.23"}),
        ):
            with self.subTest(path=url):
                self.assert_private(self.post(url, data=fields), 404)
        self.assertEqual(self.database_state(), before)

    def assert_assets(self, response, expected):
        markup = Markup(response.text)
        targets = {element["attrs"].get("href", "") for element in markup.select("link")}
        targets.update(element["attrs"].get("src", "") for element in markup.select("script"))
        for path in expected:
            matches = [target for target in targets if urlsplit(target).path == path]
            self.assertEqual(len(matches), 1, path)
            asset = self.client.get(matches[0])
            self.assertEqual(asset.status_code, 200, path)
            self.assertTrue(asset.content, path)
            self.assertIn("text/css" if path.endswith(".css") else "javascript", asset.headers["content-type"])

    def seed_prices(self):
        deck_id, card_ids = self.seed_deck(2)
        with self.cards.transaction() as connection:
            connection.executemany(
                "INSERT INTO price_history(card_id,snapshot_date,price_usd_minor,price_usd_foil_minor,price_eur_minor) "
                "VALUES (?,'2026-01-02',?,?,?)",
                [(card_ids[0], 102, 204, 99), (card_ids[1], 303, None, 150)],
            )
        return deck_id, card_ids

    @isolated_host
    def test_collection_pagination_renders_all_501_actual_holdings_and_totals(self):
        from luigi_web.modules.cards import purchases

        _, card_ids = self.seed_deck(501)
        with self.cards.transaction() as connection:
            for card_id in card_ids:
                purchases.record(connection, card_id, qty=1, price_source="unknown")
            expected_ids = {row[0] for row in connection.execute("SELECT id FROM collection")}
        self.assertEqual(len(expected_ids), 501)
        seen = set()
        for page_number, expected_count in ((1, 200), (2, 200), (3, 101)):
            with self.subTest(page=page_number):
                response = self.post("/cards/mtg/collection/query", data={"page": page_number, "page_size": 200})
                self.assert_private(response)
                markup = Markup(response.text)
                table, = markup.select(class_name="collection-table")
                body, = markup.select("tbody", parent=table)
                self.assertEqual(len(markup.select("tr", parent=body)), expected_count)
                ids = {int(element["attrs"]["data-collection-detail"])
                       for element in markup.select("button", class_name="deck-card-name", parent=body)}
                self.assertEqual(len(ids), expected_count)
                self.assertFalse(seen.intersection(ids))
                seen.update(ids)
                self.assertIn("501 matching holdings / 501 total", response.text)
                self.assertIn("501 / 501 copies", response.text)
                self.assertIn(f"Page {page_number} of 3", response.text)
        self.assertEqual(seen, expected_ids)
        last_page = self.client.get("/cards/mtg/collection", params={"page": 3, "page_size": 200})
        self.assert_private(last_page)
        markup = Markup(last_page.text)
        next_button, = markup.select("button", **{"aria-label": "Next page"})
        self.assertIn("disabled", next_button["attrs"])
        self.assertIn("Example card 500", last_page.text)
        self.assert_assets(last_page, ["/static/css/collection.css", "/static/js/collection.js"])
        filtered = self.post("/cards/mtg/collection/query", data={"q": "Example card 500"})
        self.assert_private(filtered)
        self.assertIn("1 matching holdings / 501 total", filtered.text)
        self.assertIn("1 / 501 copies", filtered.text)

    @isolated_host
    def test_estimate_post_requires_exact_day_currency_and_foil_without_fallback(self):
        _, card_ids = self.seed_prices()
        cases = (
            (card_ids[0], "2026-01-02", "USD", 0, 102),
            (card_ids[0], "2026-01-02", "USD", 1, 204),
            (card_ids[0], "2026-01-02", "EUR", 0, 99),
            (card_ids[0], "2026-01-02", "EUR", 1, None),
            (card_ids[0], "2026-01-01", "USD", 0, None),
            (card_ids[0], "2026-01-03", "USD", 0, None),
            (card_ids[1], "2026-01-02", "USD", 0, 303),
            (card_ids[1], "2026-01-02", "USD", 1, None),
        )
        for card_id, day, currency, foil, price in cases:
            with self.subTest(card=card_id, day=day, currency=currency, foil=foil):
                response = self.post("/cards/mtg/collection/estimate", data={
                    "card_id": card_id, "acquired_date": day, "currency": currency, "foil": foil,
                })
                self.assert_private(response)
                estimate = response.json()
                self.assertEqual(estimate["available"], price is not None)
                self.assertEqual(estimate["unit_price_minor"], price)
                self.assertEqual(estimate["snapshot_date"], day)
                self.assertEqual(estimate["currency"], currency)
                self.assertEqual(estimate["foil"], bool(foil))
                self.assertEqual(estimate["price_source"], "unknown" if price is None else "market_estimate")
                self.assertEqual(estimate["snapshot_source"], None if price is None else "price_history:manual")
        self.assertEqual(self.cards.list_collection("mtg"), [])

    @isolated_host
    def test_purchase_lots_keep_distinct_dates_sources_and_scoped_correction(self):
        from luigi_web.modules.cards import purchases

        _, card_ids = self.seed_prices()
        url = "/cards/mtg/collection"
        estimated = {"card_id": card_ids[0], "qty": 3, "acquired_date": "2026-01-02", "price_source": "market_estimate"}
        self.assert_private(self.post(url, data=estimated), 422)
        self.assertEqual(self.cards.list_collection("mtg"), [])
        for fields in (
            {"card_id": card_ids[0], "qty": 2, "acquired_date": "2026-01-01", "acquired_price": "1.11"},
            {**estimated, "estimate_confirmed": "true"},
            {"card_id": card_ids[0], "qty": 1, "acquired_date": "2026-01-03", "price_source": "market_estimate"},
        ):
            self.assert_private(self.post(url, data=fields, headers={"HX-Request": "true"}), 204)
        holding, = self.cards.list_collection("mtg")
        self.assertEqual((holding["qty"], holding["acquired_qty"]), (6, 5))
        lots = purchases.lots("mtg", holding["id"])
        by_day = {lot["acquired_date"]: lot for lot in lots}
        self.assertEqual(len(by_day), 3)
        for day, source, qty, price in (("2026-01-01", "entered", 2, 111),
                                       ("2026-01-02", "market_estimate", 3, 102),
                                       ("2026-01-03", "unknown", 1, None)):
            lot = by_day[day]
            self.assertEqual((lot["price_source"], lot["qty"], lot["unit_price_minor"]), (source, qty, price))
            self.assertEqual(lot["priced_qty"], 0 if price is None else qty)
        self.assertEqual(by_day["2026-01-02"]["snapshot_date"], "2026-01-02")
        self.assertEqual(by_day["2026-01-02"]["snapshot_source"], "price_history:manual")
        self.assertIsNone(by_day["2026-01-03"]["snapshot_date"])
        self.assertIsNone(by_day["2026-01-03"]["snapshot_source"])
        drawer = self.client.get(url + f"/{holding['id']}/lots")
        self.assert_private(drawer)
        for text in ("2026-01-01", "2026-01-02", "2026-01-03", "Actual paid", "Market estimate", "Unknown", "price_history:manual"):
            self.assertIn(text, drawer.text)
        correction = {"collection_id": holding["id"], "acquired_price": "1.23", "acquired_date": "2026-01-01"}
        self.assert_private(self.post(url + "/acquisition", data=correction), 422)
        self.assertEqual(purchases.lots("mtg", holding["id"]), lots)
        self.assert_private(self.post(url + "/acquisition", data={**correction, "lot_id": by_day["2026-01-01"]["id"]},
                                      headers={"HX-Request": "true"}), 204)
        corrected = {lot["acquired_date"]: lot for lot in purchases.lots("mtg", holding["id"])}
        self.assertEqual(corrected["2026-01-01"]["unit_price_minor"], 123)
        self.assertEqual(corrected["2026-01-02"], by_day["2026-01-02"])
        self.assertEqual(corrected["2026-01-03"], by_day["2026-01-03"])
        self.assert_private(self.post(url + f"/{holding['id']}/delete", headers={"HX-Request": "true"}), 204)
        self.assertEqual(self.cards.list_collection("mtg"), [])
        removed = self.client.get(url + "/removed/history")
        self.assert_private(removed)
        self.assertIn("price_history:manual", removed.text)
        self.assertEqual(len(purchases.lots("mtg", deleted=True)), 3)

    @isolated_host
    def test_collection_csv_preview_apply_is_idempotent_and_exports_selected_lots(self):
        from luigi_web.modules.cards import purchases

        deck_id, card_ids = self.seed_deck(2)
        url = "/cards/mtg/collection"
        raw = ("external_id,qty,foil,condition,acquired_date,unit_price,currency,price_source\n"
               f"synthetic-{deck_id}-0,2,0,NM,2026-01-01,1.11,USD,entered\n"
               f"synthetic-{deck_id}-1,1,1,LP,2026-01-03,,USD,unknown\n").encode("utf-8")
        preview = self.post(url + "/import/preview", files={"upload": ("example.csv", raw, "text/csv")},
                            data={"duplicate_mode": "add"})
        self.assert_private(preview)
        token_field, = Markup(preview.text).select("input", name="token")
        token = token_field["attrs"]["value"]
        self.assertEqual(self.cards.list_collection("mtg"), [])
        applied = self.post(url + "/import/apply", data={"token": token})
        self.assert_private(applied)
        self.assertEqual(applied.json(), {"added": 2, "already_applied": False})
        holdings = self.cards.list_collection("mtg")
        self.assertEqual({holding["card_id"] for holding in holdings}, set(card_ids))
        self.assertEqual(sum(holding["qty"] for holding in holdings), 3)
        before_lots = {holding["id"]: purchases.lots("mtg", holding["id"]) for holding in holdings}
        repeated = self.post(url + "/import/apply", data={"token": token})
        self.assert_private(repeated)
        self.assertEqual(repeated.json(), {"added": 2, "already_applied": True})
        self.assertEqual(self.cards.list_collection("mtg"), holdings)
        self.assertEqual({holding["id"]: purchases.lots("mtg", holding["id"]) for holding in holdings}, before_lots)
        exported = self.post(url + "/export")
        self.assert_private(exported)
        self.assertEqual(exported.headers["content-disposition"], 'attachment; filename="collection.csv"')
        self.assertIn("text/csv", exported.headers["content-type"])
        rows = list(csv.DictReader(io.StringIO(exported.text)))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["schema"] for row in rows}, {"luigi-collection-v1"})
        self.assertEqual({(row["acquired_date"], row["qty"], row["unit_price"], row["price_source"]) for row in rows}, {
            ("2026-01-01", "2", "1.11", "entered"), ("2026-01-03", "1", "", "unknown"),
        })
        selected = self.post(url + "/export", data={"foil": "1", "condition": "LP"})
        self.assert_private(selected)
        selected_row, = list(csv.DictReader(io.StringIO(selected.text)))
        self.assertEqual((selected_row["external_id"], selected_row["unit_price"]), (f"synthetic-{deck_id}-1", ""))
        self.assertFalse(any(Path(os.environ["TEMP"]).rglob("*.csv")))

    @isolated_host
    def test_collection_search_json_and_htmx_partial_are_mounted_and_private(self):
        _, card_ids = self.seed_deck(2)
        url = "/cards/mtg/collection/search"
        response = self.post(url, data={"q": "Example card 001"})
        self.assert_private(response)
        self.assertEqual([row["id"] for row in response.json()], [card_ids[1]])
        partial = self.post(url, data={"q": "Example card 001"}, headers={"HX-Target": "collection-search-results"})
        self.assert_private(partial)
        self.assertIn("text/html", partial.headers["content-type"])
        self.assertIn("Example card 001", partial.text)
        self.assertNotIn("Example card 000", partial.text)
        self.assertEqual(self.cards.list_collection("mtg"), [])

    @isolated_host
    def test_deck_build_versions_links_render_namespaced_templates_and_assets(self):
        deck_id, card_ids = self.seed_deck(2)
        self.cards.add_to_collection(card_ids[0])
        url = f"/cards/mtg/decks/{deck_id}"
        response = self.client.get(url)
        self.assert_private(response)
        links = Markup(response.text).select("a")
        for suffix, asset in (("build", "analysis.css"), ("versions", "versions.css")):
            with self.subTest(page=suffix):
                target = url + "/" + suffix
                self.assertTrue(any(link["attrs"].get("href") == target for link in links))
                page = self.client.get(target, headers={"Accept": "text/html"})
                self.assert_private(page)
                self.assertIn("text/html", page.headers["content-type"])
                self.assertTrue(Markup(page.text).select("html")[0]["attrs"].get("lang"))
                self.assert_assets(page, ["/module-assets/cards/" + asset, "/static/js/cards.js"])
        build = self.client.get(url + "/build")
        self.assertIn("Unknown", build.text)
        export_button, = Markup(build.text).select("button", formaction=url + "/build.csv")
        exported = self.client.get(export_button["attrs"]["formaction"])
        self.assert_private(exported)
        rows = list(csv.DictReader(io.StringIO(exported.text)))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["name"], rows[0]["missing_qty"]), ("Example card 001", "1"))
        self.assertEqual((rows[0]["unit_price_minor"], rows[0]["missing_cost_minor"]), ("", ""))
        self.assertIn("attachment", exported.headers["content-disposition"])

    @isolated_host
    def test_duplicate_form_is_a_valid_csrf_mutation_and_preserves_original(self):
        deck_id, card_ids = self.seed_deck(2)
        self.cards.add_card_to_deck(deck_id, card_ids[0], qty=2, board="side", category="Example side")
        self.cards.add_card_to_deck(deck_id, card_ids[1], qty=3, board="maybe", category="Example maybe")
        self.cards.set_deck_tags(deck_id, ["Example tag"])
        before = self.slots(deck_id)
        url = f"/cards/mtg/decks/{deck_id}"
        page = self.client.get(url)
        self.assert_private(page)
        form, = Markup(page.text).select("form", action=url + "/duplicate")
        self.assertEqual(form["attrs"]["method"], "post")
        self.assertEqual(form["attrs"]["hx-post"], form["attrs"]["action"])
        response = self.post(form["attrs"]["action"], headers={"HX-Request": "true"})
        self.assert_private(response, 204)
        destination = response.headers["hx-redirect"]
        clone_id = int(destination.rsplit("/", 1)[1])
        self.assertNotEqual(clone_id, deck_id)
        self.assertEqual(self.slots(clone_id), before)
        self.assertEqual(self.slots(deck_id), before)
        clone = self.client.get(destination)
        self.assert_private(clone)
        self.assertIn("Example integration deck (copy)", clone.text)
        self.assertIn("Example tag", clone.text)

    def save_version(self, deck_id):
        url = f"/cards/mtg/decks/{deck_id}/versions"
        response = self.post(url, data={"label": "Example saved version"})
        self.assert_private(response, 303)
        self.assertEqual(urlsplit(response.headers["location"]).path, url)
        version_id = int(parse_qs(urlsplit(response.headers["location"]).query)["before"][0])
        saved_page = self.client.get(response.headers["location"])
        self.assert_private(saved_page)
        self.assertIn("Example saved version", saved_page.text)
        return version_id

    def restore_form(self, deck_id, version_id):
        url = f"/cards/mtg/decks/{deck_id}/versions/{version_id}"
        preview = self.client.get(url + "/preview")
        self.assert_private(preview)
        markup = Markup(preview.text)
        form, = markup.select("form", action=url + "/restore")
        fields = {element["attrs"]["name"]: element["attrs"]["value"]
                  for element in markup.select("input", parent=form, type="hidden")}
        for name in ("expected_current_hash", "expected_version_hash"):
            self.assertRegex(fields[name], r"^[a-f0-9]{64}$")
        return form["attrs"]["action"], fields

    def edit_quantity(self, deck_id, qty):
        slot = self.cards.list_deck_cards(deck_id)[0]
        response = self.post(
            f"/cards/mtg/decks/{deck_id}/cards/{slot['id']}",
            data={"qty": qty, "category": slot["category"]}, headers={"HX-Request": "true"},
        )
        self.assert_private(response)
        stats, = Markup(response.text).select(id="deck-stats")
        self.assertEqual(stats["attrs"].get("hx-swap-oob"), "outerHTML")

    @isolated_host
    def test_versions_save_html_preview_confirm_restore_and_backup(self):
        deck_id, _ = self.seed_deck()
        before = self.slots(deck_id)
        version_id = self.save_version(deck_id)
        self.edit_quantity(deck_id, 5)
        changed = self.slots(deck_id)
        self.assertNotEqual(changed, before)
        target, fields = self.restore_form(deck_id, version_id)
        self.assertEqual(self.slots(deck_id), changed)
        self.assert_private(self.post(target, data=fields), 422)
        self.assertEqual(self.slots(deck_id), changed)
        restored = self.post(target, data={**fields, "confirm": "1"})
        self.assert_private(restored, 303)
        self.assertEqual(self.slots(deck_id), before)
        state = self.client.get(f"/cards/mtg/decks/{deck_id}/versions", headers={"Accept": "application/json"})
        self.assert_private(state)
        versions = state.json()["versions"]
        self.assertEqual(len(versions), 2)
        backup, = [version for version in versions if version["id"] != version_id]
        self.assertEqual(backup["label"], "Before restore")
        backup_target, backup_fields = self.restore_form(deck_id, backup["id"])
        self.assert_private(self.post(backup_target, data={**backup_fields, "confirm": "1"}), 303)
        self.assertEqual(self.slots(deck_id), changed)
        self.assertEqual(self.client.cookies.get(self.auth.CSRF_COOKIE_NAME), self.csrf)

    @isolated_host
    def test_stale_version_preview_rejects_restore_without_writes(self):
        deck_id, _ = self.seed_deck()
        version_id = self.save_version(deck_id)
        self.edit_quantity(deck_id, 2)
        target, fields = self.restore_form(deck_id, version_id)
        self.edit_quantity(deck_id, 3)
        changed = self.slots(deck_id)
        self.assert_private(self.post(target, data={**fields, "confirm": "1"}), 409)
        self.assertEqual(self.slots(deck_id), changed)
        state = self.client.get(f"/cards/mtg/decks/{deck_id}/versions", headers={"Accept": "application/json"})
        self.assert_private(state)
        self.assertEqual([version["id"] for version in state.json()["versions"]], [version_id])

    @isolated_host
    def test_deck_keeps_all_41_rows_and_40_plus_1_stack_rail(self):
        deck_id, card_ids = self.seed_deck(41)
        response = self.client.get(f"/cards/mtg/decks/{deck_id}")
        self.assert_private(response)
        markup = Markup(response.text)
        tables = markup.select(class_name="deck-card-table")
        self.assertEqual(len(tables), 1)
        body = markup.select("tbody", parent=tables[0])[0]
        self.assertEqual(len(markup.select("tr", parent=body)), 41)
        rails = markup.select(class_name="deck-stack-board-columns")
        self.assertEqual(len(rails), 1)
        lanes = markup.select(class_name="deck-stack-list", parent=rails[0])
        self.assertEqual([len(markup.select("button", parent=lane)) for lane in lanes], [40, 1])
        stack_cards = markup.select(class_name="deck-stack-card")
        actual_ids = {int(element["attrs"]["data-card-detail-url"].split("/cards/")[2].split("/")[0])
                      for element in stack_cards}
        self.assertEqual(actual_ids, set(card_ids))
        self.assertEqual(len(markup.select(**{"data-deck-view": "table"})), 1)
        self.assertEqual(len(markup.select(**{"data-deck-view": "stacks"})), 1)

    @isolated_host
    def test_deck_stats_render_mana_unknown_advisory_and_separate_boards(self):
        deck_id, card_ids = self.seed_deck(41)
        self.cards.add_card_to_deck(deck_id, card_ids[0], qty=2, board="side")
        self.cards.add_card_to_deck(deck_id, card_ids[0], qty=3, board="maybe")
        url = f"/cards/mtg/decks/{deck_id}"
        response = self.client.get(url)
        self.assert_private(response)
        markup = Markup(response.text)
        stats, = markup.select(id="deck-stats")
        totals, = markup.select(class_name="analysis-totals", parent=stats)
        displayed_counts = {}
        for group in markup.select("div", parent=totals):
            label, = markup.select("dt", parent=group)
            value, = markup.select("dd", parent=group)
            displayed_counts["".join(label["text"]).strip()] = "".join(value["text"]).strip()
        self.assertEqual({label: displayed_counts[label] for label in ("Main + commander", "Sideboard", "Maybeboard")},
                         {"Main + commander": "41", "Sideboard": "2", "Maybeboard": "3"})
        self.assertEqual(len(markup.select("meter", **{"aria-label": "Mana 2: 40 copies", "value": "40"})), 1)
        self.assertEqual(len(markup.select("meter", **{"aria-label": "Mana Unknown: 1 copies", "value": "1"})), 1)
        text = " ".join(" ".join(markup.text).split())
        for label in ("Mana curve", "Unknown", "Advisory", "Mainboard", "Sideboard", "Maybeboard"):
            self.assertIn(label.lower(), text.lower())
        analysis_response = self.client.get(url + "/analysis.json")
        self.assert_private(analysis_response)
        analysis = analysis_response.json()
        self.assertEqual(analysis["counts"], {
            "main": 41, "commander": 0, "playable": 41, "side": 2, "maybe": 3, "all": 46,
        })
        self.assertEqual(analysis["mana_curve"]["buckets"], [
            {"label": "2", "quantity": 40}, {"label": "Unknown", "quantity": 1},
        ])
        self.assertGreater(analysis["advisory"]["severity_counts"]["unknown"], 0)
        self.assertFalse(analysis["advisory"]["writes_blocked"])

    @isolated_host
    def test_composition_mounts_new_routes_once_in_actual_host(self):
        for module_id in self.builtin_ids:
            self.assertTrue(self.app.state.modules.is_enabled(module_id), module_id)
        declarations = [
            (method, getattr(route, "path", "")) for route in self.app.routes
            for method in getattr(route, "methods", ()) or ()
        ]
        self.assertEqual(len(declarations), len(set(declarations)))
        expected = {
            ("GET", "/cards/{game_code}/decks/{deck_id}/build"),
            ("GET", "/cards/{game_code}/decks/{deck_id}/build.csv"),
            ("GET", "/cards/{game_code}/decks/{deck_id}/analysis.json"),
            ("GET", "/cards/{game_code}/decks/{deck_id}/versions"),
            ("POST", "/cards/{game_code}/decks/{deck_id}/versions"),
            ("GET", "/cards/{game_code}/decks/{deck_id}/versions/{version_id}/preview"),
            ("POST", "/cards/{game_code}/decks/{deck_id}/versions/{version_id}/restore"),
            ("POST", "/cards/{game_code}/decks/{deck_id}/duplicate"),
            ("GET", "/cards/{game_code}/collection/{collection_id}/lots"),
            ("GET", "/cards/{game_code}/collection/removed/history"),
            *(("POST", "/cards/{game_code}/collection/" + suffix) for suffix in (
                "query", "search", "estimate", "export", "import/preview", "import/apply",
            )),
        }
        self.assertTrue(expected.issubset(set(declarations)), expected.difference(declarations))
        self.assertFalse(any("/cards/cards" in path for _, path in declarations))
        self.assert_private(self.client.get("/cards/mtg/collection"))
        self.assertTrue(self.client.cookies.get(self.auth.CSRF_COOKIE_NAME))


if __name__ == "__main__":
    if ISOLATED:
        suite = unittest.defaultTestLoader.loadTestsFromName(
            "CardsUpgradeIntegrationTests." + sys.argv[sys.argv.index("--isolated-case") + 1],
            module=sys.modules[__name__],
        )
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(not result.wasSuccessful())
    unittest.main()