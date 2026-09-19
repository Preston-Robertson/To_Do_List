from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from luigi_web.modules.media import history


NOW = datetime.fromisoformat("2026-09-19T12:30:00-04:00")


def media_item(**changes):
    item = {
        "profile": "Example profile",
        "title": "Example title",
        "status": "completed",
        "date_started": "2024-01-02",
        "date_completed": "2024-02-03",
        "hours_played": "12.5",
        "current_episode": 6,
        "current_season": 1,
        "priority": 3,
        "rating": None,
        "notes": "",
        "tags": [],
        "version": "synthetic-version",
        "key": "ignored-service-key",
    }
    item.update(changes)
    return item


class MediaHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "media.sqlite3"
        environment = patch.dict(os.environ, {"LUIGI_WEB_MEDIA_DB": str(self.database)})
        environment.start()
        self.addCleanup(environment.stop)
        current_time = patch.object(history.clock, "local_now", return_value=NOW)
        current_time.start()
        self.addCleanup(current_time.stop)

    def detail(self, section="games", item=None):
        item = item or media_item()
        return history.detail(section, item["profile"], item["title"])

    def test_identity_is_unambiguous_casefolded_and_lazy(self):
        key = history.identity_key(" Games ", " Example PROFILE ", " Example TITLE ")
        self.assertEqual(key, history.identity_key("games", "example profile", "example title"))
        self.assertEqual(len(key), 64)
        self.assertNotEqual(key, history.identity_key("shows", "example profile", "example title"))
        self.assertNotEqual(history.identity_key("games", "ab", "c"), history.identity_key("games", "a", "bc"))
        self.assertFalse(self.database.exists())

    def test_replay_completion_and_exact_undo(self):
        original = media_item()
        rated = media_item(rating=7)
        history.record_change("games", original, rated)
        original_runs = self.detail()["runs"]
        self.assertEqual(len(original_runs), 1)
        self.assertEqual(original_runs[0]["origin"], "legacy_snapshot")
        self.assertEqual(original_runs[0]["started_at"], "2024-01-02")
        self.assertEqual(original_runs[0]["completed_at"], "2024-02-03")

        operation = history.prepare_change("games", rated, "replay", str(uuid4()))
        self.assertEqual(self.detail()["pending_count"], 1)
        self.assertEqual(self.detail()["runs"], original_runs)
        replayed = media_item(rating=7, status="playing", date_started="", date_completed="")
        replay_event = history.finish_change(operation, replayed)
        replay_runs = self.detail()["runs"]
        self.assertEqual([run["number"] for run in replay_runs], [2, 1])
        self.assertEqual(replay_runs[0]["started_at"], NOW.isoformat())
        self.assertEqual(replay_runs[0]["origin"], "web")

        completed = dict(replayed, status="completed", date_completed="2026-09-19")
        complete_event = history.record_change("games", replayed, completed)
        self.assertEqual(self.detail()["runs"][0]["completed_at"], NOW.isoformat())
        history.undo_change("games", complete_event, completed, replayed)
        self.assertEqual(self.detail()["runs"], replay_runs)
        with self.assertRaises(history.HistoryConflict):
            history.undo_change("games", replay_event, replayed, rated)

    def test_replay_undo_restores_ids_and_idempotence(self):
        before = media_item()
        history.record_change("games", before, media_item(rating=1))
        original_runs = self.detail()["runs"]
        before = media_item(rating=1)
        item = dict(before, status="playing", date_completed="")
        operation = str(uuid4())
        history.prepare_change("games", before, "replay", operation)
        event_id = history.finish_change(operation, item)
        self.assertEqual(history.finish_change(operation, item), event_id)
        self.assertEqual(history.record_change("games", before, item, "replay", operation), event_id)
        self.assertEqual(len(self.detail()["runs"]), 2)
        undo_id = history.undo_change("games", event_id, item, before)
        self.assertIsInstance(undo_id, int)
        self.assertEqual(self.detail()["runs"], original_runs)
        self.assertEqual(self.detail()["activity"][0]["kind"], "undo")

    def test_failed_and_uncertain_writes_are_not_confirmed(self):
        before = media_item()
        operation = history.prepare_change("games", before, "replay", str(uuid4()))
        history.fail_change(operation, uncertain=True)
        detail = self.detail()
        self.assertEqual(detail["runs"], [])
        self.assertEqual(detail["activity"], [])
        self.assertEqual(detail["pending_count"], 1)
        with self.assertRaises(history.HistoryConflict):
            history.prepare_change("games", before, "update", str(uuid4()))
        history.finish_change(operation, dict(before, status="playing"))
        self.assertEqual(self.detail()["pending_count"], 0)
        operation = history.prepare_change("games", before, "update", str(uuid4()))
        history.fail_change(operation, uncertain=False)
        self.assertEqual(len(self.detail()["activity"]), 1)
        self.assertEqual(self.detail()["pending_count"], 1)

    def test_import_and_identity_do_not_initialize_storage(self):
        spec = importlib.util.spec_from_file_location("synthetic_media_history", history.__file__)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.object(sqlite3, "connect", side_effect=AssertionError("Unexpected database access")):
            with patch.object(Path, "mkdir", side_effect=AssertionError("Unexpected directory creation")):
                spec.loader.exec_module(module)
                module.identity_key("games", "Example profile", "Example title")
        self.assertFalse(self.database.exists())

    def test_default_path_and_environment_override_are_dynamic(self):
        default_root = self.database.parent / "default"
        with patch.object(history, "DATA_DIR", default_root):
            with patch.dict(os.environ, {"LUIGI_WEB_MEDIA_DB": ""}):
                history.detail("games", "Example profile", "Example title")
                self.assertTrue((default_root / "media.sqlite3").is_file())
        self.assertFalse(self.database.exists())
        self.detail()
        self.assertTrue(self.database.is_file())

    def test_prepare_fails_closed_without_exposing_storage_details(self):
        with patch.dict(os.environ, {"LUIGI_WEB_MEDIA_DB": str(self.database.parent)}):
            with self.assertRaises(history.HistoryUnavailable) as error:
                history.prepare_change("games", media_item(), "replay", str(uuid4()))
        self.assertEqual(str(error.exception), "Media history storage is unavailable.")
        self.assertFalse(self.database.exists())

    def test_rejects_an_existing_unrelated_database(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE synthetic_other_domain (id INTEGER PRIMARY KEY)")
        with self.assertRaises(history.HistoryUnavailable):
            history.prepare_change("games", media_item(), "update", str(uuid4()))
        with closing(sqlite3.connect(self.database)) as connection:
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            self.assertEqual(tables, [("synthetic_other_domain",)])
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], 0)

    def test_expected_column_is_never_added_to_foreign_databases(self):
        for application_id in (0, 12345):
            with self.subTest(application_id=application_id):
                foreign_database = self.database.with_name(f"synthetic-foreign-{application_id}.sqlite3")
                with closing(sqlite3.connect(foreign_database)) as connection:
                    connection.execute(f"PRAGMA application_id = {application_id}")
                    connection.execute("CREATE TABLE media_operations (operation_id TEXT)")
                    original_schema = connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
                    connection.commit()
                with patch.dict(os.environ, {"LUIGI_WEB_MEDIA_DB": str(foreign_database)}):
                    with self.assertRaises(history.HistoryUnavailable):
                        history.prepare_change("games", media_item(), expected_fields={"rating": 4})
                    with self.assertRaises(history.HistoryUnavailable):
                        history.reconcile("games", media_item(rating=4))
                with closing(sqlite3.connect(foreign_database)) as connection:
                    self.assertEqual(connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall(),
                                     original_schema)
                    self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], application_id)

    def test_journal_is_durable_before_any_confirmation(self):
        operation = history.prepare_change("games", media_item(), "replay", str(uuid4()))
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute("SELECT state, before_json, before_runs_json FROM media_operations WHERE operation_id = ?",
                                     (operation,)).fetchone()
        self.assertEqual(row[0], "pending")
        self.assertEqual(json.loads(row[1])["date_completed"], "2024-02-03")
        self.assertEqual(json.loads(row[2]), [])
        self.assertEqual(self.detail()["activity"], [])

    def test_commit_failure_rolls_back_runs_events_and_confirmation(self):
        before = media_item()
        after = dict(before, status="playing", date_completed="")
        operation = history.prepare_change("games", before, "replay", str(uuid4()))
        connect = sqlite3.connect

        class FailingCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("Synthetic failure details must remain private")

        with patch.object(sqlite3, "connect", side_effect=lambda *args, **kwargs: connect(*args, **kwargs, factory=FailingCommit)):
            with self.assertRaises(history.HistoryUnavailable) as error:
                history.finish_change(operation, after)
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(self.detail(), {"activity": [], "runs": [], "pending_count": 1})
        history.fail_change(operation, uncertain=True)
        event_id = history.finish_change(operation, after)
        self.assertEqual(history.finish_change(operation, after), event_id)
        self.assertEqual(len(self.detail()["runs"]), 2)

    def test_expected_fields_are_normalized_and_durable(self):
        expected = {"status": " Playing ", "hours_played": 14.5, "current_episode": 7,
                    "tags": [" Example tag "], "platform": " Example platform ",
                    "genre": " Example genre ", "total_episodes": 12, "notes": "x" * 4096,
                    "date_started": "2026-09-19", "date_completed": ""}
        operation = history.prepare_change("games", media_item(), expected_fields=expected)
        with closing(sqlite3.connect(self.database)) as connection:
            stored = json.loads(connection.execute(
                "SELECT expected_json FROM media_operations WHERE operation_id = ?", (operation,)
            ).fetchone()[0])
        self.assertEqual(stored, {**expected, "status": "playing", "hours_played": "14.5",
                                  "tags": ["Example tag"], "platform": "Example platform",
                                  "genre": "Example genre"})
        self.assertEqual(self.detail(), {"activity": [], "runs": [], "pending_count": 1})

    def test_expected_fields_are_part_of_operation_idempotency(self):
        before = media_item()
        operation = history.prepare_change("games", before, expected_fields={"hours_played": 14.5})
        self.assertEqual(history.prepare_change("games", before, operation_id=operation,
                                               expected_fields={"hours_played": "14.50"}), operation)
        for expected in (None, {}, {"hours_played": 15}, {"hours_played": 14.5, "rating": None}):
            with self.subTest(expected=expected):
                with self.assertRaises(history.HistoryConflict):
                    history.prepare_change("games", before, operation_id=operation, expected_fields=expected)

    def test_malformed_expected_fields_are_rejected_before_storage(self):
        for expected in ([], "status", {"credentials": "synthetic-ignore-marker"}, {"key": "ignored"},
                         {"version": "ignored"}, {"Status": "playing"}, {"unknown": 1},
                         {"title": "Different title"}, {"profile": "Different profile"},
                         {"status": "unknown"}, {"tags": "not-a-list"}, {"tags": [1]},
                         {"tags": {}}, {"tags": ""}, {"tags": False}, {"tags": 0},
                         {"rating": True}, {"current_episode": 1.5}, {"total_episodes": -1},
                         {"hours_played": float("nan")}, {"platform": []}, {"genre": 7},
                         {"notes": "x" * 4097}, {"date_started": []}):
            with self.subTest(expected=expected):
                with self.assertRaises(ValueError):
                    history.prepare_change("games", media_item(), expected_fields=expected)
        self.assertFalse(self.database.exists())

    def test_owned_database_additively_upgrades_expected_column(self):
        before = media_item()
        operation = history.prepare_change("games", before)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("ALTER TABLE media_operations DROP COLUMN expected_json")
            snapshot = history._item("games", before)
            for field in ("platform", "genre", "total_episodes"):
                snapshot.pop(field)
            connection.execute("UPDATE media_operations SET before_json = ? WHERE operation_id = ?",
                               (json.dumps(snapshot), operation))
            connection.commit()
        self.assertEqual(history.prepare_change("games", before, operation_id=operation), operation)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT expected_json FROM media_operations").fetchall(), [(None,)])
            self.assertEqual(json.loads(connection.execute("SELECT before_json FROM media_operations").fetchone()[0]),
                             snapshot)
        self.assertEqual(self.detail(), {"activity": [], "runs": [], "pending_count": 1})
        self.assertTrue(history.reconcile("games", before)["resolved"])
        self.assertEqual(self.detail(), {"activity": [], "runs": [], "pending_count": 1})
        history.prepare_change("games", before, expected_fields={"rating": 4})

    def test_lost_commit_response_does_not_demote_a_confirmed_operation(self):
        before = media_item()
        after = dict(before, rating=4)
        operation = history.prepare_change("games", before)
        connect = sqlite3.connect

        class LostResponse(sqlite3.Connection):
            def commit(self):
                super().commit()
                raise sqlite3.OperationalError("Synthetic lost response")

        with patch.object(sqlite3, "connect", side_effect=lambda *args, **kwargs: connect(*args, **kwargs, factory=LostResponse)):
            with self.assertRaises(history.HistoryUnavailable):
                history.finish_change(operation, after)
        history.fail_change(operation, uncertain=True)
        event_id = history.finish_change(operation, after)
        self.assertEqual(self.detail()["activity"][0]["id"], event_id)
        self.assertEqual(self.detail()["pending_count"], 0)

    def test_pending_after_write_reconciles_once(self):
        before = media_item(status="backlog", date_started="", date_completed="")
        expected = {"status": "playing", "date_started": "2026-09-18", "hours_played": 14.5}
        operation = history.prepare_change("games", before, expected_fields=expected)
        after = dict(before, **expected, notes="An unrelated current value", version="new-version")
        self.assertEqual(history.reconcile("games", after),
                         {"resolved": True, "pending_count": 0, "warning": None})
        detail = self.detail()
        self.assertEqual(len(detail["activity"]), 1)
        self.assertEqual(detail["activity"][0]["kind"], "update")
        self.assertEqual(detail["activity"][0]["occurred_at"], NOW.isoformat())
        self.assertEqual(len(detail["runs"]), 1)
        self.assertEqual(detail["runs"][0]["started_at"], NOW.isoformat())
        self.assertEqual(history.finish_change(operation, after), detail["activity"][0]["id"])
        self.assertEqual(history.reconcile("games", after),
                         {"resolved": False, "pending_count": 0, "warning": None})
        self.assertEqual(self.detail(), detail)

    def test_uncertain_replay_retains_kind_and_legacy_dates(self):
        before = media_item()
        expected = {"status": "playing", "date_started": "2026-09-18", "date_completed": ""}
        operation = history.prepare_change("games", before, "replay", expected_fields=expected)
        history.fail_change(operation, uncertain=True)
        after = dict(before, **expected)
        self.assertEqual(history.reconcile("games", after),
                         {"resolved": True, "pending_count": 0, "warning": None})
        detail = self.detail()
        self.assertEqual(len(detail["activity"]), 1)
        self.assertEqual(detail["activity"][0]["kind"], "replay")
        self.assertEqual([run["origin"] for run in detail["runs"]], ["web", "legacy_snapshot"])
        self.assertEqual(detail["runs"][0]["started_at"], NOW.isoformat())
        self.assertEqual(detail["runs"][1]["completed_at"], before["date_completed"])
        history.reconcile("games", after)
        self.assertEqual(self.detail(), detail)

    def test_recovered_completion_uses_confirmation_time_and_original_start(self):
        before = media_item(status="watching", date_completed="")
        expected = {"status": "completed", "date_completed": "2026-09-18"}
        operation = history.prepare_change("shows", before, expected_fields=expected)
        history.fail_change(operation, uncertain=True)
        self.assertTrue(history.reconcile("shows", dict(before, **expected))["resolved"])
        detail = self.detail("shows")
        self.assertEqual(len(detail["activity"]), 1)
        self.assertEqual(len(detail["runs"]), 1)
        self.assertEqual(detail["runs"][0]["started_at"], before["date_started"])
        self.assertEqual(detail["runs"][0]["completed_at"], NOW.isoformat())
        self.assertEqual(detail["activity"][0]["changes"]["date_completed"]["after"], "2026-09-18")

    def test_unchanged_intended_fields_release_without_activity(self):
        before = media_item()
        operation = history.prepare_change("games", before, expected_fields={"rating": 5})
        result = history.reconcile("games", dict(before, notes="Unrelated current note"))
        self.assertEqual(result, {"resolved": True, "pending_count": 1,
                                  "warning": "A recorded write was not confirmed."})
        self.assertEqual(self.detail(), {"activity": [], "runs": [], "pending_count": 1})
        with self.assertRaises(history.HistoryConflict):
            history.finish_change(operation, dict(before, rating=5))
        history.prepare_change("games", before, expected_fields={"rating": 6})
        history.prepare_change("games", dict(before, title="Other title"), expected_fields={"rating": 6})

    def test_partial_match_releases_without_changing_runs_or_snapshot(self):
        before = media_item()
        rated = dict(before, rating=3)
        history.record_change("games", before, rated)
        original = self.detail()
        expected = {"status": "playing", "date_started": "2026-09-18", "date_completed": ""}
        operation = history.prepare_change("games", rated, "replay", expected_fields=expected)
        history.fail_change(operation, uncertain=True)
        with closing(sqlite3.connect(self.database)) as connection:
            snapshot = connection.execute("SELECT before_runs_json FROM media_operations WHERE operation_id = ?",
                                          (operation,)).fetchone()[0]
        partial = dict(rated, status="playing")
        result = history.reconcile("games", partial)
        self.assertEqual(result, {"resolved": True, "pending_count": 1,
                                  "warning": "History could not reconstruct an interrupted change"})
        self.assertEqual(self.detail(), {**original, "pending_count": 1})
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute(
                "SELECT state, before_runs_json FROM media_operations WHERE operation_id = ?", (operation,)
            ).fetchone(), ("unconfirmed", snapshot))
        repeated = history.reconcile("games", dict(rated, **expected))
        self.assertFalse(repeated["resolved"])
        self.assertEqual(repeated["pending_count"], 1)
        self.assertIsNotNone(repeated["warning"])
        self.assertEqual(self.detail(), {**original, "pending_count": 1})
        history.prepare_change("games", partial, expected_fields={"rating": 4})

    def test_missing_intent_blocks_unless_full_normalized_item_is_unchanged(self):
        before = media_item()
        operation = history.prepare_change("games", before)
        history.fail_change(operation, uncertain=True)
        changed = dict(before, platform="Example platform")
        result = history.reconcile("games", changed)
        self.assertFalse(result["resolved"])
        self.assertEqual(result["pending_count"], 1)
        self.assertIsNotNone(result["warning"])
        with self.assertRaises(history.HistoryConflict):
            history.prepare_change("games", changed, expected_fields={"rating": 1})
        self.assertTrue(history.reconcile("games", dict(before, version="ignored-new-version"))["resolved"])
        self.assertEqual(self.detail(), {"activity": [], "runs": [], "pending_count": 1})
        history.prepare_change("games", before, expected_fields={"rating": 1})

    def test_noop_intent_never_confirms_or_creates_runs(self):
        before = media_item()
        for expected in ({}, {"status": "completed"}, {"hours_played": 12.5}):
            with self.subTest(expected=expected):
                history.prepare_change("games", before, expected_fields=expected)
                self.assertTrue(history.reconcile("games", before)["resolved"])
        self.assertEqual(self.detail(), {"activity": [], "runs": [], "pending_count": 3})

    def test_reconcile_only_inspects_the_requested_identity(self):
        before = media_item()
        after = dict(before, rating=5)
        history.prepare_change("games", before, expected_fields={"rating": 5})
        for section, item in (("shows", after), ("games", dict(after, title="Other title")),
                              ("games", dict(after, profile="Other profile"))):
            with self.subTest(section=section, item=item):
                self.assertEqual(history.reconcile(section, item),
                                 {"resolved": False, "pending_count": 0, "warning": None})
        self.assertEqual(self.detail(), {"activity": [], "runs": [], "pending_count": 1})
        self.assertTrue(history.reconcile("games", after)["resolved"])

    def test_reconcile_confirmation_failure_stays_blocked_and_retryable(self):
        before = media_item()
        after = dict(before, status="playing")
        operation = history.prepare_change("games", before, "replay", expected_fields={"status": "playing"})
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON media_events "
                               "BEGIN SELECT RAISE(ABORT, 'synthetic-private-failure'); END")
            connection.commit()
        self.assertEqual(history.reconcile("games", after),
                         {"resolved": False, "pending_count": 1,
                          "warning": "History could not confirm an interrupted change"})
        self.assertEqual(self.detail(), {"activity": [], "runs": [], "pending_count": 1})
        with self.assertRaises(history.HistoryConflict):
            history.prepare_change("games", after, expected_fields={"rating": 5})
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT state FROM media_operations WHERE operation_id = ?",
                                                (operation,)).fetchone()[0], "pending")
            connection.execute("DROP TRIGGER synthetic_failure")
            connection.commit()
        self.assertTrue(history.reconcile("games", after)["resolved"])
        self.assertEqual(len(self.detail()["activity"]), 1)
        self.assertEqual(len(self.detail()["runs"]), 2)

    def test_reconcile_undo_restores_original_runs(self):
        before = media_item(platform="Example platform", genre="Example genre", total_episodes=12)
        after = dict(before, status="playing", platform="Other platform", genre="Other genre", total_episodes=24)
        event_id = history.record_change("games", before, after, "replay")
        changes = self.detail()["activity"][0]["changes"]
        expected = {field: values["before"] for field, values in changes.items()}
        self.assertEqual(set(expected), {"status", "platform", "genre", "total_episodes"})
        history.prepare_change("games", after, "undo", undo_event_id=event_id, expected_fields=expected)
        self.assertTrue(history.reconcile("games", before)["resolved"])
        detail = self.detail()
        self.assertEqual(detail["runs"], [])
        self.assertEqual(detail["activity"][0]["kind"], "undo")
        history.reconcile("games", before)
        self.assertEqual(self.detail(), detail)

    def test_operation_payload_and_identity_collisions_are_rejected(self):
        before = media_item()
        after = dict(before, rating=5)
        operation = str(uuid4())
        history.record_change("games", before, after, operation_id=operation)
        for section, original, kind in (("shows", before, "update"),
                                        ("games", dict(before, profile="Other profile"), "update"),
                                        ("games", dict(before, notes="Different snapshot"), "update"),
                                        ("games", before, "replay")):
            with self.subTest(section=section, kind=kind):
                with self.assertRaises(history.HistoryConflict):
                    history.prepare_change(section, original, kind, operation)
        with self.assertRaises(history.HistoryConflict):
            history.finish_change(operation, dict(after, rating=8))
        with self.assertRaises(history.HistoryConflict):
            history.finish_change(operation, dict(after, title="Different title"))
        self.assertEqual(len(self.detail()["activity"]), 1)

    def test_concurrent_retries_confirm_exactly_once(self):
        before = media_item()
        after = dict(before, status="playing")
        operation = str(uuid4())

        def record(_index):
            return history.record_change("games", before, after, "replay", operation)

        with ThreadPoolExecutor(max_workers=4) as executor:
            event_ids = list(executor.map(record, range(8)))
        self.assertEqual(len(set(event_ids)), 1)
        self.assertEqual(len(self.detail()["activity"]), 1)
        self.assertEqual(len(self.detail()["runs"]), 2)

    def test_only_changed_allowlisted_fields_are_stored(self):
        before = media_item()
        after = dict(before, rating=7, tags=["Example tag"], version="new-version", key="different-key",
                     cover_url="https://example.invalid/ignored", credentials="synthetic-ignore-marker",
                     unrelated_column="synthetic-ignore-marker")
        history.record_change("games", before, after)
        self.assertEqual(self.detail()["activity"][0]["changes"], {
            "rating": {"before": None, "after": 7}, "tags": {"before": [], "after": ["Example tag"]}})
        with closing(sqlite3.connect(self.database)) as connection:
            stored = " ".join(str(row) for row in connection.execute("SELECT * FROM media_operations"))
            stored += " ".join(str(row) for row in connection.execute("SELECT * FROM media_events"))
        for excluded in ("synthetic-ignore-marker", "example.invalid", "new-version", "different-key"):
            self.assertNotIn(excluded, stored)
        history.record_change("games", after, dict(after, version="another-version", hours_played="12.50"))
        self.assertEqual(self.detail()["activity"][0]["changes"], {})
        self.assertEqual(history.insights("games")["confirmed_changes"], 1)

    def test_bounded_input_validation_precedes_storage(self):
        for changes in ({"title": "x" * 513}, {"profile": "x" * 257}, {"notes": "x" * 4097},
                        {"tags": ["Example"] * 65}, {"tags": ["x" * 129]}, {"tags": "not-a-list"},
                        {"current_episode": 1.5}, {"hours_played": "NaN"}, {"hours_played": "1e1000000"},
                        {"hours_played": "0.0000001"}, {"status": "unknown"}):
            with self.subTest(fields=list(changes)):
                with self.assertRaises(ValueError):
                    history.prepare_change("games", media_item(**changes))
        self.assertFalse(self.database.exists())
        with self.assertRaises(ValueError):
            history.identity_key("unknown", "Example", "Example")
        with self.assertRaises(ValueError):
            history.prepare_change("games", media_item(), operation_id="not-a-uuid")

    def test_section_and_profile_scopes_are_isolated(self):
        base = media_item()
        for section, original in (("games", base), ("shows", base),
                                  ("games", dict(base, profile="Other profile")),
                                  ("games", dict(base, title="Other title"))):
            history.record_change(section, original, dict(original, rating=6))
        self.assertEqual(len(self.detail()["activity"]), 1)
        self.assertEqual(len(self.detail("shows")["activity"]), 1)
        self.assertEqual(history.insights("games", " EXAMPLE PROFILE ")["completed_runs"], 2)
        self.assertEqual(history.insights("games", "Other profile")["completed_runs"], 1)
        self.assertEqual(history.insights("shows")["completed_runs"], 1)
        self.assertEqual(history.insights("games", "Absent profile")["confirmed_changes"], 0)

    def test_initial_active_pause_resume_and_completion_share_one_run(self):
        for section, active_status, paused_status in (("games", "playing", "paused"), ("shows", "watching", "on_hold")):
            original = media_item(status="backlog", date_started="", date_completed="")
            active = dict(original, status=active_status)
            history.record_change(section, original, active)
            paused = dict(active, status=paused_status)
            history.record_change(section, active, paused)
            history.record_change(section, paused, active)
            completed = dict(active, status="completed", date_completed="2026-09-19")
            history.record_change(section, active, completed)
            runs = self.detail(section)["runs"]
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["started_at"], NOW.isoformat())
            self.assertEqual(runs[0]["completed_at"], NOW.isoformat())

    def test_first_observed_completion_uses_only_known_start_date(self):
        before = media_item(status="watching", date_started="2026-09-01", date_completed="")
        history.record_change("shows", before, dict(before, current_episode=7))
        self.assertEqual(self.detail("shows")["runs"], [])
        before["current_episode"] = 7
        history.record_change("shows", before, dict(before, status="completed"))
        run = self.detail("shows")["runs"][0]
        self.assertEqual(run["started_at"], "2026-09-01")
        self.assertEqual(run["completed_at"], NOW.isoformat())
        unknown = media_item(title="Unknown dates", date_started="not-a-date", date_completed="")
        history.record_change("games", unknown, dict(unknown, rating=1))
        run = self.detail(item=unknown)["runs"][0]
        self.assertIsNone(run["started_at"])
        self.assertIsNone(run["completed_at"])

    def test_prepared_undo_rejects_later_events_and_wrong_identity(self):
        before = media_item()
        after = dict(before, rating=3)
        event_id = history.record_change("games", before, after)
        for section, item in (("shows", after), ("games", dict(after, profile="Other profile"))):
            with self.assertRaises(history.HistoryConflict):
                history.prepare_change(section, item, "undo", undo_event_id=event_id)
        operation = history.prepare_change("games", after, "undo", undo_event_id=event_id)
        with self.assertRaises(history.HistoryConflict):
            history.record_change("games", after, dict(after, rating=4))
        with self.assertRaises(history.HistoryConflict):
            history.finish_change(operation, dict(before, notes="Not an exact undo"))
        self.assertEqual(len(self.detail()["activity"]), 1)
        history.finish_change(operation, before)
        self.assertEqual(self.detail()["runs"], [])
        self.assertEqual(history.insights("games")["confirmed_changes"], 0)

    def test_undo_one_identity_does_not_touch_other_runs(self):
        before = media_item()
        other = media_item(profile="Other profile")
        event_id = history.record_change("games", before, dict(before, rating=1))
        history.record_change("games", other, dict(other, rating=2))
        other_runs = self.detail(item=other)["runs"]
        history.undo_change("games", event_id, dict(before, rating=1), before)
        self.assertEqual(self.detail(item=other)["runs"], other_runs)

    def test_known_failure_releases_reservation_but_retains_warning(self):
        before = media_item()
        operation = history.prepare_change("games", before)
        history.fail_change(operation, uncertain=False)
        with self.assertRaises(history.HistoryConflict):
            history.finish_change(operation, dict(before, rating=1))
        history.record_change("games", before, dict(before, rating=2))
        self.assertEqual(self.detail()["pending_count"], 1)
        self.assertEqual(len(self.detail()["activity"]), 1)

    def test_insights_use_confirmed_recent_deltas_and_ignore_undone_changes(self):
        before = media_item(hours_played="100")
        older = datetime.fromisoformat("2026-01-01T12:30:00-05:00")
        with patch.object(history.clock, "local_now", return_value=older):
            history.record_change("games", before, dict(before, hours_played="110"))
        before["hours_played"] = "110"
        after = dict(before, hours_played="112.5")
        history.record_change("games", before, after)
        replayed = dict(after, status="playing", hours_played="0")
        history.record_change("games", after, replayed, "replay")
        completed = dict(replayed, status="completed", hours_played="3")
        event_id = history.record_change("games", replayed, completed)
        history.prepare_change("games", completed)
        stats = history.insights("games")
        self.assertEqual(stats["completed_runs"], 2)
        self.assertEqual(stats["legacy_completed_runs"], 1)
        self.assertEqual(stats["pending_count"], 1)
        self.assertEqual(stats["last_30_days"], {"completed_runs": 1, "confirmed_changes": 3,
                                                "hours_added": 5.5, "episodes_added": 0})
        with closing(sqlite3.connect(self.database)) as connection:
            pending = connection.execute("SELECT operation_id FROM media_operations WHERE state = 'pending'").fetchone()[0]
        history.fail_change(pending, uncertain=False)
        history.undo_change("games", event_id, completed, replayed)
        stats = history.insights("games")
        self.assertEqual(stats["last_30_days"]["completed_runs"], 0)
        self.assertEqual(stats["last_30_days"]["hours_added"], 2.5)

    def test_episode_insights_do_not_invent_cross_season_progress(self):
        before = media_item(status="watching", date_completed="", current_episode=3)
        after = dict(before, current_episode=5)
        history.record_change("shows", before, after)
        next_season = dict(after, current_season=2, current_episode=8)
        history.record_change("shows", after, next_season)
        history.record_change("shows", next_season, dict(next_season, current_episode=1))
        self.assertEqual(history.insights("shows")["last_30_days"]["episodes_added"], 2)

    def test_detail_limits_do_not_truncate_undo_snapshots_or_insights(self):
        before = media_item()
        history.record_change("games", before, dict(before, rating=1))
        before["rating"] = 1
        event_id = None
        active = before
        for _index in range(101):
            active = dict(before, status="playing")
            history.record_change("games", before, active, "replay")
            event_id = history.record_change("games", active, before)
        detail = self.detail()
        self.assertEqual(len(detail["activity"]), 200)
        self.assertEqual(len(detail["runs"]), 100)
        self.assertEqual(detail["runs"][0]["number"], 102)
        self.assertEqual([event["id"] for event in detail["activity"]],
                         sorted((event["id"] for event in detail["activity"]), reverse=True))
        self.assertEqual(history.insights("games")["completed_runs"], 102)
        assert event_id is not None
        history.undo_change("games", event_id, before, active)
        self.assertEqual(history.insights("games")["completed_runs"], 101)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_runs").fetchone()[0], 102)


if __name__ == "__main__":
    unittest.main()