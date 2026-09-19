"""Synthetic, offline, temporary-database checks for card planning and analysis."""
from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import patch

from fastapi import Request

from luigi_web.modules.cards import analysis, repository


class CardsAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {
            "LUIGI_WEB_CARDS_DB": os.path.join(self.directory.name, "cards.sqlite3"),
            "LUIGI_WEB_CARDS_REFRESH_HOURS": "0",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        repository.init_db()
        self.deck = self.make_deck()

    def make_deck(self, game="mtg", format="commander"):
        with repository._connect() as conn:
            return conn.execute("INSERT INTO decks(game_code, name, format) VALUES (?, ?, ?)",
                                (game, "Synthetic deck", format)).lastrowid

    def card(self, name="Example Relic", game="mtg", oracle=None, price=125, raw=None, **extra):
        metadata = {"oracle_id": oracle} if oracle is not None else {}
        metadata.update(raw or {})
        values = {"game_code": game, "external_id": "synthetic", "source": "manual", "name": name,
                  "price_usd_minor": price, "price_updated_at": "2026-09-01", "raw_json": json.dumps(metadata),
                  "type_line": "Artifact", "cmc": 2, "mana_cost": "{2}", "oracle_text": "Synthetic rules text.", **extra}
        with repository._connect() as conn:
            values["external_id"] = "synthetic-" + str(conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
            return conn.execute(f"INSERT INTO cards ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                                list(values.values())).lastrowid

    def slot(self, card, qty=1, board="main", category="", deck=None):
        with repository._connect() as conn:
            conn.execute("INSERT INTO deck_cards(deck_id, card_id, qty, board, category) VALUES (?, ?, ?, ?, ?)",
                         (deck or self.deck, card, qty, board, category))

    def own(self, card, qty=1, foil=0, condition="NM"):
        with repository._connect() as conn:
            conn.execute("INSERT INTO collection(card_id, qty, foil, condition) VALUES (?, ?, ?, ?)",
                         (card, qty, foil, condition))

    def test_repeated_categories_and_lots_count_holdings_once(self):
        card = self.card()
        self.slot(card, 3, category="Ramp")
        self.slot(card, 2, category="Utility")
        self.own(card, 2)
        self.own(card, 1, foil=1)
        result = analysis.build_checklist("mtg", self.deck)
        item, = result["items"]
        self.assertEqual((item["required"], item["owned"], item["allocated"], item["missing"]), (5, 3, 3, 2))
        self.assertEqual(item["missing_cost_minor"], 250)

    def test_equivalence_uses_oracle_not_same_name(self):
        first = self.card(oracle="identity-one")
        second = self.card(name="Example Alternate Name", oracle="identity-one", price=75)
        unrelated = self.card(oracle="identity-two")
        fallback = self.card()
        self.slot(first, 2)
        self.slot(second, 1, category="Utility")
        for card in (second, unrelated, fallback):
            self.own(card, 2)
        result = analysis.build_checklist("mtg", self.deck, match_mode="any")
        item, = result["items"]
        self.assertEqual((item["required"], item["owned"], item["missing"]), (3, 2, 1))
        self.assertEqual({row["card_id"] for row in item["matching_printings"]}, {first, second})
        self.assertEqual(item["est_unit_minor"], 75)
        self.assertNotIn("Example", item["key"])

    def test_name_fallback_only_for_absent_oracle_and_pokemon_is_exact(self):
        first = self.card(name="Example Relic")
        second = self.card(name="example relic")
        self.slot(first, 2)
        self.own(second)
        item, = analysis.build_checklist("mtg", self.deck, match_mode="any")["items"]
        self.assertEqual((item["owned"], item["identity_basis"]), (1, "name_fallback"))
        pokemon_deck = self.make_deck("pokemon")
        pokemon = self.card(game="pokemon")
        alternate = self.card(game="pokemon")
        self.slot(pokemon, deck=pokemon_deck)
        self.own(alternate, 4)
        result = analysis.build_checklist("pokemon", pokemon_deck, match_mode="any")
        self.assertEqual(result["items"][0]["missing"], 1)
        self.assertEqual(result["effective_match_mode"], "exact")
        self.assertIn("unsupported", result["match_notice"])

    def test_reservations_are_ordered_share_one_pool_and_are_not_persistent(self):
        card = self.card(oracle="shared")
        alternate = self.card(oracle="shared")
        self.own(card, 4)
        self.own(alternate, 1)
        self.slot(card, 3)
        earlier = self.make_deck()
        later = self.make_deck()
        self.slot(alternate, 2, category="Ramp", deck=earlier)
        self.slot(card, 2, category="Other", deck=earlier)
        self.slot(card, 4, deck=later)
        result = analysis.build_checklist("mtg", self.deck, match_mode="any", reserve_deck_ids=[earlier, later])
        self.assertEqual([row["allocated"] for row in result["reservations"]], [4, 1])
        self.assertEqual(result["items"][0]["missing"], 3)
        reversed_result = analysis.build_checklist("mtg", self.deck, match_mode="any", reserve_deck_ids=[later, earlier])
        self.assertEqual([row["deck_id"] for row in reversed_result["reservations"]], [later, earlier])
        self.assertEqual(analysis.build_checklist("mtg", self.deck, match_mode="any")["items"][0]["owned"], 5)

    def test_boards_and_unknown_prices_do_not_imply_zero(self):
        card = self.card(price=None)
        self.slot(card, 2)
        self.slot(card, 3, board="side")
        self.slot(card, 4, board="maybe")
        result = analysis.build_checklist("mtg", self.deck)
        self.assertEqual(result["totals"]["missing"], 2)
        self.assertIsNone(result["totals"]["estimated_missing_cost_minor"])
        self.assertEqual(result["totals"]["missing_price_count"], 2)
        self.assertEqual(analysis.build_checklist("mtg", self.deck, boards=["side", "maybe"])["totals"]["required"], 7)
        self.assertEqual(analysis.build_checklist("mtg", self.deck, boards=[])["totals"]["required"], 0)

    def test_lowest_cached_alternative_is_labeled(self):
        first = self.card(oracle="shared", price=None)
        second = self.card(oracle="shared", price=90)
        self.card(oracle="shared", price=110)
        self.slot(first, 2)
        item, = analysis.build_checklist("mtg", self.deck, match_mode="any")["items"]
        self.assertEqual((item["estimate_card_id"], item["price_basis"], item["missing_cost_minor"]),
                         (second, "lowest_cached_equivalent", 180))

    def test_cross_game_current_duplicate_and_unknown_decks_rejected(self):
        other = self.make_deck("pokemon")
        for ids in ([other], [self.deck], [999999], [other, other]):
            with self.subTest(ids=ids), self.assertRaises(analysis.AnalysisError):
                analysis.build_checklist("mtg", self.deck, reserve_deck_ids=ids)

    def test_malformed_metadata_and_wrong_game_cards_are_not_equivalents(self):
        card = self.card(raw_json="{broken")
        other = self.card()
        self.slot(card)
        self.own(other, 5)
        item, = analysis.build_checklist("mtg", self.deck, match_mode="any")["items"]
        self.assertEqual((item["owned"], item["identity_basis"]), (0, "exact_printing"))
        foreign = self.card(game="pokemon")
        self.slot(foreign)
        with self.assertRaises(analysis.AnalysisError):
            analysis.build_checklist("mtg", self.deck)

    def issues(self, deck=None, game="mtg", code=None):
        issues = analysis.deck_analysis(game, deck or self.deck)["advisory"]["issues"]
        return [issue for issue in issues if code is None or issue["code"] == code]

    def test_analysis_boards_categories_mana_colors_and_price_dates(self):
        relic = self.card(oracle="relic", type_line="Artifact Creature", cmc=2.5,
                          colors_json='["U", "R"]', price_updated_at="2026-08-01")
        land = self.card(type_line="Basic Land - Island", cmc=0, price=None, raw={"colors": []})
        variable = self.card(mana_cost="{X}{U}", cmc=1, price=0)
        unknown = self.card(cmc=None, price_updated_at=None)
        self.slot(relic, 2, category="Utility")
        self.slot(land, 3)
        self.slot(variable)
        self.slot(unknown)
        self.slot(relic, 4, board="side", category="Utility")
        self.slot(land, 5, board="maybe")
        result = analysis.deck_analysis("mtg", self.deck)
        self.assertEqual(result["counts"], {"main": 7, "commander": 0, "playable": 7, "side": 4, "maybe": 5, "all": 16})
        self.assertEqual(result["mana_curve"]["buckets"], [{"label": "2.5", "quantity": 2}, {"label": "X", "quantity": 1}, {"label": "Unknown", "quantity": 1}])
        self.assertEqual(result["mana_curve"]["land_count"], 3)
        self.assertIn({"label": "R", "quantity": 2}, result["colors"])
        self.assertIn({"label": "Creature", "quantity": 2}, result["types"])
        main = next(board for board in result["boards"] if board["board"] == "main")
        self.assertEqual(main["prices"]["missing_price_count"], 3)
        self.assertEqual(main["prices"]["priced_count"], 4)
        self.assertIsNone(main["prices"]["estimated_value_minor"])
        self.assertEqual(main["prices"]["as_of_min"], "2026-08-01T00:00:00")
        self.assertEqual(main["prices"]["as_of_max"], "2026-09-01T00:00:00")
        self.assertEqual(main["prices"]["undated_priced_count"], 1)
        self.assertIn({"board": "side", "category": "Utility", "quantity": 4}, result["categories"])

    def test_commander_singleton_across_printings_basics_and_eligibility(self):
        commander = self.card(oracle="commander", type_line="Legendary Creature - Example", raw={"color_identity": ["U"]})
        first = self.card(oracle="relic", raw={"color_identity": ["R"]})
        alternate = self.card(oracle="relic", raw={"color_identity": ["U"]})
        land = self.card(type_line="Basic Land - Island", raw={"color_identity": ["U"]})
        self.slot(commander, board="commander")
        self.slot(first)
        self.slot(alternate)
        self.slot(land, 97)
        self.assertEqual(self.issues(code="deck_size")[0]["severity"], "pass")
        self.assertEqual(self.issues(code="commander_eligibility")[0]["severity"], "pass")
        self.assertEqual(self.issues(code="copy_limits")[0]["severity"], "warning")
        self.assertEqual(self.issues(code="color_identity")[0]["count"], 1)
        self.assertEqual(self.issues(code="cached_legalities")[0]["severity"], "unknown")

    def test_copy_exceptions_known_bounded_and_unknown(self):
        unlimited = self.card(name="Example Repeat", oracle="repeat", oracle_text="A deck can have any number of cards named Example Repeat.")
        bounded = self.card(name="Example Seven", oracle="seven", oracle_text="A deck can have up to seven cards named Example Seven.")
        uncertain = self.card(name="Example Unclear", oracle="unclear", oracle_text="Your deck can have a special number of these cards.")
        self.slot(unlimited, 30)
        self.slot(bounded, 8)
        self.slot(uncertain, 20)
        issues = self.issues(code="copy_limits")
        self.assertEqual([(issue["severity"], issue["count"]) for issue in issues], [("warning", 1), ("unknown", 20)])

    def test_constructed_checks_side_limits_bans_and_ignore_maybe(self):
        for format in ("standard", "modern", "pioneer", "legacy"):
            with self.subTest(format=format):
                deck = self.make_deck(format=format)
                main = self.card(oracle="main-" + format, raw={"legalities": {format: "legal"}})
                side = self.card(oracle="side-" + format, raw={"legalities": {format: "banned"}})
                self.slot(main, 60, deck=deck)
                self.slot(side, 16, board="side", deck=deck)
                self.slot(side, 500, board="maybe", deck=deck)
                self.assertEqual(self.issues(deck, code="main_size")[0]["severity"], "pass")
                self.assertEqual(self.issues(deck, code="side_size")[0]["severity"], "warning")
                self.assertEqual(self.issues(deck, code="cached_legalities")[0]["count"], 16)
                self.assertEqual(self.issues(deck, code="copy_limits")[0]["count"], 2)

    def test_synonyms_unsupported_formats_and_missing_metadata_never_clean(self):
        for format in ("EDH", "Historic", "", "Commander / Modern"):
            deck = self.make_deck(format=format)
            result = analysis.deck_analysis("mtg", deck)["advisory"]
            self.assertIsNone(result["supported_format"])
            self.assertGreater(result["severity_counts"]["unknown"], 0)
            self.assertFalse(result["writes_blocked"])
        invalid = self.card(raw_json='{"legalities": [], "color_identity": "U"}', oracle_text=None)
        self.slot(invalid, board="commander")
        self.assertEqual(self.issues(code="color_identity")[0]["severity"], "unknown")
        self.assertEqual(self.issues(code="commander_eligibility")[0]["severity"], "unknown")

    def test_explicit_commander_eligibility_and_partner_pair_unknown(self):
        first = self.card(type_line="Legendary Planeswalker - Example", oracle_text="Example can be your commander.")
        second = self.card(type_line="Legendary Creature - Example")
        self.slot(first, board="commander")
        self.slot(second, board="commander")
        self.assertEqual(self.issues(code="commander_pair")[0]["severity"], "unknown")
        self.assertTrue(all(issue["severity"] == "pass" for issue in self.issues(code="commander_eligibility")))

    def test_pokemon_and_riftbound_do_not_invent_mtg_statistics(self):
        pokemon_deck = self.make_deck("pokemon")
        pokemon = self.card(game="pokemon", raw={"supertype": "Pokemon", "types": ["Psychic", "Water"]})
        self.slot(pokemon, 3, deck=pokemon_deck)
        result = analysis.deck_analysis("pokemon", pokemon_deck)
        self.assertEqual(result["supertypes"], [{"label": "Pokemon", "quantity": 3}])
        self.assertEqual(len(result["types"]), 2)
        self.assertEqual(result["mana_curve"]["buckets"], [])
        self.assertEqual(result["colors"], [])
        riftbound_deck = self.make_deck("riftbound")
        riftbound = self.card(game="riftbound", type_line="Synthetic Unit")
        self.slot(riftbound, 2, deck=riftbound_deck)
        result = analysis.deck_analysis("riftbound", riftbound_deck)
        self.assertEqual(result["types"], [{"label": "Synthetic Unit", "quantity": 2}])
        self.assertEqual(result["mana_curve"]["buckets"], [])

    def test_queries_are_bulk_and_snapshots_complete_without_writes(self):
        for number in range(105):
            self.slot(self.card(name=f"Synthetic Card {number}", oracle=f"synthetic-{number}"))
        original_connect = repository._connect
        statements = []
        @contextmanager
        def traced():
            with original_connect() as conn:
                conn.set_trace_callback(statements.append)
                yield conn

        with patch.object(repository, "_connect", traced):
            result = analysis.deck_analysis("mtg", self.deck)
            build = analysis.build_checklist("mtg", self.deck, match_mode="any")
        self.assertEqual(result["counts"]["main"], 105)
        self.assertEqual(len(build["items"]), 105)
        reads = [statement for statement in statements if statement.startswith(("SELECT", "WITH"))]
        self.assertEqual(len(reads), 5)
        self.assertFalse(any(statement.startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements))

    def client(self, authenticated=True):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from luigi_web import auth
        from luigi_web.modules.cards.analysis_routes import router

        app = FastAPI()
        app.include_router(router)
        environment = patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "synthetic-test-token"})
        environment.start()
        self.addCleanup(environment.stop)
        client = TestClient(app)
        self.addCleanup(client.close)
        if authenticated:
            client.cookies.set(auth.COOKIE_NAME, "synthetic-test-token")
        return client

