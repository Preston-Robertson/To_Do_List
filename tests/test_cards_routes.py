"""Authentication, CSRF, rendering, and route tests for Trading Cards."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from luigi_web import application, auth, cards, cards_pokemon


class CardRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "LUIGI_WEB_UI_TOKEN": "main-secret",
            "LUIGI_WEB_CARDS_DB": os.path.join(self.temp_dir.name, "cards.db"),
            "LUIGI_WEB_CARDS_REFRESH_HOURS": "0",
        })
        self.env.start()
        cards.init_db()
        cards.upsert_scryfall_cards([{
            "id": "route-card",
            "name": "Route Example",
            "set": "TST",
            "set_name": "Synthetic Set",
            "collector_number": "9",
            "type_line": "Artifact",
            "prices": {"usd": "2.50"},
        }])
        self.card = cards.find_card("mtg", "Route Example")
        self.client = TestClient(application.app)
        self.client.cookies.set(auth.COOKIE_NAME, "main-secret")

    def tearDown(self) -> None:
        self.client.close()
        self.env.stop()
        self.temp_dir.cleanup()

    def csrf_headers(self) -> dict[str, str]:
        self.client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        return {"X-CSRF-Token": "csrf-value", "HX-Request": "true"}

    def test_routes_require_main_authentication(self) -> None:
        anonymous = TestClient(application.app)
        response = anonymous.get(
            "/cards/mtg/decks",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        anonymous.close()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_card_pages_render_and_are_no_store(self) -> None:
        for path in (
            "/cards/mtg/catalog",
            "/cards/mtg/decks",
            "/cards/mtg/collection",
            "/cards/mtg/data",
            "/cards/pokemon/catalog",
            "/cards/riftbound/decks",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertIn("Trading Cards", response.text)

    def test_rich_card_detail_route_and_stack_markup(self) -> None:
        deck_id = cards.create_deck("mtg", "Visual Deck")
        cards.add_card_to_deck(deck_id, self.card["id"], qty=2, category="Engine")
        detail = self.client.get(
            f"/cards/mtg/cards/{self.card['id']}/detail"
        )
        deck = self.client.get(f"/cards/mtg/decks/{deck_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Card info", detail.text)
        self.assertIn("Art & printings", detail.text)
        self.assertEqual(detail.headers["Cache-Control"], "no-store")
        self.assertIn('data-deck-view="stacks"', deck.text)
        self.assertIn("deck-stack-card", deck.text)

    def test_long_category_is_balanced_into_visual_stack_lanes(self) -> None:
        cards.upsert_scryfall_cards([
            {
                "id": f"stack-lane-{index}",
                "name": f"Stack Lane Card {index}",
                "set": "TST",
                "set_name": "Synthetic Set",
                "collector_number": str(index),
                "type_line": "Land",
                "prices": {"usd": "1.00"},
            }
            for index in range(1, 10)
        ])
        deck_id = cards.create_deck("mtg", "Balanced Stack Deck")
        for index in range(1, 10):
            card = cards.find_card("mtg", f"Stack Lane Card {index}")
            cards.add_card_to_deck(deck_id, card["id"], category="Lands")

        response = self.client.get(f"/cards/mtg/decks/{deck_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count('class="deck-stack-column"'), 2)
        self.assertIn("Stack 1 of 2 · Qty 8", response.text)
        self.assertIn("Stack 2 of 2 · Qty 1", response.text)

    def test_inspector_can_swap_deck_and_collection_printings(self) -> None:
        alternate_payload = {
            "id": "route-card-alt", "oracle_id": "route-oracle",
            "name": "Route Example", "set": "ALT",
            "set_name": "Alternate Set", "collector_number": "10",
            "type_line": "Artifact", "prices": {"usd": "3.00"},
        }
        original_payload = {
            "id": "route-card", "oracle_id": "route-oracle",
            "name": "Route Example", "set": "TST",
            "set_name": "Synthetic Set", "collector_number": "9",
            "type_line": "Artifact", "prices": {"usd": "2.50"},
        }
        cards.upsert_scryfall_cards([original_payload, alternate_payload])
        self.card = cards.find_card("mtg", "Route Example", set_code="TST")
        alternate = cards.find_card("mtg", "Route Example", set_code="ALT")
        deck_id = cards.create_deck("mtg", "Printing Route Deck")
        cards.add_card_to_deck(deck_id, self.card["id"], qty=2)
        slot = cards.list_deck_cards(deck_id)[0]
        cards.add_to_collection(self.card["id"], qty=1)
        collection = cards.list_collection("mtg")[0]
        headers = self.csrf_headers()

        detail = self.client.get(
            f"/cards/mtg/cards/{alternate['id']}/detail",
            params={"deck_id": deck_id, "deck_card_id": slot["id"]},
        )
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Use this printing", detail.text)
        self.assertIn(f"deck_id={deck_id}", detail.text)
        self.assertIn("card-printing-grid", detail.text)

        deck_swap = self.client.post(
            f"/cards/mtg/decks/{deck_id}/cards/{slot['id']}/printing",
            data={"target_card_id": alternate["id"]},
            headers=headers,
        )
        self.assertEqual(deck_swap.status_code, 204)
        self.assertEqual(cards.list_deck_cards(deck_id)[0]["card_id"], alternate["id"])

        collection_swap = self.client.post(
            f"/cards/mtg/collection/{collection['id']}/printing",
            data={"target_card_id": alternate["id"]},
            headers=headers,
        )
        self.assertEqual(collection_swap.status_code, 204)
        self.assertEqual(cards.list_collection("mtg")[0]["card_id"], alternate["id"])

    def test_advanced_catalog_route_preserves_repeated_filters(self) -> None:
        response = self.client.get(
            "/cards/mtg/catalog",
            params=[
                ("q", "Route"), ("colors", "R"), ("colors", "U"),
                ("rarities", "rare"), ("order", "usd"),
                ("direction", "desc"),
            ],
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Advanced filters", response.text)
        self.assertIn('name="colors" value="R" checked', response.text)
        self.assertIn('name="colors" value="U" checked', response.text)
        self.assertIn('name="order"', response.text)
        self.assertIn('name="criteria" value="borderless"', response.text)
        self.assertIn('name="include_extras" value="1"', response.text)

    def test_mutations_require_csrf(self) -> None:
        rejected = self.client.post(
            "/cards/mtg/decks", data={"name": "Rejected Deck"}
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(cards.list_decks("mtg"), [])

    def test_non_htmx_mutation_redirects_to_known_local_page(self) -> None:
        self.client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        response = self.client.post(
            "/cards/mtg/collection",
            data={"card_id": self.card["id"], "qty": 1, "condition": "NM"},
            headers={
                "X-CSRF-Token": "csrf-value",
                "Referer": "https://untrusted.example/redirect",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/cards/mtg/collection")

    def test_create_add_import_export_and_collection_routes(self) -> None:
        headers = self.csrf_headers()
        created = self.client.post(
            "/cards/mtg/decks",
            data={"name": "Route Deck", "format_": "commander"},
            headers=headers,
        )
        self.assertEqual(created.status_code, 204)
        location = created.headers["HX-Redirect"]
        deck_id = int(location.rsplit("/", 1)[-1])

        added = self.client.post(
            f"/cards/mtg/decks/{deck_id}/cards",
            data={"card_id": self.card["id"], "qty": 2, "board": "main"},
            headers=headers,
        )
        self.assertEqual(added.status_code, 200)
        self.assertIn("Route Example", added.text)

        imported = self.client.post(
            f"/cards/mtg/decks/{deck_id}/import",
            data={"text": "1 Route Example (TST) 9"},
            headers=headers,
        )
        self.assertEqual(imported.status_code, 204)
        self.assertIn("imported=1", imported.headers["HX-Redirect"])

        exported = self.client.get(f"/cards/mtg/decks/{deck_id}/export.txt")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("3 Route Example (TST) 9", exported.text)
        self.assertEqual(exported.headers["Cache-Control"], "no-store")

        collected = self.client.post(
            "/cards/mtg/collection",
            data={
                "card_id": self.card["id"], "qty": 2, "condition": "NM",
                "acquired_date": "2026-08-02", "acquired_price": "1.25",
                "acquired_currency": "USD",
            },
            headers=headers,
        )
        self.assertEqual(collected.status_code, 204)
        self.assertEqual(cards.collection_totals("mtg")["qty"], 2)
        self.assertEqual(cards.list_collection("mtg")[0]["acquired_date"], "2026-08-02")

    def test_svg_charts_keep_private_cache_headers(self) -> None:
        deck_id = cards.create_deck("mtg", "Chart Deck")
        cards.add_card_to_deck(deck_id, self.card["id"])
        cards.snapshot_prices("mtg")
        card_chart = self.client.get(
            f"/cards/mtg/cards/{self.card['id']}/sparkline.svg"
        )
        deck_chart = self.client.get(
            f"/cards/mtg/decks/{deck_id}/value-trend.svg"
        )
        self.assertEqual(card_chart.headers["Cache-Control"], "private, max-age=1800")
        self.assertEqual(deck_chart.headers["Cache-Control"], "private, max-age=900")

    def test_object_ids_are_scoped_to_game_and_deck(self) -> None:
        headers = self.csrf_headers()
        deck_id = cards.create_deck("mtg", "Scoped Route Deck")
        pokemon_card = cards.create_manual_card("pokemon", {"name": "Other Game Card"})
        response = self.client.post(
            f"/cards/mtg/decks/{deck_id}/cards",
            data={"card_id": pokemon_card, "qty": 1},
            headers=headers,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(cards.list_deck_cards(deck_id), [])

    def test_import_preview_and_search_are_authenticated(self) -> None:
        headers = self.csrf_headers()
        preview = self.client.post(
            "/cards/mtg/import/preview",
            json={"text": "1 Route Example"},
            headers=headers,
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["counts"]["matched"], 1)
        search = self.client.get("/cards/mtg/search?q=Route")
        self.assertEqual(search.status_code, 200)
        self.assertIn("Route Example", search.text)

    def test_import_preview_and_viewer_split_board_categories(self) -> None:
        headers = self.csrf_headers()
        preview = self.client.post(
            "/cards/mtg/import/preview",
            json={
                "text": (
                    "Mainboard\n[Engine]\n2 Route Example\n"
                    "Sideboard\n[Answers]\n1 Route Example"
                )
            },
            headers=headers,
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(
            [
                (row["board"], row["category"], row["qty"])
                for row in preview.json()["matched"]
            ],
            [("main", "Engine", 2), ("side", "Answers", 1)],
        )

        deck_id = cards.create_deck("mtg", "Section Route Deck")
        cards.add_card_to_deck(
            deck_id, self.card["id"], qty=2, board="main", category="Engine"
        )
        cards.add_card_to_deck(
            deck_id, self.card["id"], qty=1, board="side", category="Answers"
        )
        response = self.client.get(f"/cards/mtg/decks/{deck_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count('class="deck-board-section"'), 2)
        self.assertEqual(response.text.count('class="deck-stack-board"'), 2)
        for label in ("Mainboard", "Sideboard", "Engine", "Answers"):
            self.assertIn(label, response.text)

    def test_notes_route_rejects_active_xml(self) -> None:
        headers = self.csrf_headers()
        deck_id = cards.create_deck("mtg", "Notes Route Deck")
        response = self.client.post(
            f"/cards/mtg/decks/{deck_id}/notes",
            json={"xml": '<mxfile><object href="javascript:alert(1)"/></mxfile>'},
            headers=headers,
        )
        self.assertEqual(response.status_code, 422)
        self.assertIsNone(cards.get_deck(deck_id)["notes_xml"])

    def test_manual_card_route_completes_non_mtg_catalog(self) -> None:
        response = self.client.post(
            "/cards/riftbound/catalog/manual",
            data={"name": "Manual Route Champion", "set_code": "CORE"},
            headers=self.csrf_headers(),
        )
        self.assertEqual(response.status_code, 204)
        self.assertIn("Manual%20Route%20Champion", response.headers["HX-Redirect"])
        self.assertEqual(cards.browse_catalog("riftbound")["total"], 1)

    def test_pokemon_data_status_and_refresh_route(self) -> None:
        headers = self.csrf_headers()
        status = self.client.get("/cards/pokemon/data/status")
        self.assertEqual(status.status_code, 200)
        self.assertIn("pokemontcg.io", status.text)
        with patch.object(cards_pokemon, "start_refresh", return_value=True) as start:
            response = self.client.post(
                "/cards/pokemon/data/pokemon/refresh",
                headers=headers,
            )
        self.assertEqual(response.status_code, 204)
        start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()