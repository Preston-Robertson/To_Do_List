"""Synthetic, offline tests for local deck versions and duplication."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from luigi_web.modules.cards import repository as cards
from luigi_web.modules.cards import versions
from test_cards import _mtg_card


class DeckVersionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = patch.dict(os.environ, {"LUIGI_WEB_CARDS_DB": os.path.join(self.temp_dir.name, "cards.db")})
        self.env.start()
        self.addCleanup(self.env.stop)
        cards.init_db()
        cards.upsert_scryfall_cards([_mtg_card("version-a", "Example Alpha"), _mtg_card("version-b", "Example Beta")])
        first_card = cards.find_card("mtg", "Example Alpha")
        second_card = cards.find_card("mtg", "Example Beta")
        assert first_card is not None and second_card is not None
        self.card_id = first_card["id"]
        self.other_card_id = second_card["id"]
        self.deck_id = cards.create_deck("mtg", "Example Deck", "commander")
        for board in cards.BOARDS:
            cards.add_card_to_deck(self.deck_id, self.card_id, qty=2, board=board, category="Example category")
        cards.add_card_to_deck(self.deck_id, self.card_id, qty=3, category="Second category")
        cards.set_deck_tags(self.deck_id, ["Example tag", "Second tag"])
        cards.save_deck_notes(self.deck_id, "mtg", '<mxfile><diagram id="example" /></mxfile>')
        with cards.transaction() as conn:
            conn.execute(
                "UPDATE decks SET description = ?, partner_card_id = ?, cover_card_id = ?, archived = 1 WHERE id = ?",
                ("Synthetic description", self.other_card_id, self.other_card_id, self.deck_id),
            )

    def capture(self, deck_id: int | None = None) -> tuple[dict, str]:
        with cards._connect() as conn:
            conn.execute("BEGIN")
            return versions._capture(conn, "mtg", deck_id or self.deck_id)


class DeckVersionTests(DeckVersionFixture):
    def test_save_holds_one_write_transaction_for_snapshot(self) -> None:
        capture = versions._capture

        def attempt_concurrent_change(conn, game_code, deck_id):
            with cards._connect() as competing:
                competing.execute("PRAGMA busy_timeout=0")
                with self.assertRaises(sqlite3.OperationalError):
                    competing.execute("UPDATE decks SET name = 'Concurrent synthetic edit' WHERE id = ?", (deck_id,))
            return capture(conn, game_code, deck_id)

        expected = self.capture()[0]
        with patch.object(versions, "_capture", side_effect=attempt_concurrent_change):
            saved = versions.save_version("mtg", self.deck_id, "Consistent snapshot")
        self.assertEqual(saved["snapshot"], expected)

    def test_actual_two_megabyte_bound_and_table_constraint(self) -> None:
        notes = "<mxfile>" + "\u00e9" * 400000 + "</mxfile>"
        cards.save_deck_notes(self.deck_id, "mtg", notes)
        with self.assertRaises(versions.VersionError):
            versions.save_version("mtg", self.deck_id, "Oversized encoded snapshot")
        self.assertEqual(versions.list_versions("mtg", self.deck_id), [])
        with self.assertRaises(sqlite3.IntegrityError), cards.transaction() as conn:
            conn.execute(
                "INSERT INTO deck_versions(deck_id, game_code, label, snapshot_version, snapshot_json, snapshot_hash) VALUES (?, 'mtg', 'Oversized', 1, ?, ?)",
                (self.deck_id, "x" * (versions.MAX_SNAPSHOT_BYTES + 1), "0" * 64),
            )

    def test_duplicate_failure_rolls_back_new_deck(self) -> None:
        with cards.transaction() as conn:
            conn.execute("CREATE TRIGGER fail_clone BEFORE INSERT ON deck_cards BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            versions.duplicate_deck("mtg", self.deck_id)
        with cards._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0], 1)

    def test_save_is_immutable_and_excludes_catalog_collection_prices(self) -> None:
        before, state_hash = self.capture()
        saved = versions.save_version("mtg", self.deck_id, " First version ")
        self.assertEqual(saved["snapshot"], before)
        self.assertEqual(saved["label"], "First version")
        self.assertTrue(saved["created_at"])
        self.assertEqual(self.capture()[1], state_hash)
        cards.add_card_to_deck(self.deck_id, self.other_card_id, qty=4)
        cards.upsert_scryfall_cards([_mtg_card("version-a", "Example Alpha", price="9.99")])
        self.assertEqual(versions.get_version("mtg", self.deck_id, saved["id"])["snapshot"], before)
        encoded = versions._encode(saved["snapshot"])
        for excluded in ("raw_json", "price_", "image_", "collection", "external_id"):
            self.assertNotIn(excluded, encoded)

    def test_duplicate_preserves_slots_tags_notes_and_references(self) -> None:
        before, state_hash = self.capture()
        clone = versions.duplicate_deck("mtg", self.deck_id)
        copied = self.capture(clone["id"])[0]
        self.assertNotEqual(clone["id"], self.deck_id)
        self.assertEqual(clone["name"], "Example Deck (copy)")
        before["deck_id"] = clone["id"]
        before["deck"]["name"] = clone["name"]
        self.assertEqual(copied, before)
        self.assertEqual(self.capture()[1], state_hash)
        self.assertEqual(versions.duplicate_deck("mtg", self.deck_id, "Named copy")["name"], "Named copy")

    def test_label_bounds_and_schema_version_unchanged(self) -> None:
        for label in ("", "   ", "x" * 81):
            with self.subTest(label_length=len(label)), self.assertRaises(versions.VersionError):
                versions.save_version("mtg", self.deck_id, label)
        versions.save_version("mtg", self.deck_id, "x" * 80)
        versions.ensure_schema()
        with cards._connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], cards.SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM deck_versions").fetchone()[0], 1)

    def test_scope_and_invalid_notes_are_rejected(self) -> None:
        saved = versions.save_version("mtg", self.deck_id, "Example")
        other_deck = cards.create_deck("mtg", "Other deck")
        for game_code, deck_id in (("pokemon", self.deck_id), ("mtg", other_deck)):
            with self.subTest(game_code=game_code, deck_id=deck_id), self.assertRaises(versions.VersionNotFound):
                versions.get_version(game_code, deck_id, saved["id"])
        with self.assertRaises(versions.VersionNotFound):
            versions.duplicate_deck("pokemon", self.deck_id)
        with cards.transaction() as conn:
            conn.execute("UPDATE decks SET notes_xml = ? WHERE id = ?", ("<mxfile><script /></mxfile>", self.deck_id))
        with self.assertRaises(versions.VersionError):
            versions.save_version("mtg", self.deck_id, "Invalid")

    def restore(self, saved: dict, preview: dict) -> dict:
        return versions.restore_version(
            "mtg", self.deck_id, saved["id"], confirm=True,
            expected_current_hash=preview["expected_current_hash"],
            expected_version_hash=preview["expected_version_hash"],
        )

    def test_restore_requires_preview_and_confirmation_and_creates_recoverable_backup(self) -> None:
        original = self.capture()[0]
        saved = versions.save_version("mtg", self.deck_id, "Original")
        cards.add_card_to_deck(self.deck_id, self.other_card_id, qty=7, board="side")
        cards.set_deck_tags(self.deck_id, ["Changed tag"])
        changed, changed_hash = self.capture()
        preview = versions.preview_restore("mtg", self.deck_id, saved["id"])
        self.assertEqual(self.capture()[1], changed_hash)
        self.assertEqual(len(versions.list_versions("mtg", self.deck_id)), 1)
        for current_hash, version_hash, confirm in (("", "", True), (preview["expected_current_hash"], preview["expected_version_hash"], False)):
            with self.assertRaises(versions.VersionError):
                versions.restore_version(
                    "mtg", self.deck_id, saved["id"],
                    expected_current_hash=current_hash,
                    expected_version_hash=version_hash,
                    confirm=confirm,
                )
        result = self.restore(saved, preview)
        self.assertEqual(self.capture()[0], original)
        backup = versions.get_version("mtg", self.deck_id, result["backup_version_id"])
        self.assertEqual(backup["label"], "Before restore")
        self.assertEqual(backup["snapshot"], changed)
        self.restore(backup, versions.preview_restore("mtg", self.deck_id, backup["id"]))
        self.assertEqual(self.capture()[0], changed)

    def test_stale_guards_cover_row_slots_and_tags_without_timestamp_change(self) -> None:
        saved = versions.save_version("mtg", self.deck_id, "Original")
        mutations = (
            ("UPDATE decks SET description = ? WHERE id = ?", ("Later description", self.deck_id)),
            ("UPDATE deck_cards SET qty = qty + 1 WHERE deck_id = ?", (self.deck_id,)),
            ("DELETE FROM deck_tags WHERE deck_id = ?", (self.deck_id,)),
            ("UPDATE decks SET partner_card_id = NULL WHERE id = ?", (self.deck_id,)),
        )
        for sql, values in mutations:
            with self.subTest(sql=sql):
                preview = versions.preview_restore("mtg", self.deck_id, saved["id"])
                with cards.transaction() as conn:
                    conn.execute(sql, values)
                state = self.capture()
                with self.assertRaises(versions.VersionConflict):
                    self.restore(saved, preview)
                self.assertEqual(self.capture(), state)
                self.assertEqual(len(versions.list_versions("mtg", self.deck_id)), 1)

    def test_snapshot_stale_guard_and_invalid_snapshot(self) -> None:
        saved = versions.save_version("mtg", self.deck_id, "Original")
        preview = versions.preview_restore("mtg", self.deck_id, saved["id"])
        changed = saved["snapshot"]
        changed["deck"]["name"] = "Replaced snapshot"
        with cards.transaction() as conn:
            conn.execute("UPDATE deck_versions SET snapshot_json = ?, snapshot_hash = ? WHERE id = ?", (versions._encode(changed), versions._hash(changed), saved["id"]))
        with self.assertRaises(versions.VersionConflict):
            self.restore(saved, preview)
        with cards.transaction() as conn:
            conn.execute("UPDATE deck_versions SET snapshot_json = ? WHERE id = ?", ('{"schema_version":1}', saved["id"]))
        with self.assertRaises(versions.VersionError):
            versions.preview_restore("mtg", self.deck_id, saved["id"])

    def test_limit_includes_automatic_backup_and_refuses_without_modifying_deck(self) -> None:
        saved = versions.save_version("mtg", self.deck_id, "Original")
        preview = versions.preview_restore("mtg", self.deck_id, saved["id"])
        with cards.transaction() as conn:
            for index in range(99):
                versions._insert_version(conn, saved["snapshot"], f"Version {index}")
        state = self.capture()
        with self.assertRaises(versions.VersionConflict):
            versions.save_version("mtg", self.deck_id, "Over limit")
        with self.assertRaises(versions.VersionConflict):
            self.restore(saved, preview)
        with self.assertRaises(versions.VersionConflict):
            versions.preview_restore("mtg", self.deck_id, saved["id"])
        self.assertEqual(len(versions.list_versions("mtg", self.deck_id)), 100)
        self.assertEqual(self.capture(), state)
        self.assertTrue(versions.duplicate_deck("mtg", self.deck_id)["id"])

    def test_oversized_snapshot_rolls_back(self) -> None:
        with cards.transaction() as conn:
            conn.execute("UPDATE decks SET notes_xml = ? WHERE id = ?", ("<mxfile>" + "&amp;" * 190000 + "</mxfile>", self.deck_id))
        with patch.object(versions, "MAX_SNAPSHOT_BYTES", 500):
            with self.assertRaises(versions.VersionError):
                versions.save_version("mtg", self.deck_id, "Too large")
        self.assertEqual(versions.list_versions("mtg", self.deck_id), [])

    def test_catalog_game_mismatch_and_missing_cards_block_restore(self) -> None:
        saved = versions.save_version("mtg", self.deck_id, "Original")
        preview = versions.preview_restore("mtg", self.deck_id, saved["id"])
        with cards.transaction() as conn:
            conn.execute("UPDATE cards SET game_code = 'pokemon' WHERE id = ?", (self.other_card_id,))
        with self.assertRaises(versions.VersionConflict):
            self.restore(saved, preview)
        with self.assertRaises(versions.VersionConflict):
            versions.duplicate_deck("mtg", self.deck_id)
        with cards.transaction() as conn:
            conn.execute("DELETE FROM cards WHERE id = ?", (self.other_card_id,))
        with self.assertRaises(versions.VersionConflict):
            versions.preview_restore("mtg", self.deck_id, saved["id"])

    def test_restore_rollback_keeps_other_deck_collection_and_versions_unchanged(self) -> None:
        saved = versions.save_version("mtg", self.deck_id, "Original")
        clone = versions.duplicate_deck("mtg", self.deck_id)
        cards.add_to_collection(self.card_id, qty=12)
        cards.add_card_to_deck(self.deck_id, self.other_card_id)
        preview = versions.preview_restore("mtg", self.deck_id, saved["id"])
        before = self.capture()
        other = self.capture(clone["id"])
        with cards.transaction() as conn:
            conn.execute(
                f"CREATE TRIGGER fail_version_restore BEFORE INSERT ON deck_cards "
                f"WHEN NEW.deck_id = {self.deck_id} BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.restore(saved, preview)
        self.assertEqual(self.capture(), before)
        self.assertEqual(self.capture(clone["id"]), other)
        self.assertEqual(len(versions.list_versions("mtg", self.deck_id)), 1)
        with cards._connect() as conn:
            self.assertEqual(conn.execute("SELECT qty FROM collection WHERE card_id = ?", (self.card_id,)).fetchone()[0], 12)
        with cards.transaction() as conn:
            conn.execute("DROP TRIGGER fail_version_restore")
        self.restore(saved, preview)
        self.assertEqual(self.capture(clone["id"]), other)

    def test_diff_saved_to_current_and_two_saved_is_board_aware(self) -> None:
        first = versions.save_version("mtg", self.deck_id, "First")
        with cards.transaction() as conn:
            conn.execute("UPDATE deck_cards SET qty = 9 WHERE deck_id = ? AND board = 'side'", (self.deck_id,))
            conn.execute("UPDATE deck_cards SET board = 'main', category = 'Moved category' WHERE deck_id = ? AND board = 'maybe'", (self.deck_id,))
            conn.execute("UPDATE decks SET name = 'New example', notes_xml = NULL WHERE id = ?", (self.deck_id,))
        cards.set_deck_tags(self.deck_id, ["New tag"])
        cards.add_card_to_deck(self.deck_id, self.other_card_id, qty=4)
        second = versions.save_version("mtg", self.deck_id, "Second")
        current = versions.version_page("mtg", self.deck_id, first["id"])["comparison"]
        saved = versions.version_page("mtg", self.deck_id, first["id"], second["id"])["comparison"]
        self.assertEqual(current, saved)
        self.assertEqual({row["kind"] for row in saved["slots"]}, {"moved", "quantity", "added"})
        self.assertEqual(saved["before_totals"], {"commander": 2, "main": 5, "side": 2, "maybe": 2, "deck": 7, "all": 11})
        self.assertEqual(saved["after_totals"]["deck"], 13)
        self.assertEqual(saved["after_totals"]["side"], 9)
        self.assertEqual({row["field"] for row in saved["metadata"]}, {"name", "notes_xml"})
        self.assertEqual(saved["tags_added"][0]["name"], "New tag")
        self.assertEqual(len(saved["tags_removed"]), 2)
        reverse = versions.version_page("mtg", self.deck_id, second["id"], first["id"])["comparison"]
        self.assertIn("removed", {row["kind"] for row in reverse["slots"]})

    def test_changed_shared_tag_never_mutates_another_deck(self) -> None:
        saved = versions.save_version("mtg", self.deck_id, "Original")
        clone = versions.duplicate_deck("mtg", self.deck_id)
        with cards.transaction() as conn:
            conn.execute("UPDATE tags SET color = '#123456'")
        state = self.capture(clone["id"])
        with self.assertRaises(versions.VersionConflict):
            versions.preview_restore("mtg", self.deck_id, saved["id"])
        self.assertEqual(self.capture(clone["id"]), state)


class DeckVersionRouteTests(DeckVersionFixture):
    def setUp(self) -> None:
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from luigi_web import application, auth
        from luigi_web.modules.cards import version_routes

        self.auth = auth
        token = patch.dict(os.environ, {"LUIGI_WEB_UI_TOKEN": "synthetic-version-token"})
        token.start()
        self.addCleanup(token.stop)
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.app.middleware("http")(application.csrf_middleware)
        self.app.include_router(version_routes.router)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.cookies.set(auth.COOKIE_NAME, "synthetic-version-token")
        self.client.cookies.set(auth.CSRF_COOKIE_NAME, "synthetic-version-csrf")
        self.headers = {"X-CSRF-Token": "synthetic-version-csrf"}
        self.url = f"/cards/mtg/decks/{self.deck_id}/versions"

    def test_auth_csrf_and_bearer_requests(self) -> None:
        self.client.cookies.clear()
        for method, url in (("get", self.url), ("post", self.url), ("get", self.url + "/1/preview"), ("post", self.url + "/1/restore"), ("post", self.url.removesuffix("/versions") + "/duplicate")):
            response = getattr(self.client, method)(url)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["cache-control"], "no-store")
        response = self.client.get(self.url, headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")
        self.client.cookies.set(self.auth.COOKIE_NAME, "synthetic-version-token")
        response = self.client.post(self.url, data={"label": "Rejected"})
        self.assertEqual(response.status_code, 403)
        self.client.cookies.clear()
        response = self.client.post(self.url, json={"label": "Bearer save"}, headers={"Authorization": "Bearer synthetic-version-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(versions.list_versions("mtg", self.deck_id)), 1)

    def test_html_json_htmx_and_redirect_patterns(self) -> None:
        empty = self.client.get(self.url)
        self.assertEqual(empty.status_code, 200)
        self.assertIn("No saved versions.", empty.text)
        self.assertIn("/module-assets/cards/versions.css", empty.text)
        saved = self.client.post(self.url, data={"label": "Form save"}, headers=self.headers, follow_redirects=False)
        self.assertEqual(saved.status_code, 303)
        self.assertIn("?before=", saved.headers["location"])
        htmx = self.client.post(self.url, data={"label": "HTMX save"}, headers=self.headers | {"HX-Request": "true"})
        self.assertEqual(htmx.status_code, 204)
        self.assertIn("?before=", htmx.headers["hx-redirect"])
        page = self.client.get(self.url)
        self.assertIn("Saved versions", page.text)
        self.assertIn("Before", page.text)
        payload = self.client.get(self.url, headers={"Accept": "application/json"})
        self.assertEqual(len(payload.json()["versions"]), 2)
        for response in (empty, saved, htmx, page, payload):
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        clone = self.client.post(self.url.removesuffix("/versions") + "/duplicate", json={"name": "Example clone"}, headers=self.headers)
        self.assertEqual(clone.status_code, 200)
        copied = cards.get_deck(clone.json()["deck_id"], "mtg")
        assert copied is not None
        self.assertEqual(copied["name"], "Example clone")
        unnamed = self.client.post(self.url.removesuffix("/versions") + "/duplicate", headers=self.headers, follow_redirects=False)
        self.assertEqual(unnamed.status_code, 303)

    def test_preview_restore_confirmation_and_stale_http(self) -> None:
        saved = self.client.post(self.url, json={"label": "Original"}, headers=self.headers).json()
        version_url = self.url + f"/{saved['version_id']}"
        cards.add_card_to_deck(self.deck_id, self.other_card_id, qty=4)
        before = self.capture()
        html = self.client.get(version_url + "/preview")
        self.assertEqual(html.status_code, 200)
        self.assertIn('name="confirm"', html.text)
        self.assertIn('name="expected_current_hash"', html.text)
        self.assertEqual(self.capture(), before)
        response = self.client.get(version_url + "/restore")
        self.assertEqual(response.status_code, 405)
        preview = self.client.get(version_url + "/preview", headers={"Accept": "application/json"}).json()
        data = {key: preview[key] for key in ("expected_current_hash", "expected_version_hash")}
        rejected = self.client.post(version_url + "/restore", json=data, headers=self.headers)
        self.assertEqual(rejected.status_code, 422)
        restored = self.client.post(version_url + "/restore", json=data | {"confirm": True, "snapshot": {"deck": {"name": "Forged"}}}, headers=self.headers)
        self.assertEqual(restored.status_code, 200)
        self.assertIn("backup_version_id", restored.json())
        restored_deck = cards.get_deck(self.deck_id, "mtg")
        assert restored_deck is not None
        self.assertEqual(restored_deck["name"], "Example Deck")
        stale = self.client.post(version_url + "/restore", json=data | {"confirm": True}, headers=self.headers)
        self.assertEqual(stale.status_code, 409)

    def test_cross_game_cross_deck_and_untrusted_markup(self) -> None:
        saved = versions.save_version("mtg", self.deck_id, '<script>alert("example")</script>')
        other = cards.create_deck("mtg", "Unrelated synthetic deck")
        for path in (
            self.url.replace("/mtg/", "/pokemon/"),
            self.url.replace(f"/{self.deck_id}/", f"/{other}/") + f"?before={saved['id']}",
            self.url.replace(f"/{self.deck_id}/", f"/{other}/") + f"/{saved['id']}/preview",
            self.url + f"?after={saved['id'] + 1000}",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("Example Deck", response.text)
        html = self.client.get(self.url)
        self.assertNotIn('<script>alert("example")</script>', html.text)
        self.assertIn("&lt;script&gt;", html.text)
        self.assertNotIn("<mxfile>", html.text)

    def test_generic_database_errors_and_bounded_requests(self) -> None:
        with patch.object(versions, "save_version", side_effect=sqlite3.OperationalError("SELECT private synthetic column at private/path")):
            response = self.client.post(self.url, json={"label": "Example"}, headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SELECT", response.text)
        self.assertNotIn("private/path", response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        for payload in ({"label": "x" * 20000}, {"label": {"invalid": "shape"}}, ["invalid"]):
            response = self.client.post(self.url, json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 422)
        for query in ("?before=-1", "?after=bad", "?after=99999999999999999999999999"):
            self.assertEqual(self.client.get(self.url + query).status_code, 422)


def synthetic_versions_app():
    from contextlib import asynccontextmanager
    from pathlib import Path
    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles

    fixture = DeckVersionRouteTests()
    fixture.setUp()
    saved = versions.save_version("mtg", fixture.deck_id, "Example baseline")
    cards.add_card_to_deck(fixture.deck_id, fixture.other_card_id, qty=4, board="side", category="Example answers")
    cards.set_deck_tags(fixture.deck_id, ["Updated example tag"])
    versions.save_version("mtg", fixture.deck_id, "Example revised version")
    cards.add_card_to_deck(fixture.deck_id, fixture.card_id, qty=9, board="main", category="Example category")
    root = Path(__file__).resolve().parents[1]
    fixture.app.mount("/static", StaticFiles(directory=root / "luigi_web/core/static"))
    fixture.app.mount("/module-assets/cards", StaticFiles(directory=root / "luigi_web/modules/cards/static"))

    @fixture.app.get("/__fixture__")
    def enter_fixture():
        response = RedirectResponse(fixture.url + f"?before={saved['id']}", status_code=303)
        response.set_cookie(fixture.auth.COOKIE_NAME, "synthetic-version-token", httponly=True, samesite="strict")
        response.set_cookie(fixture.auth.CSRF_COOKIE_NAME, "synthetic-version-csrf", samesite="strict")
        return response

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            fixture.doCleanups()

    fixture.app.router.lifespan_context = lifespan
    return fixture.app


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        import uvicorn

        uvicorn.run(synthetic_versions_app(), host="127.0.0.1", port=int(sys.argv[2]) if len(sys.argv) > 2 else 8897, access_log=False)
    else:
        unittest.main()