    def test_routes_authentication_no_store_and_read_only_contract(self):
        self.slot(self.card(), 2)
        client = self.client()
        anonymous = self.client(authenticated=False)
        for suffix in ("build", "build.csv", "analysis.json"):
            path = f"/cards/mtg/decks/{self.deck}/{suffix}"
            self.assertIn(anonymous.get(path, follow_redirects=False).status_code, (303, 401))
            response = client.get(path)
            self.assertEqual(response.status_code, 200, response.text[:500])
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(client.post(path).status_code, 405)
        page = client.get(f"/cards/mtg/decks/{self.deck}/build").text
        self.assertIn("Any equivalent printing", page)
        self.assertIn("/module-assets/cards/analysis.css", page)
        self.assertIn("/static/icons/lucide/list-checks.svg", page)
        self.assertNotIn("data-lucide", page)
        self.assertNotIn("https://", page)

    def test_csv_escapes_formulas_and_keeps_unknown_prices_blank(self):
        for name in ("=EXAMPLE(1)", " +Example", "@Example", "\tExample", "-Example"):
            self.slot(self.card(name=name, price=None), 2)
        owned = self.card(name="Owned synthetic card")
        self.slot(owned)
        self.own(owned)
        response = self.client().get(f"/cards/mtg/decks/{self.deck}/build.csv")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("attachment", response.headers["content-disposition"])
        rows = list(csv.DictReader(io.StringIO(response.text)))
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row["name"].startswith("'") for row in rows))
        self.assertTrue(all(row["missing_cost_minor"] == "" for row in rows))
        self.assertEqual({row["currency"] for row in rows}, {"USD"})

    def test_routes_validate_options_and_keep_reservation_priority(self):
        card = self.card()
        self.slot(card, 2)
        self.slot(card, 3, board="side")
        self.own(card, 3)
        first, second = self.make_deck(), self.make_deck()
        self.slot(card, 2, deck=first)
        self.slot(card, 2, deck=second)
        client = self.client()
        path = f"/cards/mtg/decks/{self.deck}/build"
        response = client.get(path, params=[("reserve_deck_id", str(second)), ("reserve_deck_id", str(first))])
        self.assertEqual(response.status_code, 200)
        self.assertIn('aria-label="Reserve priority 3"', response.text)
        empty = client.get(path, params={"boards_set": "1"})
        self.assertIn("No cards on the selected boards", empty.text)
        for params in ({"board": "other"}, {"match_mode": "oracle"}, {"reserve_deck_id": str(self.deck)},
                       {"reserve_deck_id": "invalid"}, {"choices_page": "0"}):
            with self.subTest(params=params):
                response = client.get(path, params=params)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.headers["cache-control"], "no-store")
        foreign = self.make_deck("pokemon")
        self.assertEqual(client.get(path, params={"reserve_deck_id": str(foreign)}).status_code, 404)

    def test_choices_are_paginated_same_game_and_exclude_current(self):
        foreign = self.make_deck("pokemon")
        for _number in range(28):
            self.make_deck()
        first = analysis.reservation_choices("mtg", self.deck)
        self.assertEqual(len(first["items"]), 25)
        self.assertTrue(first["has_more"])
        self.assertFalse({foreign, self.deck} & {row["id"] for row in first["items"]})
        self.assertEqual(len(analysis.reservation_choices("mtg", self.deck, page=2)["items"]), 3)

    def test_analysis_partial_compiles_and_renders_with_only_analysis_contract(self):
        from luigi_web.modules.cards.analysis_routes import templates

        self.slot(self.card(), 2)
        template = templates.env.get_template("cards/partials/deck_analysis.html")
        rendered = template.render(analysis=analysis.deck_analysis("mtg", self.deck))
        self.assertIn("Nonland mana curve", rendered)
        self.assertIn("Format advisory", rendered)
        self.assertIn("Unknown", rendered)
        self.assertIn(f"/cards/mtg/decks/{self.deck}/build", rendered)

    def test_unicode_fallback_is_casefolded_without_weakening_oracle_identity(self):
        first = self.card(name="Example Stra\u00dfe")
        alternate = self.card(name="example STRASSE")
        oracle_card = self.card(name="Example Stra\u00dfe", oracle="different")
        self.slot(first, 2)
        self.own(alternate)
        self.own(oracle_card, 10)
        item, = analysis.build_checklist("mtg", self.deck, match_mode="any")["items"]
        self.assertEqual(item["owned"], 1)
        self.assertEqual({row["card_id"] for row in item["matching_printings"]}, {first, alternate})

    def test_unknown_copy_exception_never_becomes_a_copy_warning(self):
        card = self.card(oracle="unknown-limit", oracle_text="A deck can have up to countless cards named Example Relic.")
        self.slot(card, 30)
        self.assertEqual({issue["severity"] for issue in self.issues(code="copy_limits")}, {"unknown"})
        with repository._connect() as conn:
            conn.execute("UPDATE cards SET oracle_text=NULL WHERE id=?", (card,))
        self.assertEqual({issue["severity"] for issue in self.issues(code="copy_limits")}, {"unknown"})

    def test_snapshot_limits_fail_explicitly_instead_of_truncating(self):
        first = self.card(oracle="shared-limit")
        self.card(oracle="shared-limit")
        self.slot(first)
        self.slot(first, board="side")
        with patch.object(analysis, "MAX_ROWS", 1), self.assertRaises(analysis.AnalysisError):
            analysis.deck_analysis("mtg", self.deck)
        with patch.object(analysis, "MAX_PRINTINGS", 1), self.assertRaises(analysis.AnalysisError):
            analysis.build_checklist("mtg", self.deck, match_mode="any")

    def test_reservation_bound_and_invalid_json_shapes(self):
        with self.assertRaises(analysis.AnalysisError):
            analysis.build_checklist("mtg", self.deck, reserve_deck_ids=list(range(2, 23)))
        for raw_json in ('[]', 'null', '{"oracle_id": 42}', '{"oracle_id": ""}'):
            card = self.card(raw_json=raw_json)
            self.slot(card)
        result = analysis.build_checklist("mtg", self.deck, match_mode="any")
        self.assertEqual(len(result["items"]), 4)
        self.assertTrue(all(item["identity_basis"] == "exact_printing" for item in result["items"]))

    def test_html_escapes_names_categories_and_raw_provider_text(self):
        card = self.card(name='<script>synthetic()</script>', type_line='<img src=x onerror="synthetic()">')
        self.slot(card, category='<script>category()</script>')
        response = self.client().get(f"/cards/mtg/decks/{self.deck}/build")
        self.assertIn("&lt;script&gt;synthetic()&lt;/script&gt;", response.text)
        self.assertNotIn("<script>synthetic()", response.text)
        from luigi_web.modules.cards.analysis_routes import templates
        html = templates.env.get_template("cards/partials/deck_analysis.html").render(
            analysis=analysis.deck_analysis("mtg", self.deck))
        self.assertNotIn("<script>category()", html)

    def test_complete_cached_checks_still_expose_rules_and_freshness_unknown(self):
        commander = self.card(oracle="commander", type_line="Legendary Creature - Example",
                               raw={"color_identity": ["U"], "legalities": {"commander": "legal"}})
        land = self.card(type_line="Basic Land - Island", raw={"color_identity": ["U"], "legalities": {"commander": "legal"}})
        self.slot(commander, board="commander")
        self.slot(land, 99)
        advisory = analysis.deck_analysis("mtg", self.deck)["advisory"]
        self.assertEqual(advisory["severity_counts"]["warning"], 0)
        self.assertEqual(advisory["severity_counts"]["unknown"], 1)
        self.assertIsNone(advisory["provider_rules_as_of"])
        self.assertNotIn("legal", advisory)
        self.assertFalse(advisory["writes_blocked"])

    def test_disposable_preview_renders_both_pages_and_closes_its_database(self):
        from fastapi.testclient import TestClient

        previous_path = repository.db_path()
        app = create_preview_app()
        temporary_path = repository.db_path()
        with TestClient(app) as client:
            self.assertEqual(client.get("/").status_code, 200)
            response = client.get("/synthetic-analysis")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Nonland mana curve", response.text)
        self.assertEqual(repository.db_path(), previous_path)
        self.assertFalse(temporary_path.exists())


