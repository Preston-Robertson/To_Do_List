"""Synthetic query-shape checks, deliberately without timing thresholds."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.benchmark_cards import synthetic_database, trace_operation


ROOT = Path(__file__).resolve().parents[1]


class CardsPerformanceTests(unittest.TestCase):
    def test_dataset_is_temporary_isolated_batched_and_network_blocked(self):
        with patch.dict(os.environ, {"LUIGI_WEB_CARDS_DB": "must-not-be-opened.db", "SYNTHETIC_SENTINEL": "hidden"}):
            with synthetic_database(1000, 250) as (cards, database):
                self.assertNotIn("SYNTHETIC_SENTINEL", os.environ)
                self.assertEqual(cards.db_path(), database.resolve())
                self.assertEqual(Path(os.environ["SQLITE_TMPDIR"]), database.parent)
                with cards._connect() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM cards").fetchone()[0], 1000)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM collection").fetchone()[0], 250)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM collection_lots").fetchone()[0], 250)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM deck_cards").fetchone()[0], 120)
                    self.assertEqual([row[0] for row in connection.execute(
                        "SELECT COUNT(*) FROM deck_cards GROUP BY category")], [40, 40, 40])
                with self.assertRaisesRegex(RuntimeError, "network blocked"):
                    socket.create_connection(("example.invalid", 443))
                with self.assertRaisesRegex(RuntimeError, "DNS blocked"):
                    socket.getaddrinfo("example.invalid", 443)
            self.assertFalse(database.parent.exists())
            self.assertEqual(os.environ["LUIGI_WEB_CARDS_DB"], "must-not-be-opened.db")

    def test_deck_and_collection_reads_are_not_per_row_selects(self):
        with synthetic_database(1000, 250) as (cards, _database):
            deck, deck_trace = trace_operation(cards, lambda: cards.list_deck_cards(1))
            collection, collection_trace = trace_operation(cards, lambda: cards.list_collection("mtg"))
            _, small_trace = trace_operation(cards, lambda: cards.list_collection("mtg", limit=12))
            self.assertEqual(len(deck), 120)
            self.assertEqual(len(collection), 250)
            self.assertTrue(all(row["exact_cost_minor"] == 150 and row["actual_cost_minor"] == 150
                                for row in collection))
            with cards.transaction() as connection:
                connection.execute("DELETE FROM deck_cards WHERE card_id > 12")
            small_deck, small_deck_trace = trace_operation(cards, lambda: cards.list_deck_cards(1))
            self.assertEqual(len(small_deck), 12)
            for trace in (deck_trace, small_deck_trace, collection_trace, small_trace):
                self.assertEqual(trace["domain_select_statements"], 1)
                self.assertLessEqual(trace["schema_select_statements"], 1)
                self.assertLessEqual(trace["schema_checks"], 1)
                self.assertLessEqual(trace["table_info_checks"], 1)
                self.assertEqual(trace["select_statements"],
                                 trace["domain_select_statements"] + trace["schema_select_statements"])
            for large, small in ((deck_trace, small_deck_trace), (collection_trace, small_trace)):
                for key in ("connections", "statements", "select_statements", "domain_select_statements",
                            "schema_select_statements", "schema_checks", "table_info_checks"):
                    self.assertEqual(large[key], small[key], key)
            deck_plans = [item for item in deck_trace["query_plans"]
                          if item["kind"] == "domain" and "FROM deck_cards dc JOIN cards c" in item["query"]]
            self.assertEqual(len(deck_plans), 1)
            plan = " ".join(deck_plans[0]["plan"])
            self.assertIn("CORRELATED SCALAR SUBQUERY", plan)
            self.assertRegex(plan, r"SEARCH col USING (?:COVERING )?INDEX")
            collection_plans = [item for item in collection_trace["query_plans"]
                                if item["kind"] == "domain" and "FROM collection col JOIN cards c" in item["query"]]
            self.assertEqual(len(collection_plans), 1)
            self.assertIn("FROM collection_lots", collection_plans[0]["query"])

    def test_trace_classifies_only_recognized_schema_reads(self):
        with synthetic_database(120, 24) as (cards, _database):
            def read_queries():
                with cards._connect() as connection:
                    for statement in (
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'collection_lots'",
                        "select name from main.sqlite_master",
                        "SELECT name FROM pragma_table_info('collection')",
                        "PRAGMA table_info(collection)",
                        "SELECT 'sqlite_master', 'pragma_table_info', name FROM cards LIMIT 1",
                        "SELECT name AS sqlite_master FROM cards LIMIT 1",
                        "SELECT * FROM sqlite_master JOIN cards ON cards.name = sqlite_master.name LIMIT 1",
                        "WITH sqlite_master AS (SELECT name FROM cards) SELECT * FROM sqlite_master LIMIT 1",
                    ):
                        connection.execute(statement).fetchall()

            _, trace = trace_operation(cards, read_queries)
            self.assertEqual(trace["select_statements"], 7)
            self.assertEqual(trace["schema_select_statements"], 3)
            self.assertEqual(trace["domain_select_statements"], 4)
            self.assertEqual(trace["table_info_checks"], 1)
            self.assertEqual([item["kind"] for item in trace["query_plans"]],
                             ["schema"] * 3 + ["domain"] * 4)

    def test_trace_explains_connection_local_functions_without_executing_them(self):
        with synthetic_database(120, 24) as (cards, _database):
            calls = []

            def identity(value):
                calls.append(value)
                return value

            def read_query():
                with cards._connect() as connection:
                    connection.create_function("synthetic_identity", 1, identity, deterministic=True)
                    return connection.execute("SELECT synthetic_identity(id) FROM cards WHERE id = 1").fetchone()[0]

            result, trace = trace_operation(cards, read_query)
            self.assertEqual(result, 1)
            self.assertEqual(calls, [1])
            self.assertEqual(trace["connections"], 1)
            self.assertEqual(trace["statements"], 1)
            self.assertEqual(trace["domain_select_statements"], 1)
            self.assertEqual(trace["schema_select_statements"], 0)
            self.assertRegex(" ".join(trace["query_plans"][0]["plan"]), r"SEARCH cards USING (?:INTEGER )?PRIMARY KEY")

    def test_checklist_and_analysis_query_counts_do_not_scale_with_dataset(self):
        observations = {}
        for catalog_size, holdings, deck_rows in ((120, 12, 12), (1200, 240, 120)):
            with self.subTest(catalog_size=catalog_size), synthetic_database(catalog_size, holdings) as (cards, _database):
                from luigi_web.modules.cards import analysis

                with cards.transaction() as connection:
                    connection.execute("DELETE FROM deck_cards WHERE card_id > ?", (deck_rows,))
                for mode in ("exact", "any"):
                    result, trace = trace_operation(cards, lambda: analysis.build_checklist("mtg", 1, match_mode=mode))
                    self.assertEqual(len(result["items"]), deck_rows if mode == "exact" else deck_rows // 2)
                    self.assertEqual(result["totals"]["required"], deck_rows)
                    self.assertEqual(result["totals"]["allocated"] + result["totals"]["missing"], deck_rows)
                    self.assertEqual(result["effective_match_mode"], mode)
                    for item in result["items"]:
                        self.assertEqual(item["identity_basis"], "exact_printing" if mode == "exact" else "oracle_id")
                        self.assertEqual(item["matching_printings_count"], 1 if mode == "exact" else 2)
                    observations.setdefault(mode, []).append(trace)
                result, trace = trace_operation(cards, lambda: analysis.deck_analysis("mtg", 1))
                self.assertEqual(result["counts"]["all"], deck_rows)
                self.assertEqual(result["counts"]["playable"], deck_rows)
                self.assertEqual(sum(category["quantity"] for category in result["categories"]), deck_rows)
                observations.setdefault("analysis", []).append(trace)
            self.assertFalse(_database.parent.exists())
        for name, traces in observations.items():
            for trace in traces:
                self.assertEqual(trace["domain_select_statements"], 2 if name == "analysis" else 3, name)
                self.assertEqual(trace["schema_select_statements"], 0, name)
                self.assertEqual(trace["connections"], 1, name)
            for key in ("connections", "statements", "select_statements", "domain_select_statements",
                        "schema_select_statements", "schema_checks", "table_info_checks"):
                self.assertEqual(traces[0][key], traces[1][key], (name, key))

    def test_benchmark_cli_renders_without_host_and_removes_data(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "benchmark_cards.py"),
             "--catalog-size", "600", "--holdings", "120", "--samples", "1"],
            cwd=ROOT, capture_output=True, text=True, timeout=60, check=True,
        )
        report = json.loads(result.stdout)
        self.assertFalse(report["host_imported"])
        self.assertTrue(report["network_blocked"])
        self.assertTrue(report["temporary_database_removed"])
        self.assertEqual(report["operations"]["list_deck_cards"]["returned_rows_or_characters"], 120)
        self.assertEqual(report["operations"]["catalog_default"]["returned_rows_or_characters"], 48)
        self.assertGreater(report["operations"]["render_deck_state"]["returned_rows_or_characters"], 1000)
        self.assertEqual(report["dataset"]["purchase_lots"], 120)
        for name, expected_items in (("build_checklist_exact", 120), ("build_checklist_equivalent", 60)):
            operation = report["operations"][name]
            self.assertEqual(operation["returned_rows_or_characters"], expected_items)
            self.assertEqual(operation["result_totals"]["required"], 120)
            self.assertEqual(operation["domain_select_statements"], 3)
        self.assertEqual(report["operations"]["deck_analysis"]["domain_select_statements"], 2)
        self.assertEqual(report["operations"]["deck_analysis"]["result_totals"]["all"], 120)

    def test_preview_has_forty_category_hundred_deck_and_only_local_images(self):
        from fastapi.testclient import TestClient
        from scripts.preview_cards import loopback_network_only, preview_app, seed_cards
        from luigi_web.modules.cards import repository as cards

        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
            "LUIGI_WEB_CARDS_DB": str(Path(temporary) / "cards.db"),
            "LUIGI_WEB_DATA_DIR": temporary,
            "LUIGI_WEB_UI_TOKEN": "synthetic-preview-token",
        }, clear=True), loopback_network_only():
            seed_cards()
            decks = {deck["name"]: deck for deck in cards.list_decks("mtg")}
            self.assertEqual(decks["Hundred Card Deck"]["card_count"], 100)
            forty = cards.list_deck_cards(decks["Forty Card Category"]["id"])
            self.assertEqual(len(forty), 40)
            self.assertEqual({card["category"] for card in forty}, {"Forty Card Category"})
            self.assertTrue(all(card["image_normal"].startswith("/__preview__/") for card in forty))
            application = preview_app()

            async def loopback_app(scope, receive, send):
                await application({**scope, "client": ("127.0.0.1", 51000)}, receive, send)

            with TestClient(loopback_app, base_url="http://localhost") as client:
                response = client.get("/")
                self.assertEqual(response.status_code, 200)
                self.assertIn("img-src 'self' data:", response.headers["Content-Security-Policy"])
                image = client.get(forty[0]["image_normal"])
                self.assertEqual(image.status_code, 200)
                self.assertTrue(image.content.startswith(b"\x89PNG"))
            with self.assertRaisesRegex(RuntimeError, "external DNS disabled"):
                socket.getaddrinfo("example.invalid", 443)


class CatalogQueryPerformanceTests(unittest.TestCase):
    @staticmethod
    def _seed_catalog_variants(cards):
        updates = []
        for card_id in range(1, 121):
            identity, printing = divmod(card_id - 1, 4)
            raw = {
                "oracle_id": f"identity-{identity}" if identity % 5 else None,
                "illustration_id": f"art-{identity}-{printing // 2}" if identity % 4 else None,
                "released_at": f"{2020 + printing}-01-01" if printing else None,
                "promo": printing == 2,
                "set_type": "universes_beyond" if printing == 2 else "expansion",
                "layout": "token" if identity % 7 == 0 and printing == 3 else "normal",
                "games": ["paper"] if printing % 2 else ["arena", "mtgo"],
                "prices": {"tix": str(printing / 2) if printing else None},
                "power": "*" if printing == 0 else str(identity % 6),
                "toughness": str(printing + 1) if printing else None,
                "artist": "Example Artist" if printing % 2 else "example artist",
                "lang": "en",
                "synthetic_padding": "x" * 512,
            }
            if printing % 2:
                raw["frame_effects"] = ["showcase"] if printing == 3 else []
            updates.append((
                f"Example Identity {identity:02d}", f"S{printing}",
                "Example Set" if printing else None,
                ("2a", "2b", "10", None)[printing],
                ("common", "uncommon", "rare", None)[printing],
                identity % 5 if printing else None,
                100 * printing if printing else None,
                75 * (3 - printing) if printing != 2 else None,
                '["R", "U"]' if printing % 2 else '[]',
                "{U/R}" if printing % 2 else "{2}", json.dumps(raw), card_id,
            ))
        with cards.transaction() as connection:
            connection.executemany(
                "UPDATE cards SET name = ?, set_code = ?, set_name = ?, "
                "collector_number = ?, rarity = ?, cmc = ?, price_usd_minor = ?, "
                "price_eur_minor = ?, colors_json = ?, mana_cost = ?, raw_json = ? WHERE id = ?",
                updates,
            )

    @staticmethod
    def _legacy_catalog(cards, connection, filters, page=1, page_size=12):
        """Reference the original wide-ranking SQL on a small synthetic fixture."""
        normalized = cards.normalize_catalog_filters("mtg", filters)
        clauses = ["c.game_code = ?"]
        values = ["mtg"]
        cards._catalog_filter_clauses(normalized, clauses, values)
        partition = {
            "cards": "COALESCE(json_extract(c.raw_json, '$.oracle_id'), LOWER(c.name))",
            "art": "COALESCE(json_extract(c.raw_json, '$.illustration_id'), json_extract(c.raw_json, '$.oracle_id'), LOWER(c.name))",
        }.get(normalized["unique"])
        ranking = (
            f"ROW_NUMBER() OVER (PARTITION BY {partition} ORDER BY {cards._catalog_prefer_sql(normalized['prefer'])}, c.id)"
            if partition else "1"
        )
        rows = [dict(row) for row in connection.execute(
            f"""
            SELECT c.id, c.name, c.set_code, c.set_name, c.collector_number,
                   c.rarity, c.type_line, c.mana_cost, c.image_small,
                   c.image_normal, c.price_usd_minor, c.price_usd_foil_minor, c.source
              FROM (SELECT c.*, {ranking} AS catalog_rank FROM cards c
                    WHERE {' AND '.join(clauses)}) c
             WHERE c.catalog_rank = 1
             ORDER BY {cards._catalog_order_sql(normalized['order'], normalized['direction'])}, c.id
            """, values,
        )]
        safe_size = min(max(int(page_size), 12), 96)
        pages = max(1, (len(rows) + safe_size - 1) // safe_size)
        safe_page = min(max(int(page), 1), pages)
        offset = (safe_page - 1) * safe_size
        return {
            "rows": rows[offset:offset + safe_size], "total": len(rows),
            "page": safe_page, "pages": pages, "page_size": safe_size,
            "filters": normalized,
            "active_filter_count": cards._active_catalog_filter_count(normalized),
        }

    def test_catalog_matches_wide_ranking_for_all_sorts_directions_and_preferences(self):
        with synthetic_database(120, 24) as (cards, _database):
            self._seed_catalog_variants(cards)
            with cards._connect() as connection, patch.object(cards, "init_db"):
                for unique in ("prints", "cards", "art"):
                    preferences = cards.CATALOG_PREFERS if unique != "prints" else ("best",)
                    for order in cards.CATALOG_SORTS:
                        for direction in ("asc", "desc"):
                            for prefer_index, prefer in enumerate(preferences):
                                filters = {
                                    "unique": unique, "order": order,
                                    "direction": direction, "prefer": prefer,
                                }
                                for page in (1, 2, 999):
                                    with self.subTest(**filters, page=page):
                                        page_size = 12 + prefer_index % 3
                                        expected = self._legacy_catalog(cards, connection, filters, page, page_size)
                                        actual = cards.browse_catalog("mtg", filters=filters, page=page, page_size=page_size)
                                        self.assertEqual(actual, expected)
                                        self.assertEqual(len({row["id"] for row in actual["rows"]}), len(actual["rows"]))

    def test_catalog_identity_fallbacks_and_valid_empty_json(self):
        with synthetic_database(120, 24) as (cards, _database):
            with cards.transaction() as connection:
                connection.executemany("UPDATE cards SET name = ?, raw_json = ? WHERE id = ?", [
                    ("Fallback Name", None, 1),
                    ("fallback name", "{}", 2),
                    ("Fallback Name", '{"oracle_id": null, "illustration_id": null}', 3),
                    ("Fallback Separate", "{}", 4),
                    ("Fallback Alias A", '{"oracle_id": "shared", "illustration_id": "first"}', 5),
                    ("Fallback Alias B", '{"oracle_id": "shared", "illustration_id": "second"}', 6),
                    ("Fallback Empty", '{"oracle_id": ""}', 7),
                    ("Fallback Empty Other", '{"oracle_id": ""}', 8),
                    ("Fallback Name", '{"oracle_id": "different"}', 9),
                    ("Fallback Array", "[]", 10),
                    ("Fallback Name", "null", 11),
                ])
            for unique, expected in (("cards", {1, 4, 5, 7, 9, 10}), ("art", {1, 4, 5, 6, 7, 9, 10})):
                with self.subTest(unique=unique):
                    result = cards.browse_catalog("mtg", filters={"q": "Fallback", "unique": unique}, page_size=96)
                    self.assertEqual({row["id"] for row in result["rows"]}, expected)
                    self.assertEqual(result["total"], len(expected))
                    empty = cards.browse_catalog("mtg", filters={"q": "Missing", "unique": unique}, page=999)
                    self.assertEqual((empty["rows"], empty["total"], empty["page"], empty["pages"]), ([], 0, 1, 1))

    def test_catalog_filters_apply_before_ranking_in_both_queries(self):
        with synthetic_database(120, 24) as (cards, _database):
            self._seed_catalog_variants(cards)
            selections = (
                {"set_code": "s3"}, {"include_extras": "1"}, {"criteria": ["token"]},
                {"criteria": ["splitmana"], "colors": ["U", "R"], "color_mode": "exact"},
                {"games": ["paper"], "games_mode": "not"},
                {"price_value": "0", "price_operator": "=", "price_currency": "eur"},
                {"price_value": "1", "price_operator": ">=", "price_currency": "tix"},
                {"q": "Identity 02", "stat": "power", "stat_value": "2"},
            )
            with cards._connect() as connection, patch.object(cards, "init_db"):
                for unique in ("prints", "cards", "art"):
                    for selection in selections:
                        for prefer in ("best", "newest", "usdlow"):
                            filters = {"unique": unique, "prefer": prefer, **selection}
                            with self.subTest(**filters):
                                self.assertEqual(
                                    cards.browse_catalog("mtg", filters=filters, page_size=12),
                                    self._legacy_catalog(cards, connection, filters),
                                )

    def test_catalog_escaped_filters_remain_literal_and_parameterized(self):
        with synthetic_database(120, 24) as (cards, _database):
            literal = "%_\\'"
            with cards.transaction() as connection:
                for card_id, text in ((1, literal), (2, "decoy")):
                    connection.execute(
                        "UPDATE cards SET name = ?, oracle_text = ?, type_line = ?, raw_json = ? WHERE id = ?",
                        (f"Example {text}", text, text, json.dumps({"artist": text, "flavor_text": text, "block": text}), card_id),
                    )
            for unique in ("prints", "cards", "art"):
                for key in ("q", "oracle", "type_line", "artist", "flavor", "group", "lore"):
                    with self.subTest(unique=unique, key=key):
                        result = cards.browse_catalog("mtg", filters={key: literal, "unique": unique})
                        self.assertEqual([row["id"] for row in result["rows"]], [1])
                        self.assertEqual(result["total"], 1)
                result = cards.browse_catalog("mtg", filters={"q": "' OR 1=1 --", "unique": unique})
                self.assertEqual(result["total"], 0)

    def test_deduplicated_count_avoids_ranking_and_page_looks_up_details_by_id(self):
        with synthetic_database(600, 120) as (cards, _database):
            for unique in ("cards", "art"):
                with self.subTest(unique=unique):
                    result, trace = trace_operation(cards, lambda: cards.browse_catalog("mtg", filters={"unique": unique}))
                    self.assertEqual((result["total"], len(result["rows"])), (300, 48))
                    counts = [item for item in trace["query_plans"] if item["query"].startswith("SELECT COUNT(DISTINCT ")]
                    pages = [item for item in trace["query_plans"] if "ROW_NUMBER() OVER" in item["query"]]
                    self.assertEqual((len(counts), len(pages)), (1, 1))
                    self.assertNotIn("ROW_NUMBER", counts[0]["query"])
                    self.assertFalse(any("ORDER BY" in detail for detail in counts[0]["plan"]))
                    self.assertIn("SELECT c.id, ROW_NUMBER()", pages[0]["query"])
                    self.assertNotIn("SELECT c.*", pages[0]["query"])
                    self.assertRegex(" ".join(pages[0]["plan"]), r"SEARCH c USING (?:INTEGER )?PRIMARY KEY")


if __name__ == "__main__":
    unittest.main()