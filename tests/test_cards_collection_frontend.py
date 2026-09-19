"""Synthetic browser-to-ASGI collection workflows; every browser request is intercepted."""
from __future__ import annotations

import csv
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from luigi_web import auth
from luigi_web.modules.cards import collection, purchases, repository as cards
from test_cards_collection_routes import fixture_app, seed, SYNTHETIC_SESSION, SYNTHETIC_CSRF

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class CollectionBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, {
            "LUIGI_WEB_CARDS_DB": str(Path(self.temp.name) / "synthetic.sqlite"),
            "LUIGI_WEB_UI_TOKEN": SYNTHETIC_SESSION,
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.card_id = seed()
        self.client = TestClient(fixture_app())
        self.addCleanup(self.client.close)
        self.requests = []

    def page(self, width=1440, height=900):
        context = self.browser.new_context(viewport={"width": width, "height": height})
        self.addCleanup(context.close)
        context.add_cookies([
            {"name": auth.COOKIE_NAME, "value": SYNTHETIC_SESSION, "url": "http://testserver"},
            {"name": auth.CSRF_COOKIE_NAME, "value": SYNTHETIC_CSRF, "url": "http://testserver"},
        ])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))

        def respond(route):
            request = route.request
            if urlsplit(request.url).netloc != "testserver":
                route.abort()
                return
            self.requests.append((request.method, request.url, request.post_data_buffer))
            response = self.client.request(request.method, request.url,
                                           headers=request.all_headers(), content=request.post_data_buffer)
            headers = dict(response.headers)
            for key in ("content-length", "content-encoding", "transfer-encoding"):
                headers.pop(key, None)
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("**/*", respond)
        page.goto("http://testserver/cards/mtg/collection")
        page.locator("[data-collection-page]").wait_for()
        return page

    def select_purchase(self, page):
        page.locator('[data-collection-open="collection-purchase"]').click()
        page.locator('#collection-purchase input[name="q"]').fill("Example")
        page.locator('[data-collection-select]').first.click()
        return page.locator('#collection-purchase [data-collection-purchase]')

    def test_estimate_and_one_lot_correction_desktop_and_mobile(self):
        for width, height in ((1440, 900), (390, 844)):
            with self.subTest(width=width):
                page = self.page(width, height)
                form = self.select_purchase(page)
                form.locator('[name="acquired_date"]').fill("2026-01-02")
                form.locator('[name="price_source"]').select_option("market_estimate")
                form.locator('[data-collection-estimate]').click()
                output = form.locator('[data-collection-estimate-output]')
                from playwright.sync_api import expect
                expect(output).to_contain_text("USD 1.02")
                form.locator('[name="estimate_confirmed"]').check()
                form.locator('button[type="submit"]').click()
                expect(page.locator('#collection-purchase')).not_to_be_visible()
                expect(page.locator('.collection-holdings')).to_contain_text("Estimated $1.02")
                page.locator('[data-collection-detail]').first.click()
                drawer = page.locator('#collection-detail')
                drawer.locator('summary').first.click()
                correction = drawer.locator('form').first
                correction.locator('[name="price_source"]').select_option("entered")
                correction.locator('[name="acquired_price"]').fill("1.03")
                correction.locator('button[type="submit"]').click()
                expect(drawer).not_to_be_visible()
                self.assertEqual(collection.page("mtg", {})["totals"]["actual_cost_minor"], 103)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                self.assertGreater(len(page.screenshot()), 1000)
                holding = cards.list_collection("mtg")[0]
                cards.remove_from_collection(holding["id"], game_code="mtg")
                page.close()

    def test_missing_history_can_be_recorded_as_unknown(self):
        page = self.page(390, 844)
        form = self.select_purchase(page)
        form.locator('[name="acquired_date"]').fill("2020-01-01")
        form.locator('[name="price_source"]').select_option("market_estimate")
        form.locator('[data-collection-estimate]').click()
        from playwright.sync_api import expect
        expect(form.locator('[data-collection-estimate-output]')).to_contain_text("Unknown cost")
        expect(form.locator('[name="price_source"]')).to_have_value("unknown")
        form.locator('button[type="submit"]').click()
        expect(page.locator('#collection-purchase')).not_to_be_visible()
        holding = cards.list_collection("mtg")[0]
        self.assertEqual(purchases.lots("mtg", holding["id"])[0]["price_source"], "unknown")

    def test_csv_preview_apply_filter_and_download(self):
        cards.add_to_collection(self.card_id, acquired_price="1.01")
        page = self.page()
        page.locator('[data-collection-open="collection-import"]').click()
        dialog = page.locator('#collection-import')
        dialog.locator('input[type="file"]').set_input_files({
            "name": "synthetic.csv", "mimeType": "text/csv",
            "buffer": b"external_id,qty,foil,condition,unit_price\nsynthetic-route,2,1,LP,2.02\n",
        })
        dialog.locator('[name="duplicate_mode"]').select_option("add")
        dialog.locator('[data-collection-preview] button[type="submit"]').click()
        from playwright.sync_api import expect
        expect(dialog.locator('[data-collection-preview-body]')).to_contain_text("1 purchases to add")
        self.assertEqual(len(cards.list_collection("mtg")), 1)
        dialog.locator('[data-collection-submit] button[type="submit"]').click()
        expect(dialog).not_to_be_visible()
        filters = page.locator('[data-collection-filters]')
        filters.locator('[name="foil"]').select_option("1")
        filters.locator('[name="condition"]').select_option("LP")
        filters.locator('button[type="submit"]').click()
        expect(page.locator('.cards-page-header')).to_contain_text("1 matching holdings / 2 total")
        with page.expect_download() as download_event:
            page.locator('[data-collection-export]').click()
        download = download_event.value
        self.assertEqual(download.suggested_filename, "collection.csv")
        rows = list(csv.DictReader(io.StringIO(Path(download.path()).read_text())))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["qty"], rows[0]["foil"], rows[0]["condition"]), ("2", "1", "LP"))
        self.assertTrue(any(url.endswith('/collection/query') and method == 'POST' for method, url, _ in self.requests))