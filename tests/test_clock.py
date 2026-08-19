"""Configured single-user timezone regressions."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from luigi_web import clock, task_events


class ClockTests(unittest.TestCase):
    def test_default_timezone_is_new_york(self) -> None:
        with patch.dict(os.environ, {"LUIGI_WEB_TIMEZONE": ""}):
            self.assertEqual(clock.timezone_name(), "America/New_York")

    def test_legacy_naive_utc_timestamp_converts_to_previous_est_date(self) -> None:
        with patch.dict(os.environ, {"LUIGI_WEB_TIMEZONE": "America/New_York"}):
            self.assertEqual(
                clock.local_date_from_timestamp("2026-08-19T01:00:00"),
                "2026-08-18",
            )

    def test_effective_date_applies_cutoff_after_timezone_conversion(self) -> None:
        occurred = datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc)  # 03:00 EDT
        with patch.dict(os.environ, {"LUIGI_WEB_TIMEZONE": "America/New_York"}):
            self.assertEqual(
                task_events.effective_date_for(occurred, cutoff="04:00"),
                "2026-08-18",
            )

    def test_invalid_timezone_fails_visibly(self) -> None:
        with patch.dict(os.environ, {"LUIGI_WEB_TIMEZONE": "Not/AZone"}):
            with self.assertRaisesRegex(ValueError, "valid IANA timezone"):
                clock.user_timezone()


if __name__ == "__main__":
    unittest.main()