def create_preview_app():
    """Disposable localhost-only UI fixture: uvicorn --app-dir tests
    test_cards_analysis:create_preview_app --factory --host 127.0.0.1.
    No host application, deployment configuration, or persistent databases.
    """
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles
    from luigi_web import auth, paths
    from luigi_web.core.templating import shell_context
    from luigi_web.modules.cards import analysis_routes

    fixture = CardsAnalysisTests()
    fixture.setUp()
    commander = fixture.card(name="Example Commander", oracle="example-commander",
        type_line="Legendary Creature - Example", raw={"color_identity": ["U", "R"], "colors": ["U"]})
    relic = fixture.card(name="Example Multitype Relic", oracle="example-relic", type_line="Artifact Creature", cmc=2.5,
                         raw={"color_identity": ["U"], "colors": ["U", "R"]})
    alternate = fixture.card(name="Example Alternate Relic", oracle="example-relic", price=95)
    land = fixture.card(name="Example Island", type_line="Basic Land - Island", cmc=0, raw={"color_identity": ["U"], "colors": []})
    variable = fixture.card(name="Example Variable Spell", type_line="Instant", mana_cost="{X}{R}", price=None)
    fixture.slot(commander, board="commander")
    fixture.slot(relic, 3, category="Utility")
    fixture.slot(relic, 2, category="Ramp")
    fixture.slot(land, 92)
    fixture.slot(variable, 2)
    fixture.slot(variable, 4, board="side")
    fixture.slot(relic, 3, board="maybe")
    fixture.own(relic, 2)
    fixture.own(alternate, 2)
    fixture.own(land, 90)
    other = fixture.make_deck()
    fixture.slot(alternate, 3, deck=other)
    with repository._connect() as conn:
        conn.execute("UPDATE decks SET name=? WHERE id=?", ("Synthetic Commander Workshop", fixture.deck))
        conn.execute("UPDATE decks SET name=? WHERE id=?", ("Synthetic Competing Deck", other))

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            fixture.doCleanups()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.dependency_overrides[auth.require_auth] = lambda: None
    app.include_router(analysis_routes.router)
    app.mount("/static", StaticFiles(directory=str(paths.STATIC_DIR)))
    app.mount("/module-assets/cards", StaticFiles(directory=str(paths.PACKAGE_DIR / "modules" / "cards" / "static")))

    @app.get("/")
    def root():
        return RedirectResponse(f"/cards/mtg/decks/{fixture.deck}/build")

    @app.get("/synthetic-analysis", response_class=HTMLResponse)
    def preview_analysis(request: Request):
        template = analysis_routes.templates.env.from_string(
            '{% extends "cards/layout.html" %}{% block cards_content %}'
            '{% include "cards/partials/deck_analysis.html" %}{% endblock %}')
        return HTMLResponse(template.render(request=request, current_game="mtg", games=repository.list_games(),
                            analysis=analysis.deck_analysis("mtg", fixture.deck), **shell_context(request)),
                            headers={"Cache-Control": "no-store"})

    return app


if __name__ == "__main__":
    unittest.main()