from __future__ import annotations

import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luigi_web.modules.cards import repository as cards
from luigi_web.modules.cards import collection, purchases


class CollectionLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"LUIGI_WEB_CARDS_DB": str(Path(self.temp.name) / "cards.sqlite")})
        self.env.start()
        self.addCleanup(self.env.stop)
        cards.init_db()
        with cards.transaction() as conn:
            card_id = conn.execute(
                "INSERT INTO cards(game_code,external_id,source,name) VALUES ('mtg','synthetic-a','manual','Example card')"
            ).lastrowid
            assert card_id is not None
            self.card_id = card_id

    def test_v3_migration_is_once_and_keeps_unknown_cost(self):
        with cards.transaction() as conn:
            conn.execute("PRAGMA user_version=3")
            conn.execute("""
                INSERT INTO collection(card_id,qty,acquired_qty,acquired_price_minor)
                VALUES (?,3,2,101)
            """, (self.card_id,))
            conn.execute("INSERT INTO collection(card_id,qty,foil) VALUES (?,2,1)", (self.card_id,))
        cards.init_db()
        cards.init_db()
        with cards._connect() as conn:
            lots = conn.execute("SELECT * FROM collection_lots ORDER BY id").fetchall()
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT SUM(qty) FROM collection").fetchone()[0], 5)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(len(lots), 2)
        self.assertEqual([lot["price_source"] for lot in lots], ["legacy_summary"] * 2)
        self.assertEqual((lots[0]["qty"], lots[0]["priced_qty"], lots[0]["unit_price_minor"]), (3, 2, 101))
        self.assertIsNone(lots[1]["unit_price_minor"])
        self.assertEqual(lots[1]["priced_qty"], 0)

    def holding(self):
        return cards.list_collection("mtg")[0]

    def test_v4_without_ledger_migrates_partial_baseline_once(self):
        with cards.transaction() as conn:
            conn.execute("DROP TABLE collection_lots")
            conn.execute("INSERT INTO collection(card_id,qty,acquired_qty,acquired_price_minor) VALUES (?,5,2,101)", (self.card_id,))
        cards.init_db()
        cards.init_db()
        holding = self.holding()
        lots = purchases.lots("mtg", holding["id"])
        self.assertEqual(len(lots), 1)
        self.assertEqual((lots[0]["price_source"], lots[0]["priced_qty"]), ("legacy_summary", 2))
        cards.update_collection_acquisition(holding["id"], "mtg", lot_id=lots[0]["id"], acquired_price="2.00")
        totals = collection.page("mtg", {})["totals"]
        self.assertEqual((totals["legacy_cost_minor"], totals["unknown_cost_qty"]), (400, 3))
        self.assertIsNone(totals["actual_cost_minor"])

    def snapshot(self, day="2026-01-02", usd=102, foil: int | None = 204, eur=99):
        with cards.transaction() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO price_history VALUES (?,?,?,?,?)
            """, (self.card_id, day, usd, foil, eur))

    def csv_bytes(self, rows):
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=collection.CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue().encode()

    def test_repeated_purchases_keep_dates_and_exact_spend(self):
        cards.add_to_collection(self.card_id, qty=2, acquired_price="1.01", acquired_date="2026-01-01")
        cards.add_to_collection(self.card_id, acquired_price="1.02", acquired_date="2026-02-01")
        row = self.holding()
        self.assertEqual(row["acquired_price_minor"], 101)
        self.assertEqual(row["purchase_cost_minor"], 304)
        self.assertEqual(cards.collection_totals("mtg")["cost_basis_usd_minor"], 304)
        history = purchases.lots("mtg", row["id"])
        self.assertEqual([lot["acquired_date"] for lot in history], ["2026-02-01", "2026-01-01"])
        self.assertEqual(collection.page("mtg", {})["totals"]["actual_cost_minor"], 304)
        cards.add_to_collection(self.card_id, qty=3)
        totals = collection.page("mtg", {})["totals"]
        self.assertEqual(totals["unknown_cost_qty"], 3)
        self.assertEqual(totals["actual_cost_minor"], 304)

    def test_unknown_is_not_zero_and_explicit_zero_is_known(self):
        cards.add_to_collection(self.card_id)
        totals = collection.page("mtg", {})["totals"]
        self.assertIsNone(totals["actual_cost_minor"])
        self.assertIsNone(totals["market_minor"])
        self.assertEqual(totals["unpriced_qty"], 1)
        cards.add_to_collection(self.card_id, acquired_price="0")
        self.assertEqual(collection.page("mtg", {})["totals"]["actual_cost_minor"], 0)

    def test_compatibility_valuation_excludes_estimates_and_missing_foil_price(self):
        self.snapshot()
        with cards.transaction() as conn:
            conn.execute("UPDATE cards SET price_usd_minor=250 WHERE id=?", (self.card_id,))
        cards.add_to_collection(self.card_id, qty=2, acquired_price="1.01")
        cards.add_to_collection(self.card_id, acquired_price="1.02")
        cards.add_to_collection(self.card_id, acquired_date="2026-01-02", price_source="market_estimate", estimate_confirmed=True)
        row = self.holding()
        self.assertEqual(row["purchase_cost_minor"], 406)
        self.assertEqual(row["gain_loss_usd_minor"], 446)
        detail = cards.card_detail(self.card_id, "mtg")["collection"][0]
        self.assertEqual((detail["purchase_cost_minor"], detail["gain_loss_usd_minor"]), (406, 446))
        totals = cards.collection_totals("mtg")
        self.assertEqual((totals["cost_basis_usd_minor"], totals["comparison_qty"]), (304, 3))
        cards.add_to_collection(self.card_id, foil=True, acquired_price="1.00")
        foil = next(row for row in cards.list_collection("mtg") if row["foil"])
        self.assertIsNone(foil["current_value_usd_minor"])
        self.assertIsNone(foil["gain_loss_usd_minor"])
        self.assertEqual(cards.collection_totals("mtg")["comparison_qty"], 3)

    def test_estimate_exact_day_variant_currency_and_catalog_date(self):
        self.snapshot(foil=None)
        with cards.transaction() as conn:
            conn.execute("UPDATE cards SET price_usd_minor=999, price_usd_foil_minor=888, price_updated_at='2026-02-01T10:00:00Z' WHERE id=?", (self.card_id,))
            self.assertEqual(purchases.estimate(conn, self.card_id, "2026-01-02")["unit_price_minor"], 102)
            self.assertFalse(purchases.estimate(conn, self.card_id, "2026-01-03")["available"])
            self.assertFalse(purchases.estimate(conn, self.card_id, "2026-01-02", foil=True)["available"])
            self.assertFalse(purchases.estimate(conn, self.card_id, "2026-01-02", "EUR", True)["available"])
            self.assertEqual(purchases.estimate(conn, self.card_id, "2026-01-02", "EUR")["unit_price_minor"], 99)
            self.assertEqual(purchases.estimate(conn, self.card_id, "2026-02-01")["unit_price_minor"], 999)

    def test_estimates_require_confirmation_and_survive_price_refresh(self):
        self.snapshot()
        with self.assertRaises(ValueError):
            cards.add_to_collection(self.card_id, acquired_date="2026-01-02", price_source="market_estimate")
        cards.add_to_collection(self.card_id, acquired_date="2026-01-02", price_source="market_estimate", estimate_confirmed=True)
        lot = purchases.lots("mtg", self.holding()["id"])[0]
        self.snapshot(usd=800)
        self.assertEqual(purchases.lots("mtg", self.holding()["id"])[0], lot)
        self.assertEqual((lot["price_source"], lot["unit_price_minor"], lot["snapshot_date"]), ("market_estimate", 102, "2026-01-02"))
        self.assertTrue(lot["recorded_at"])
        self.assertIsNone(collection.page("mtg", {})["totals"]["actual_cost_minor"])

    def test_lot_correction_targets_one_and_rejects_aggregate_rewrite(self):
        cards.add_to_collection(self.card_id, acquired_price="1.01")
        cards.add_to_collection(self.card_id, acquired_price="2.02")
        row = self.holding()
        lots = purchases.lots("mtg", row["id"])
        with self.assertRaisesRegex(ValueError, "lot ID"):
            cards.update_collection_acquisition(row["id"], "mtg", acquired_price="9.00")
        self.assertTrue(cards.update_collection_acquisition(row["id"], "mtg", lot_id=lots[0]["id"], acquired_price="3.03"))
        updated = purchases.lots("mtg", row["id"])
        self.assertEqual(updated[1], lots[1])
        self.assertEqual(updated[0]["unit_price_minor"], 303)
        self.assertFalse(cards.update_collection_acquisition(row["id"], "pokemon", lot_id=lots[0]["id"]))

    def test_mixed_currency_add_and_correction_rollback(self):
        cards.add_to_collection(self.card_id, acquired_price="1.01")
        before = purchases.lots("mtg", self.holding()["id"])
        with self.assertRaisesRegex(ValueError, "currency must match"):
            cards.add_to_collection(self.card_id, acquired_price="2.02", acquired_currency="EUR", notes="Rejected note")
        self.assertEqual(purchases.lots("mtg", self.holding()["id"]), before)
        self.assertIsNone(self.holding()["notes"])
        cards.add_to_collection(self.card_id, acquired_price="2.02")
        with self.assertRaises(ValueError):
            cards.update_collection_acquisition(self.holding()["id"], "mtg", lot_id=before[0]["id"], acquired_price="1.00", acquired_currency="EUR")
        self.assertEqual(collection.page("mtg", {})["totals"]["actual_cost_minor"], 303)

    def test_swap_merge_preserves_original_printing_estimate_and_dates(self):
        self.snapshot()
        cards.add_to_collection(self.card_id, acquired_date="2026-01-02", price_source="market_estimate", estimate_confirmed=True)
        original = self.holding()
        with cards.transaction() as conn:
            target = conn.execute("INSERT INTO cards(game_code,external_id,source,name) VALUES ('mtg','synthetic-b','manual','Example card')").lastrowid
            assert target is not None
        cards.add_to_collection(target, qty=2, acquired_price="1.01", acquired_date="2026-02-02")
        before = purchases.lots("mtg", original["id"])[0]
        result = cards.swap_collection_printing(original["id"], target, "mtg")
        lots = purchases.lots("mtg", result["collection_id"])
        self.assertEqual(len(lots), 2)
        moved = next(lot for lot in lots if lot["id"] == before["id"])
        for key in ("original_card_id", "unit_price_minor", "snapshot_date", "snapshot_source", "recorded_at", "acquired_date"):
            self.assertEqual(moved[key], before[key])
        self.assertEqual(moved["holding_card_id"], target)
        self.assertEqual(collection.page("mtg", {})["rows"][0]["exact_cost_minor"], 304)

    def test_partial_removal_fifo_then_delete_retains_audit_and_valid_fks(self):
        cards.add_to_collection(self.card_id, qty=2, acquired_price="1.01")
        cards.add_to_collection(self.card_id, acquired_price="2.02")
        row = self.holding()
        cards.remove_from_collection(row["id"], 1, game_code="mtg")
        self.assertEqual(collection.page("mtg", {})["totals"]["actual_cost_minor"], 303)
        cards.remove_from_collection(row["id"], game_code="mtg")
        self.assertEqual(cards.list_collection("mtg"), [])
        history = purchases.lots("mtg", deleted=True)
        self.assertEqual(sum(lot["qty"] for lot in history), 3)
        self.assertTrue(all(lot["collection_id"] is None and lot["remaining_qty"] == 0 for lot in history))
        with cards._connect() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_csv_atomic_preview_apply_and_repeat_token(self):
        raw = self.csv_bytes([dict(external_id="synthetic-a", qty=2, unit_price="1.01"), dict(external_id="synthetic-a", qty=1, unit_price="1.02")])
        preview = collection.preview_csv("mtg", raw, "add")
        self.assertEqual(cards.list_collection("mtg"), [])
        result = collection.apply_csv("mtg", preview["token"])
        self.assertEqual(result, dict(added=2, already_applied=False))
        self.assertTrue(collection.apply_csv("mtg", preview["token"])["already_applied"])
        self.assertEqual(collection.page("mtg", {})["totals"]["actual_cost_minor"], 304)

    def test_csv_duplicate_modes_and_invalid_row_are_atomic(self):
        row = dict(external_id="synthetic-a", qty=1, unit_price="1.00")
        raw = self.csv_bytes([row, row])
        with self.assertRaises(ValueError):
            collection.preview_csv("mtg", raw, "reject")
        self.assertEqual(cards.list_collection("mtg"), [])
        preview = collection.preview_csv("mtg", raw, "skip")
        self.assertEqual((preview["added"], preview["skipped"]), (1, 1))
        with self.assertRaises(ValueError):
            collection.preview_csv("mtg", self.csv_bytes([row, dict(external_id="missing")]), "add")
        self.assertEqual(cards.list_collection("mtg"), [])

    def test_csv_mixed_currency_rollback_and_apply_revalidates(self):
        with self.assertRaises(ValueError):
            collection.preview_csv("mtg", self.csv_bytes([
                dict(external_id="synthetic-a", unit_price="1.00", currency="USD"),
                dict(external_id="synthetic-a", unit_price="1.00", currency="EUR"),
            ]), "add")
        self.assertEqual(cards.list_collection("mtg"), [])
        preview = collection.preview_csv("mtg", self.csv_bytes([dict(external_id="synthetic-a", unit_price="1.00", currency="EUR")]), "add")
        cards.add_to_collection(self.card_id, acquired_price="2.00")
        with self.assertRaises(ValueError):
            collection.apply_csv("mtg", preview["token"])
        self.assertEqual(self.holding()["qty"], 1)

    def test_csv_limits_and_exact_ambiguous_printing_match(self):
        with self.assertRaisesRegex(ValueError, "500000"):
            collection.preview_csv("mtg", b"a" * 500001, "add")
        with self.assertRaisesRegex(ValueError, "2000"):
            collection.preview_csv("mtg", b"external_id\n" + b"synthetic-a\n" * 2001, "add")
        with cards.transaction() as conn:
            conn.execute("UPDATE cards SET set_code='TST', collector_number='1' WHERE id=?", (self.card_id,))
            conn.execute("INSERT INTO cards(game_code,external_id,source,name,set_code,collector_number) VALUES ('mtg','synthetic-b','manual','Example card','TST','1')")
        with self.assertRaises(ValueError):
            collection.preview_csv("mtg", b"set_code,collector_number\nTST,1\n", "add")
        with self.assertRaises(ValueError):
            collection.preview_csv("mtg", b"name\nExample card\n", "add")
        self.assertEqual(collection.preview_csv("mtg", b"external_id\nsynthetic-a\n", "add")["added"], 1)

    def test_csv_formula_escaping_and_own_roundtrip(self):
        notes = "=Example formula\nSecond line, quoted"
        with cards.transaction() as conn:
            conn.execute("UPDATE cards SET name='@Example card' WHERE id=?", (self.card_id,))
        cards.add_to_collection(self.card_id, acquired_price="1.01", notes=notes)
        raw = "".join(collection.export_csv("mtg", {}))
        parsed = list(csv.DictReader(io.StringIO(raw)))
        self.assertEqual(parsed[0]["name"], "'@Example card")
        self.assertEqual(parsed[0]["notes"], "'" + notes)
        preview = collection.preview_csv("mtg", raw.encode(), "add")
        self.assertEqual(preview["rows"][0]["notes"], notes)

    def test_csv_estimate_roundtrip_uses_retained_snapshot_not_changed_price(self):
        self.snapshot()
        cards.add_to_collection(self.card_id, acquired_date="2026-01-02", price_source="market_estimate", estimate_confirmed=True)
        raw = "".join(collection.export_csv("mtg", {})).encode()
        self.snapshot(usd=900)
        preview = collection.preview_csv("mtg", raw, "add")
        with self.assertRaisesRegex(ValueError, "confirm"):
            collection.apply_csv("mtg", preview["token"])
        collection.apply_csv("mtg", preview["token"], confirm_estimates=True)
        self.assertEqual(collection.page("mtg", {})["totals"]["estimate_cost_minor"], 204)
        invented = self.csv_bytes([dict(external_id="synthetic-a", acquired_date="2020-01-01", unit_price="1.00", price_source="market_estimate", snapshot_date="2020-01-01", snapshot_source="invented")])
        with self.assertRaises(ValueError):
            collection.preview_csv("mtg", invented, "add")

    def test_partial_legacy_csv_keeps_unknown_remainder_and_currency_scope(self):
        raw = self.csv_bytes([dict(external_id="synthetic-a", qty=5, priced_qty=2,
                                  unit_price="1.01", price_source="legacy_summary")])
        collection.apply_csv("mtg", collection.preview_csv("mtg", raw, "add")["token"])
        totals = collection.page("mtg", {})["totals"]
        self.assertEqual((totals["legacy_cost_minor"], totals["unknown_cost_qty"]), (202, 3))
        self.assertIsNone(totals["actual_cost_minor"])
        exported = list(csv.DictReader(io.StringIO("".join(collection.export_csv("mtg", {})))))
        self.assertEqual((exported[0]["qty"], exported[0]["priced_qty"]), ("5", "2"))
        unknown = self.csv_bytes([dict(external_id="synthetic-a", qty=2, priced_qty=0,
                                      unit_price="9.00", currency="EUR", price_source="legacy_summary")])
        collection.apply_csv("mtg", collection.preview_csv("mtg", unknown, "add")["token"])
        totals = collection.page("mtg", {})["totals"]
        self.assertEqual((totals["legacy_cost_minor"], totals["unknown_cost_qty"]), (202, 5))
        self.assertIsNone(collection.page("mtg", {"currency": "EUR"})["totals"]["legacy_cost_minor"])

    def test_zero_priced_legacy_is_unknown_not_free(self):
        raw = self.csv_bytes([dict(external_id="synthetic-a", qty=2, priced_qty=0,
                                  unit_price="9.00", price_source="legacy_summary")])
        collection.apply_csv("mtg", collection.preview_csv("mtg", raw, "add")["token"])
        self.assertIsNone(collection.page("mtg", {})["totals"]["legacy_cost_minor"])
        self.assertIsNone(self.holding()["purchase_cost_minor"])

    def test_quantity_limit_and_foreign_lot_correction_are_atomic(self):
        cards.add_to_collection(self.card_id, qty=9999, acquired_price="1.00")
        holding = self.holding()
        before = purchases.lots("mtg", holding["id"])
        with self.assertRaises(ValueError):
            cards.add_to_collection(self.card_id, acquired_price="2.00", notes="Rejected synthetic note")
        self.assertEqual(purchases.lots("mtg", holding["id"]), before)
        self.assertIsNone(self.holding()["notes"])
        cards.add_to_collection(self.card_id, foil=True)
        other = next(row for row in cards.list_collection("mtg") if row["foil"])
        other_lot = purchases.lots("mtg", other["id"])[0]
        self.assertFalse(cards.update_collection_acquisition(holding["id"], "mtg", lot_id=other_lot["id"], acquired_price="3.00"))
        self.assertEqual(purchases.lots("mtg", holding["id"]), before)

    def test_import_token_is_game_scoped_and_expires_without_writes(self):
        preview = collection.preview_csv("mtg", b"external_id\nsynthetic-a\n", "add")
        with self.assertRaises(ValueError):
            collection.apply_csv("pokemon", preview["token"])
        with cards.transaction() as conn:
            conn.execute("UPDATE collection_imports SET created_at=datetime('now','-2 hours') WHERE token=?", (preview["token"],))
        with self.assertRaises(ValueError):
            collection.apply_csv("mtg", preview["token"])
        self.assertEqual(cards.list_collection("mtg"), [])

    def test_pagination_filters_and_export_exceed_500_without_truncation(self):
        with cards.transaction() as conn:
            for index in range(551):
                card_id = conn.execute("""
                    INSERT INTO cards(game_code,external_id,source,name,set_code,collector_number,rarity,price_usd_minor)
                    VALUES ('mtg',?,'manual','Same synthetic name',?,?,'rare',200)
                """, (f"page-{index}", "AAA" if index < 501 else "BBB", str(index))).lastrowid
                assert card_id is not None
                purchases.record(conn, card_id, acquired_price="1.01")
        first = collection.page("mtg", {})
        second = collection.page("mtg", {"page": 2})
        self.assertEqual(len(first["rows"]), 50)
        self.assertEqual(first["totals"]["holdings"], 551)
        self.assertFalse({row["id"] for row in first["rows"]} & {row["id"] for row in second["rows"]})
        self.assertEqual(len(collection.page("mtg", {"page_size": 500})["rows"]), 200)
        selected = {"q": "synthetic", "set_code": "AAA", "rarity": "rare", "foil": "0", "condition": "NM", "in_decks": "no", "ownership": "covered", "min_price": "2.00", "max_price": "2.00"}
        result = collection.page("mtg", selected)
        self.assertEqual((result["totals"]["holdings"], result["all_totals"]["holdings"]), (501, 551))
        self.assertEqual(len(list(csv.DictReader(io.StringIO("".join(collection.export_csv("mtg", selected)))))), 501)
        self.assertEqual(collection.page("mtg", {"q": "%"})["totals"]["holdings"], 0)
        with patch.object(collection, "MAX_EXPORT_ROWS", 500):
            with self.assertRaises(ValueError):
                next(collection.export_csv("mtg", {}))


if __name__ == "__main__":
    unittest.main()