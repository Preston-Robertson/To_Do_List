"""Weekly targets count completion days, not an assumed daily cadence."""
from __future__ import annotations

from datetime import date
import unittest

from luigi_web.modules.discipline.progress import weekly_progress


class WeeklyProgressTests(unittest.TestCase):
    def test_spaced_completions_can_meet_a_weekly_target(self) -> None:
        state = weekly_progress(["2030-04-01", "2030-04-03", "2030-04-05"], 3, date(2030, 4, 7))
        self.assertEqual(state, {"week_start": "2030-04-01", "week_end": "2030-04-07", "count": 3, "target": 3, "remaining": 0, "target_met": True})

    def test_duplicates_future_and_other_week_do_not_count(self) -> None:
        state = weekly_progress(["2030-04-01", "2030-04-01", "2030-03-31", "2030-04-06", "invalid"], 4, date(2030, 4, 5))
        self.assertEqual(state["count"], 1)
        self.assertEqual(state["remaining"], 3)
        self.assertFalse(state["target_met"])

    def test_week_can_span_calendar_years(self) -> None:
        state = weekly_progress(["2029-12-31", "2030-01-01", "2030-01-02"], 4, date(2030, 1, 2))
        self.assertEqual(state["week_start"], "2029-12-31")
        self.assertEqual(state["count"], 3)
        self.assertEqual(state["remaining"], 1)

    def test_extra_days_remain_visible_without_negative_remaining(self) -> None:
        state = weekly_progress(["2030-04-01", "2030-04-02", "2030-04-03", "2030-04-04"], 2, date(2030, 4, 5))
        self.assertEqual(state["count"], 4)
        self.assertEqual(state["remaining"], 0)
        self.assertTrue(state["target_met"])

    def test_daily_target_does_not_report_early_failure(self) -> None:
        state = weekly_progress(["2030-04-01"], 7, date(2030, 4, 1))
        self.assertEqual(state["remaining"], 6)
        self.assertNotIn("at_risk", state)

    def test_invalid_targets_fail_validation(self) -> None:
        for target in (0, 8, True, "3", None):
            with self.subTest(target=target), self.assertRaises(ValueError):
                weekly_progress([], target, date(2030, 4, 1))


if __name__ == "__main__":
    unittest.main()