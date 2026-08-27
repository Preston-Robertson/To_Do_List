"""Offline storage and import tests for the isolated Trading Cards domain."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luigi_web import (
    cards,
    cards_importer,
    cards_pokemon,
    cards_scryfall,
    cards_templating,
)


def _mtg_card(
    external_id: str,
    name: str,
    *,
    price: str = "1.00",
    set_code: str = "TST",
    collector_number: str = "1",
) -> dict:
    return {
        "id": external_id,
        "name": name,
        "set": set_code,
        "set_name": "Synthetic Set",
        "collector_number": collector_number,
        "type_line": "Artifact",
        "mana_cost": "{2}",
        "prices": {"usd": price, "usd_foil": None, "eur": None},
        "image_uris": {
            "small": f"https://cards.scryfall.io/small/front/a/b/{external_id}.jpg",
            "normal": f"https://cards.scryfall.io/normal/front/a/b/{external_id}.jpg",
        },
    }


def _pokemon_card(external_id: str, name: str, number: str = "1") -> dict:
    return {
        "id": external_id,
        "name": name,
        "supertype": "Pokemon",
        "subtypes": ["Stage 1"],
        "types": ["Psychic"],
        "number": number,
        "rarity": "Rare",
        "set": {"id": "base1", "name": "Base Set"},
        "images": {
            "small": f"https://images.pokemontcg.io/base1/{number}.png",
            "large": f"https://images.pokemontcg.io/base1/{number}_hires.png",
        },
        "attacks": [{
            "name": "Example Attack",
            "cost": ["Psychic"],
            "damage": "30",
            "text": "Synthetic effect.",
        }],
        "tcgplayer": {
            "prices": {
                "normal": {"market": 1.23},
                "holofoil": {"market": 2.34},
            }
        },
        "cardmarket": {"prices": {"averageSellPrice": 0.98}},
    }


class CardRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "LUIGI_WEB_CARDS_DB": os.path.join(self.temp_dir.name, "cards.db"),
            "LUIGI_WEB_CARDS_BULK_DIR": os.path.join(self.temp_dir.name, "bulk"),
            "LUIGI_WEB_CARDS_REFRESH_HOURS": "0",
        })
        self.env.start()
        cards.init_db()

    def tearDown(self) -> None:
        cards_scryfall.stop_scheduler()
        self.env.stop()
        self.temp_dir.cleanup()

    def seed(self) -> tuple[dict, dict]:
        cards.upsert_scryfall_cards([
            _mtg_card("example-relic", "Example Relic", price="12.345", collector_number="7"),
            _mtg_card("example-wizard", "Example Wizard", price="2.00", collector_number="8"),
        ])
        return (
            cards.find_card("mtg", "Example Relic"),
            cards.find_card("mtg", "Example Wizard"),
        )

    def test_schema_is_app_owned_and_uses_integer_money(self) -> None:
        self.assertEqual(cards.to_minor("12.345"), 1235)
        self.assertEqual(cards.to_minor("1,234.56"), 123456)
        with cards._connect() as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            columns = {
                row[1]: row[2] for row in conn.execute("PRAGMA table_info(cards)")
            }
        self.assertIn("decks", tables)
        self.assertIn("collection", tables)
        self.assertEqual(columns["price_usd_minor"], "INTEGER")
        self.assertNotIn("tasks", tables)

    def test_scryfall_upsert_rejects_untrusted_images(self) -> None:
        payload = _mtg_card("unsafe-image", "Image Example")
        payload["image_uris"]["normal"] = "https://untrusted.example/card.jpg"
        payload["image_uris"]["art_crop"] = (
            "https://cards.scryfall.io/art_crop/example.jpg')"
        )
        cards.upsert_scryfall_cards([payload])
        row = cards.find_card("mtg", "Image Example")
        self.assertIsNotNone(row["image_small"])
        self.assertIsNone(row["image_normal"])
        self.assertIsNone(row["image_art_crop"])

    def test_deck_collection_and_export_lifecycle(self) -> None:
        relic, _ = self.seed()
        deck_id = cards.create_deck("mtg", "Example Deck", "commander")
        cards.add_card_to_deck(deck_id, relic["id"], qty=2, category="Engine")
        cards.add_card_to_deck(deck_id, relic["id"], qty=3, category="Engine")
        cards.add_to_collection(
            relic["id"], qty=3, condition="NM", acquired_date="2026-08-01",
            acquired_price="4.20",
        )

        deck_row = cards.list_decks("mtg")[0]
        self.assertEqual(deck_row["card_count"], 5)
        self.assertEqual(deck_row["price_usd_minor"], 6175)
        self.assertEqual(
            cards.collection_totals("mtg"),
            {"qty": 3, "value_usd_minor": 3705},
        )
        self.assertEqual(cards.list_collection("mtg")[0]["acquired_date"], "2026-08-01")
        export = cards.deck_export_text(deck_id)
        self.assertIn("5 Example Relic (TST) 7", export)
        cards.add_card_to_deck(deck_id, relic["id"], board="commander")
        self.assertTrue(cards.deck_export_text(deck_id).startswith("Commander\n"))

    def test_cross_game_card_cannot_enter_mtg_deck(self) -> None:
        deck_id = cards.create_deck("mtg", "Scoped Deck")
        pokemon_id = cards.create_manual_card("pokemon", {"name": "Example Creature"})
        with self.assertRaisesRegex(ValueError, "same game"):
            cards.add_card_to_deck(deck_id, pokemon_id)

    def test_manual_games_have_catalog_decks_and_collection(self) -> None:
        card_id = cards.create_manual_card("riftbound", {
            "name": "Example Champion",
            "set_code": "CORE",
            "type_line": "Unit",
        })
        deck_id = cards.create_deck("riftbound", "Example Rift Deck", "constructed")
        cards.add_card_to_deck(deck_id, card_id, qty=3)
        cards.add_to_collection(card_id, qty=1)
        self.assertEqual(cards.browse_catalog("riftbound")["total"], 1)
        self.assertEqual(cards.list_decks("riftbound")[0]["card_count"], 3)
        self.assertEqual(cards.collection_totals("riftbound")["qty"], 1)

    def test_pokemon_provider_mapping_uses_shared_catalog_contract(self) -> None:
        cards.upsert_pokemon_cards([_pokemon_card("base1-1", "Example Pokemon")])
        row = cards.find_card("pokemon", "Example Pokemon")
        self.assertEqual(row["set_code"], "base1")
        self.assertEqual(row["price_usd_minor"], 123)
        self.assertEqual(row["price_usd_foil_minor"], 234)
        self.assertEqual(row["price_eur_minor"], 98)
        self.assertIn("Example Attack", row["oracle_text"])
        self.assertTrue(row["image_small"].startswith("https://images.pokemontcg.io/"))
        self.assertEqual(cards.list_sets("pokemon")[0]["card_count"], 1)

    def test_pokemon_refresh_paginates_and_audits_offline(self) -> None:
        pages = {
            1: {
                "data": [
                    _pokemon_card("base1-1", "Example One", "1"),
                    _pokemon_card("base1-2", "Example Two", "2"),
                ],
                "totalCount": 3,
            },
            2: {
                "data": [_pokemon_card("base1-3", "Example Three", "3")],
                "totalCount": 3,
            },
        }
        progress: list[tuple[int, int | None]] = []
        with patch.object(cards_pokemon, "_fetch_page", side_effect=lambda page: pages[page]):
            result = cards_pokemon.refresh_pokemon(
                lambda current, total: progress.append((current, total))
            )
        self.assertIsNone(result["error"])
        self.assertEqual(result["cards_upserted"], 3)
        self.assertEqual(result["prices_snapshotted"], 3)
        self.assertEqual(progress, [(2, 3), (3, 3)])
        self.assertEqual(cards.last_refresh("pokemon")["status"], "ok")
        self.assertEqual(cards.catalog_stats("pokemon")["card_count"], 3)

    def test_pokemon_api_key_is_only_sent_in_provider_header(self) -> None:
        with patch.dict(os.environ, {"LUIGI_WEB_CARDS_POKEMON_API_KEY": "example-key"}):
            headers = cards_pokemon._headers()
        self.assertEqual(headers["X-Api-Key"], "example-key")

    def test_deck_import_formats_and_atomic_rollback(self) -> None:
        relic, wizard = self.seed()
        deck_id = cards.create_deck("mtg", "Import Deck")
        report = cards_importer.preview("mtg", """
            Commander
            1 Example Wizard (TST) 8 *CMDR*
            Mainboard
            4x Example Relic (TST) 7
            SB: 2 Missing Card
            invalid row
        """)
        self.assertEqual(report.matched_count, 5)
        self.assertEqual(report.unmatched_count, 2)
        self.assertEqual(len(report.unparsed), 1)
        self.assertEqual(cards_importer.apply(deck_id, report), 5)
        self.assertEqual(cards.get_deck(deck_id)["commander_card_id"], wizard["id"])

        before = sum(row["qty"] for row in cards.list_deck_cards(deck_id))
        invalid = cards_importer.ImportReport(matched=[
            cards_importer.ImportRow(
                cards_importer.ParsedLine("1 Example Relic", 1, "Example Relic"),
                card_id=relic["id"],
            ),
            cards_importer.ImportRow(
                cards_importer.ParsedLine("1 Missing", 1, "Missing"),
                card_id=999999,
            ),
        ])
        with self.assertRaisesRegex(ValueError, "not found"):
            cards_importer.apply(deck_id, invalid)
        after = sum(row["qty"] for row in cards.list_deck_cards(deck_id))
        self.assertEqual(after, before)

    def test_create_plus_import_can_share_one_transaction(self) -> None:
        invalid = cards_importer.ImportReport(matched=[
            cards_importer.ImportRow(
                cards_importer.ParsedLine("1 Missing", 1, "Missing"),
                card_id=999999,
            )
        ])
        with self.assertRaises(ValueError):
            with cards.transaction() as conn:
                deck_id = cards.create_deck("mtg", "Roll Back", conn=conn)
                cards_importer.apply(deck_id, invalid, conn=conn)
        self.assertEqual(cards.list_decks("mtg"), [])

    def test_notes_accept_drawio_and_reject_active_content(self) -> None:
        deck_id = cards.create_deck("mtg", "Diagram Deck")
        xml = "<mxfile><diagram><mxGraphModel><root/></mxGraphModel></diagram></mxfile>"
        self.assertEqual(cards.save_deck_notes(deck_id, "mtg", xml), len(xml))
        for unsafe in (
            "<!DOCTYPE x><mxfile/>",
            "<script/>",
            '<mxfile><object href="javascript:alert(1)"/></mxfile>',
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(ValueError):
                    cards.save_deck_notes(deck_id, "mtg", unsafe)

    def test_price_snapshots_skip_unchanged_values(self) -> None:
        self.seed()
        self.assertEqual(cards.snapshot_prices("mtg"), 2)
        self.assertEqual(cards.snapshot_prices("mtg"), 0)

    def test_price_history_and_deck_value_series(self) -> None:
        relic, _ = self.seed()
        deck_id = cards.create_deck("mtg", "History Deck")
        cards.add_card_to_deck(deck_id, relic["id"], qty=2)
        with cards._connect() as conn:
            conn.executemany(
                """
                INSERT INTO price_history(card_id, snapshot_date, price_usd_minor)
                VALUES (?, date('now', ?), ?)
                """,
                ((relic["id"], "-2 days", 100), (relic["id"], "-1 day", 200)),
            )
        history = cards.card_price_history(relic["id"], days=10)
        series = cards.deck_price_series(deck_id, days=10)
        self.assertEqual([row["price_usd_minor"] for row in history], [100, 200])
        self.assertEqual([row["value_usd_minor"] for row in series], [200, 400])

    def test_collection_rejects_invalid_dates_and_mixed_currency(self) -> None:
        relic, _ = self.seed()
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            cards.add_to_collection(relic["id"], acquired_date="08/01/2026")
        cards.add_to_collection(
            relic["id"], acquired_price="4.20", acquired_currency="USD"
        )
        with self.assertRaisesRegex(ValueError, "currency must match"):
            cards.add_to_collection(
                relic["id"], acquired_price="3.00", acquired_currency="EUR"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with cards._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO collection(card_id, qty, foil, condition, acquired_currency)
                    VALUES (?, 1, 0, 'LP', 'GBP')
                    """,
                    (relic["id"],),
                )

    def test_filters_escape_mana_and_format_minor_units(self) -> None:
        self.assertEqual(cards_templating.money_minor(1235), "$12.35")
        self.assertNotIn("<script", cards_templating.format_mana("{<script>}"))

    def test_scryfall_refresh_is_streamed_audited_and_cleans_up(self) -> None:
        payloads = [
            _mtg_card("refresh-card", "Refresh Card", price="3.21"),
            {**_mtg_card("refresh-token", "Refresh Token"), "layout": "token"},
        ]
        metadata = {
            "type": "default_cards",
            "download_uri": "https://data.scryfall.io/example.json",
            "size": 2,
        }

        def fake_download(url: str, destination: Path, progress=None) -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"[]")
            if progress:
                progress(2, 2)

        with (
            patch.object(cards_scryfall, "_bulk_meta", return_value=metadata),
            patch.object(cards_scryfall, "_download", side_effect=fake_download),
            patch.object(cards_scryfall, "_iter_bulk", return_value=iter(payloads)),
        ):
            result = cards_scryfall.refresh_mtg("default_cards")

        self.assertIsNone(result["error"])
        self.assertEqual(result["cards_upserted"], 1)
        self.assertEqual(cards.last_refresh("mtg")["status"], "ok")
        self.assertFalse(list(Path(self.temp_dir.name).rglob("*.json*")))

    def test_scryfall_download_host_is_allow_listed(self) -> None:
        with self.assertRaisesRegex(ValueError, "untrusted"):
            cards_scryfall._trusted_url(
                "https://untrusted.example/cards.json", {"data.scryfall.io"}
            )

    def test_refresh_lock_releases_when_audit_setup_fails(self) -> None:
        with (
            patch.object(cards, "refresh_start", side_effect=RuntimeError("unavailable")),
            self.assertLogs("luigi_web.cards.scryfall", level="ERROR"),
        ):
            result = cards_scryfall.refresh_mtg("default_cards")
        self.assertIn("unavailable", result["error"])
        self.assertTrue(cards_scryfall._refresh_lock.acquire(blocking=False))
        cards_scryfall._refresh_lock.release()


if __name__ == "__main__":
    unittest.main()