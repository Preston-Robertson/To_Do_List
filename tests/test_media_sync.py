"""Offline header-addressed media write regressions."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from luigi_web.modules.media import service


class MediaSyncTests(unittest.TestCase):
    def setUp(self):
        self.headers = ["Title", "Profile", "Status", "Priority", "Rating", "Notes", "Date Started", "Date Completed", "Current Episode"]
        self.values = [self.headers, ["Example title", "Example profile", "backlog", "3", "", "", "2020-01-01", "", "0"]]
        self.enterContext(patch.object(service, "is_enabled", return_value=True))
        self.read = self.enterContext(patch.object(service, "_all_values", side_effect=lambda *args, **kwargs: deepcopy(self.values)))
        self.sheet = Mock()
        self.enterContext(patch.object(service, "_ws", return_value=self.sheet))
        self.sheet.batch_update.side_effect = self.apply_batch

    def apply_batch(self, batch, **kwargs):
        for cell in batch:
            column = ord(cell["range"][0]) - ord("A")
            self.values[1][column] = cell["values"][0][0]

    def write(self, **fields):
        return service.write_item("shows", "Example profile", "Example title", fields)

    def test_one_batch_preserves_earliest_date_and_reads_back(self):
        result = self.write(status="watching", priority=4, current_episode=1)
        self.assertEqual(self.sheet.batch_update.call_count, 1)
        self.assertEqual(self.read.call_count, 2)
        self.assertEqual(self.sheet.batch_update.call_args.kwargs, {"value_input_option": "RAW"})
        self.assertEqual(result["item"]["date_started"], "2020-01-01")
        self.assertEqual(result["item"]["current_episode"], 1)
        self.assertEqual(result["before_cells"], {"Status": "backlog", "Priority": "3", "Current Episode": "0"})

    def test_noop_has_no_write(self):
        self.assertFalse(self.write(priority=3)["changed"])
        self.sheet.batch_update.assert_not_called()

    def test_bad_values_and_missing_columns_do_not_write(self):
        for fields in ({"priority": 6}, {"rating": "invalid"}, {"status": "unknown"}, {"current_episode": -1}, {"genre": "Example"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.write(**fields)
        self.sheet.batch_update.assert_not_called()

    def test_stale_version_rejected(self):
        with self.assertRaises(service.MediaConflict):
            service.write_item("shows", "Example profile", "Example title", {"priority": 4}, expected_version="stale")
        self.sheet.batch_update.assert_not_called()

    def test_noop_provider_does_not_report_success(self):
        self.sheet.batch_update.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "verified"):
            self.write(priority=4)

    def test_read_failure_does_not_overwrite_dates(self):
        self.read.side_effect = RuntimeError("synthetic read failure")
        with self.assertRaises(RuntimeError):
            self.write(status="watching")
        self.sheet.batch_update.assert_not_called()

    def test_completion_and_undo_keep_original_dates(self):
        result = self.write(status="completed")
        self.assertTrue(result["item"]["date_completed"])
        restored = service.write_item("shows", "Example profile", "Example title", {},
            expected_version=result["item"]["version"], restore_cells=result["before_cells"])
        self.assertFalse(restored["item"]["date_completed"])
        self.assertEqual(restored["item"]["date_started"], "2020-01-01")

    def test_duplicate_identity_rejected(self):
        self.values.append(self.values[1][:])
        with self.assertRaises(service.MediaConflict):
            self.write(priority=4)
        self.sheet.batch_update.assert_not_called()

    def test_catalog_noop_provider_does_not_report_success(self):
        with patch.object(service, "_invalidate") as invalidate:
            with self.assertRaisesRegex(RuntimeError, "verified"):
                service.add_catalog_item("shows", "Example profile", {"title": "New example title"})
        self.sheet.update.assert_called_once()
        self.assertEqual(self.read.call_count, 2)
        invalidate.assert_called_once_with("shows")


class MediaCatalogSyncTests(unittest.TestCase):
    def setUp(self):
        self.headers = ["Extra column", "Priority", "Title", "Profile", "Status", "Notes", "Cover URL", "Source", "Current Episode", "Current Season", "Date Added"]
        self.values = [self.headers[:]]
        self.read = self.enterContext(patch.object(service, "_all_values", side_effect=lambda *args, **kwargs: deepcopy(self.values)))
        self.sheet = Mock()
        self.enterContext(patch.object(service, "_ws", return_value=self.sheet))
        self.invalidate = self.enterContext(patch.object(service, "_invalidate"))
        self.sheet.update.side_effect = self.apply_update

    def apply_update(self, rows, cell_range, **kwargs):
        row_number = int(cell_range.partition(":")[0][1:])
        while len(self.values) < row_number:
            self.values.append([])
        self.values[row_number - 1] = rows[0][:]

    def create(self, *, section="shows", profile="Example profile", title="New example title", **fields):
        return service.add_catalog_item(section, profile,
            {"title": title, "cover_url": "example-cover", "source": "manual"}, **fields)

    def test_append_by_reordered_headers_uses_raw_and_fresh_readback(self):
        for section in ("games", "shows"):
            with self.subTest(section=section):
                self.values = [self.headers[:]]
                self.read.reset_mock()
                self.sheet.update.reset_mock()
                self.invalidate.reset_mock()
                self.assertEqual(self.create(section=section, priority=5), (True, "New example title"))
                row = self.values[1]
                self.assertEqual(len(row), len(self.headers))
                self.assertEqual(row[self.headers.index("Title")], "New example title")
                self.assertEqual(row[self.headers.index("Profile")], "Example profile")
                self.assertEqual(row[self.headers.index("Priority")], "5")
                self.assertEqual(row[self.headers.index("Status")], "backlog")
                self.assertEqual(row[self.headers.index("Source")], "manual")
                self.assertEqual(row[self.headers.index("Cover URL")], "example-cover")
                self.assertTrue(row[self.headers.index("Date Added")])
                self.sheet.update.assert_called_once_with([row], "A2:K2", value_input_option="RAW")
                self.assertEqual(self.read.call_count, 2)
                for read_call in self.read.call_args_list:
                    self.assertEqual(read_call.args, (section,))
                    self.assertEqual(read_call.kwargs, {"force": True})
                self.invalidate.assert_called_once_with(section)

    def test_reserved_row_preserves_notes_unknown_and_unheaded_cells(self):
        self.values.append(["Keep extra", "1", "", "Example profile", "paused", "Keep notes", "old-cover", "old-source", "9", "2", "", "Keep unheaded"])
        self.assertTrue(self.create()[0])
        row = self.values[1]
        self.assertEqual(len(self.values), 2)
        self.assertEqual(row[0], "Keep extra")
        self.assertEqual(row[5], "Keep notes")
        self.assertEqual(row[-1], "Keep unheaded")
        self.assertEqual(row[8:10], ["0", "1"])
        self.assertEqual(self.sheet.update.call_args.args[1], "A2:L2")

    def test_short_reserved_row_is_padded_without_losing_extra_cells(self):
        self.values.append(["Keep extra", "", "", "Example profile"])
        self.assertTrue(self.create()[0])
        self.assertEqual(len(self.values), 2)
        self.assertEqual(len(self.values[1]), len(self.headers))
        self.assertEqual(self.values[1][0], "Keep extra")

    def test_optional_metadata_columns_can_be_absent(self):
        self.values = [["Priority", "Title", "Status", "Profile"]]
        self.assertTrue(self.create()[0])
        self.sheet.update.assert_called_once_with(
            [["3", "New example title", "backlog", "Example profile"]], "A2:D2", value_input_option="RAW")

    def test_required_columns_must_exist_before_writing(self):
        for missing in ("Profile", "Title", "Status", "Priority"):
            with self.subTest(missing=missing):
                self.values = [[header for header in self.headers if header != missing]]
                self.assertFalse(self.create()[0])
        self.sheet.update.assert_not_called()

    def test_empty_sheet_or_header_does_not_write(self):
        for values in ([], [[]]):
            with self.subTest(values=values):
                self.values = values
                self.assertFalse(self.create()[0])
        self.sheet.update.assert_not_called()

    def test_invalid_inputs_are_rejected_before_read_or_write(self):
        for fields in (
            {"priority": 0}, {"priority": 6}, {"priority": "invalid"},
            {"priority": 1.5}, {"priority": True}, {"priority": None},
            {"profile": ""}, {"profile": "   "}, {"profile": "P" * 257},
            {"title": ""}, {"title": "   "}, {"title": "T" * 513},
            {"status": "unknown"}, {"section": "games", "status": "watching"},
            {"section": "invalid"},
        ):
            with self.subTest(fields=fields):
                self.assertFalse(self.create(**fields)[0])
        self.read.assert_not_called()
        self.sheet.update.assert_not_called()

    def test_identity_length_bounds_are_inclusive(self):
        self.assertEqual(self.create(profile="P" * 256, title="T" * 512), (True, "T" * 512))
        self.sheet.update.assert_called_once()

    def test_existing_identity_is_rejected_case_insensitively(self):
        self.values.append(["", "3", "NEW EXAMPLE TITLE", "EXAMPLE PROFILE", "backlog"])
        self.assertFalse(self.create()[0])
        self.sheet.update.assert_not_called()

    def test_initial_read_failure_invalidates_without_writing(self):
        failure = RuntimeError("Synthetic read failure")
        self.read.side_effect = failure
        with self.assertRaises(RuntimeError) as caught:
            self.create()
        self.assertIs(caught.exception, failure)
        self.sheet.update.assert_not_called()
        self.invalidate.assert_called_once_with("shows")

    def test_write_failure_invalidates_and_is_not_retried(self):
        failure = RuntimeError("Synthetic write failure")
        self.sheet.update.side_effect = failure
        with self.assertRaises(RuntimeError) as caught:
            self.create()
        self.assertIs(caught.exception, failure)
        self.sheet.update.assert_called_once()
        self.assertEqual(self.read.call_count, 1)
        self.invalidate.assert_called_once_with("shows")

    def test_write_applied_then_failed_is_not_retried(self):
        failure = RuntimeError("Synthetic uncertain write")

        def uncertain_update(*args, **kwargs):
            self.apply_update(*args, **kwargs)
            raise failure

        self.sheet.update.side_effect = uncertain_update
        with self.assertRaises(RuntimeError) as caught:
            self.create()
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(self.values), 2)
        self.sheet.update.assert_called_once()
        self.assertEqual(self.read.call_count, 1)
        self.invalidate.assert_called_once_with("shows")

    def test_readback_failure_invalidates_and_does_not_retry_write(self):
        failure = RuntimeError("Synthetic readback failure")
        self.read.side_effect = [deepcopy(self.values), failure]
        with self.assertRaises(RuntimeError) as caught:
            self.create()
        self.assertIs(caught.exception, failure)
        self.sheet.update.assert_called_once()
        self.assertEqual(self.read.call_count, 2)
        self.invalidate.assert_called_once_with("shows")

    def test_readback_must_match_each_intended_cell(self):
        for header in ("Title", "Profile", "Status", "Priority", "Cover URL", "Source", "Current Episode", "Current Season", "Date Added"):
            with self.subTest(header=header):
                self.values = [self.headers[:]]
                self.sheet.update.reset_mock()
                self.invalidate.reset_mock()

                def changed_update(*args, changed_header=header, **kwargs):
                    self.apply_update(*args, **kwargs)
                    self.values[1][self.headers.index(changed_header)] = "unexpected"

                self.sheet.update.side_effect = changed_update
                with self.assertRaisesRegex(RuntimeError, "verified"):
                    self.create()
                self.sheet.update.assert_called_once()
                self.invalidate.assert_called_once_with("shows")

    def test_duplicate_readback_identity_is_not_verified(self):
        def duplicate_update(*args, **kwargs):
            self.apply_update(*args, **kwargs)
            duplicate = self.values[1][:]
            duplicate[self.headers.index("Title")] = "NEW EXAMPLE TITLE"
            duplicate[self.headers.index("Profile")] = "EXAMPLE PROFILE"
            self.values.append(duplicate)

        self.sheet.update.side_effect = duplicate_update
        with self.assertRaisesRegex(RuntimeError, "verified"):
            self.create()
        self.sheet.update.assert_called_once()
        self.invalidate.assert_called_once_with("shows")

    def test_readback_missing_written_column_is_not_verified(self):
        def missing_column_update(*args, **kwargs):
            self.apply_update(*args, **kwargs)
            column = self.headers.index("Source")
            for row in self.values:
                row.pop(column)

        self.sheet.update.side_effect = missing_column_update
        with self.assertRaisesRegex(RuntimeError, "verified"):
            self.create()
        self.sheet.update.assert_called_once()
        self.invalidate.assert_called_once_with("shows")

    def test_readback_uses_fresh_headers_and_identity_after_row_move(self):
        def moved_update(*args, **kwargs):
            self.apply_update(*args, **kwargs)
            self.values.insert(1, ["", "3", "Other title", "Other profile", "backlog"] + [""] * 6)
            self.values = [list(reversed(row)) for row in self.values]

        self.sheet.update.side_effect = moved_update
        self.assertTrue(self.create()[0])
        self.sheet.update.assert_called_once()
        self.assertEqual(self.read.call_count, 2)

    def test_lock_covers_initial_read_write_readback_and_invalidation(self):
        from unittest.mock import MagicMock

        lock = MagicMock()
        events = Mock()
        events.attach_mock(lock.__enter__, "enter")
        events.attach_mock(self.read, "read")
        events.attach_mock(self.sheet.update, "update")
        events.attach_mock(self.invalidate, "invalidate")
        events.attach_mock(lock.__exit__, "exit")
        with patch.object(service, "_write_lock", lock):
            self.assertTrue(self.create()[0])
        self.assertEqual([event[0] for event in events.mock_calls],
            ["enter", "read", "update", "read", "invalidate", "exit"])

    def test_steam_stats_delegates_without_turning_unknown_into_zero(self):
        snapshot = {"app_id": "123", "hours_played": None, "achievements_total": 0}
        with (
            patch("luigi_web.modules.media.steam._fetch", return_value=snapshot) as fetch,
            patch.object(service.httpx, "Client", side_effect=AssertionError("Unexpected network access")),
        ):
            self.assertIs(service.steam_stats("123"), snapshot)
        fetch.assert_called_once_with("123")
        self.assertIsNone(snapshot["hours_played"])

    def test_steam_stats_preserves_fetch_failure(self):
        failure = RuntimeError("Steam statistics are unavailable.")
        with patch("luigi_web.modules.media.steam._fetch", side_effect=failure) as fetch:
            with self.assertRaises(RuntimeError) as caught:
                service.steam_stats("123")
        self.assertIs(caught.exception, failure)
        fetch.assert_called_once_with("123")