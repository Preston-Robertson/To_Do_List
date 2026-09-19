"""Offline browser checks with synthetic card stacks and local assets only."""
from pathlib import Path
import unittest

from luigi_web.modules.cards import routes

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "luigi_web" / "core" / "static"


def stack_fixture(deck_id=1):
    categories = []
    for category in range(3):
        count = 40 if category < 2 else 20
        rows = [{"id": category * 40 + index + 1, "card_id": category * 40 + index + 1,
                 "name": f"Example Card {category}-{index:02d}", "qty": 1,
                 "category": f"Example category {category}", "owned_qty": 0,
                 "type_line": "Artifact", "set_code": "TST", "collector_number": str(index),
                 "price_usd_minor": 100, "mana_cost": "{2}",
                 "image_small": f"https://cards.scryfall.io/synthetic/small/{category}/{index}.png",
                 "image_normal": f"https://cards.scryfall.io/synthetic/normal/{category}/{index}.png"}
                for index in range(count)]
        categories.append({"label": f"Example category {category}", "quantity": count,
                           "price_minor": count * 100, "cards": rows,
                           "stack_lanes": [{"cards": rows, "quantity": count, "price_minor": count * 100}]})
    panel = routes.templates.get_template("cards/partials/deck_cards.html").render(
        deck={"id": deck_id}, current_game="mtg", total_qty=100, total_price_minor=10000,
        deck_sections=[{"code": "main", "label": "Mainboard", "quantity": 100,
                        "price_minor": 10000, "categories": categories}],
    )
    return ('<!doctype html><html><head><meta name="viewport" content="width=device-width">'
            '<link rel="stylesheet" href="/cards.css"><style>'
            '* {box-sizing:border-box} body {margin:0;padding:16px;background:#171a19;color:white}'
            'main {min-width:0} :root {--cards-accent:#4fb78c;--text:#fff;--text-muted:#bbb}'
            '</style><script src="/cards.js" defer></script></head><body><main data-cards-workspace>'
            + panel + '<dialog class="cards-dialog" id="card-detail-dialog">'
            '<div id="card-detail-content"></div></dialog></main></body></html>')


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class CardsLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def page(self, width=1440, height=900, storage_failure=False):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        self.addCleanup(page.close)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        if storage_failure:
            page.add_init_script("""Object.defineProperty(window, 'localStorage', {
                get() { throw new DOMException('Unavailable', 'SecurityError'); }
            });""")

        def respond(route):
            url = route.request.url
            if url == "http://cards.test/cards.css":
                route.fulfill(body=(STATIC / "css" / "cards.css").read_text(), content_type="text/css")
            elif url == "http://cards.test/cards.js":
                route.fulfill(body=(STATIC / "js" / "cards.js").read_text(), content_type="text/javascript")
            elif url == "http://cards.test/":
                route.fulfill(body=stack_fixture(), content_type="text/html")
            elif url.startswith("http://cards.test/cards/"):
                route.fulfill(body="Unavailable", status=503, content_type="text/plain")
            else:
                route.abort()

        page.route("**/*", respond)
        page.goto("http://cards.test/")
        return page

    def swap(self, page, deck_id=1):
        page.evaluate("""html => {
            const oldPanel = document.querySelector('#deck-card-panel');
            oldPanel.dispatchEvent(new CustomEvent('htmx:beforeSwap', {
                bubbles: true, detail: {target: oldPanel, shouldSwap: true, xhr: oldPanel.testXhr}
            }));
            const panel = new DOMParser().parseFromString(html, 'text/html').querySelector('#deck-card-panel');
            oldPanel.replaceWith(panel);
            panel.dispatchEvent(new CustomEvent('htmx:afterSwap', {bubbles: true, detail: {target: panel}}));
        }""", stack_fixture(deck_id))

    def test_forty_card_category_stays_bounded_and_last_card_is_reachable(self):
        for width, height in ((1440, 900), (1000, 900), (390, 844)):
            with self.subTest(width=width):
                page = self.page(width, height)
                page.locator('[data-deck-view="stacks"]').click()
                self.assertEqual(page.locator(".deck-stack-card").count(), 100)
                geometry = page.locator(".deck-stack-list").first.evaluate("""element => ({
                    height: element.clientHeight, content: element.scrollHeight,
                    tops: [...document.querySelectorAll('.deck-stack-column')].map(column => column.offsetTop),
                    pageWidth: document.documentElement.scrollWidth,
                    pageHeight: document.documentElement.scrollHeight
                })""")
                self.assertLessEqual(geometry["height"], min(700, height * .72) + 1)
                self.assertGreater(geometry["content"], geometry["height"])
                self.assertEqual(len(set(geometry["tops"])), 1)
                self.assertLessEqual(geometry["pageWidth"], width)
                self.assertLessEqual(geometry["pageHeight"], height + 100)
                cards = page.locator(".deck-stack-list").first.locator("button")
                cards.nth(38).focus()
                page.keyboard.press("Tab")
                self.assertTrue(cards.last.evaluate("element => element === document.activeElement"))
                self.assertTrue(cards.last.evaluate("""element => {
                    const card = element.getBoundingClientRect();
                    const viewport = element.parentElement.getBoundingClientRect();
                    return card.top >= viewport.top && card.bottom <= viewport.bottom;
                }"""))

    def test_keyboard_end_and_home_reach_whole_category(self):
        page = self.page(390, 844)
        stack = page.locator(".deck-stack-list").first
        stack.focus()
        page.keyboard.press("End")
        self.assertTrue(stack.locator("button").last.evaluate("element => element === document.activeElement"))
        self.assertGreater(stack.evaluate("element => element.scrollTop"), 1000)
        page.keyboard.press("Home")
        self.assertTrue(stack.locator("button").first.evaluate("element => element === document.activeElement"))
        self.assertLess(stack.evaluate("element => element.scrollTop"), 10)

    def test_table_keeps_all_rows_and_inactive_images_are_not_activated(self):
        page = self.page()
        self.assertEqual(page.locator(".deck-card-table tbody tr:visible").count(), 100)
        self.assertEqual(page.locator('.deck-stack-view img[src]').count(), 0)
        self.assertEqual(page.locator('.deck-table-view img[src]').count(), 100)
        page.locator('[data-deck-view="stacks"]').click()
        self.assertEqual(page.locator('.deck-stack-view img[src]').count(), 100)
        page.reload()
        self.assertEqual(page.locator('.deck-table-view img[src]').count(), 0)
        self.assertFalse(page.locator('.deck-stack-view').evaluate("element => element.hidden"))

    def test_per_deck_view_preferences_remain_separate(self):
        page = self.page()
        page.locator('[data-deck-view="stacks"]').click()
        self.swap(page, deck_id=2)
        self.assertFalse(page.locator('.deck-table-view').evaluate("element => element.hidden"))
        page.locator('[data-deck-view="table"]').click()
        self.swap(page, deck_id=1)
        self.assertFalse(page.locator('.deck-stack-view').evaluate("element => element.hidden"))

    def test_swap_preserves_focus_rails_and_stack_scroll_when_storage_throws(self):
        page = self.page(390, 844, storage_failure=True)
        card = page.locator(".deck-stack-list").last.locator("button").last
        card.focus()
        before = page.evaluate("""() => ({
            card: document.activeElement.dataset.cardDetailUrl,
            rail: document.querySelector('.deck-stack-board-columns').scrollLeft,
            stack: [...document.querySelectorAll('.deck-stack-list')].at(-1).scrollTop
        })""")
        self.assertGreater(before["rail"], 0)
        self.assertGreater(before["stack"], 0)
        self.swap(page)
        after = page.evaluate("""() => ({
            card: document.activeElement.dataset.cardDetailUrl,
            rail: document.querySelector('.deck-stack-board-columns').scrollLeft,
            stack: [...document.querySelectorAll('.deck-stack-list')].at(-1).scrollTop
        })""")
        self.assertEqual(before, after)

    def test_swap_preserves_category_expansion_and_edit_focus_when_storage_throws(self):
        page = self.page(storage_failure=True)
        page.locator("details summary").first.click()
        page.wait_for_function("!document.querySelector('details').open")
        field = page.locator('details').nth(1).locator('input[name="qty"]').first
        field.focus()
        identity = field.evaluate("element => element.form.action")
        self.swap(page)
        self.assertFalse(page.locator('details').first.evaluate("element => element.open"))
        self.assertTrue(page.locator('details').nth(1).evaluate("element => element.open"))
        self.assertEqual(page.evaluate("document.activeElement.form.action"), identity)
        self.assertEqual(page.evaluate("document.activeElement.name"), "qty")
        self.assertEqual(page.locator('details').first.locator('img[src]').count(), 0)

    def test_failed_card_detail_returns_focus_without_moving_stack(self):
        page = self.page(390, 844, storage_failure=True)
        stack = page.locator(".deck-stack-list").first
        stack.focus()
        page.keyboard.press("End")
        scroll = stack.evaluate("element => element.scrollTop")
        page.keyboard.press("Enter")
        page.locator('.card-detail-error').wait_for()
        page.locator('[data-cards-dialog-close]').click()
        self.assertTrue(stack.locator('button').last.evaluate("element => element === document.activeElement"))
        self.assertEqual(stack.evaluate("element => element.scrollTop"), scroll)

    def test_failed_mutation_keeps_existing_state_and_focus(self):
        page = self.page(storage_failure=True)
        button = page.locator('.deck-card-save').nth(25)
        button.focus()
        page.evaluate("""() => {
            const panel = document.querySelector('#deck-card-panel');
            const button = document.activeElement;
            const xhr = {};
            button.form.dispatchEvent(new CustomEvent('htmx:beforeRequest', {
                bubbles: true, detail: {elt: button.form, xhr}
            }));
            button.disabled = true;
            button.blur();
            panel.dispatchEvent(new CustomEvent('htmx:beforeSwap', {
                bubbles: true, detail: {target: panel, shouldSwap: false, xhr}
            }));
            button.disabled = false;
            panel.dispatchEvent(new CustomEvent('htmx:afterRequest', {
                bubbles: true, detail: {elt: button.form, successful: false, xhr}
            }));
        }""")
        self.assertTrue(button.evaluate("element => element === document.activeElement"))
        self.assertEqual(page.locator('.deck-card-table tbody tr').count(), 100)

    def test_disabled_save_button_regains_focus_after_swap(self):
        page = self.page(storage_failure=True)
        button = page.locator('.deck-card-save').nth(25)
        button.focus()
        label = button.get_attribute('aria-label')
        page.evaluate("""() => {
            const panel = document.querySelector('#deck-card-panel');
            const button = document.activeElement;
            panel.testXhr = {};
            button.form.dispatchEvent(new CustomEvent('htmx:beforeRequest', {
                bubbles: true, detail: {elt: button.form, xhr: panel.testXhr}
            }));
            button.disabled = true;
            button.blur();
        }""")
        self.assertEqual(page.evaluate("document.activeElement.tagName"), "BODY")
        self.swap(page)
        self.assertEqual(page.evaluate("document.activeElement.getAttribute('aria-label')"), label)


if __name__ == "__main__":
    unittest.main()