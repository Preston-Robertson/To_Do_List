"""Synthetic planning math only; no financial data or application startup."""
from datetime import date
from copy import deepcopy
from decimal import Decimal, Inexact, ROUND_DOWN, ROUND_HALF_UP, localcontext
from typing import Any
import unittest
from unittest.mock import patch

from luigi_web.modules.finance.projections import (
    forecast, housing_projection, scheduled_dates, scheduled_months, wealth_projection,
)


class FinanceScheduleTests(unittest.TestCase):
    def item(self, **overrides):
        return {"amount_minor": -1250, "next_due_date": "2028-01-31", "cadence": "monthly", "active": 1, **overrides}

    def test_month_end_keeps_original_anchor_through_leap_year(self):
        self.assertEqual(scheduled_dates(self.item(), date(2028, 1, 1), date(2028, 4, 30)),
                         [date(2028, 1, 31), date(2028, 2, 29), date(2028, 3, 31), date(2028, 4, 30)])

    def test_overdue_schedule_is_not_mutated_or_counted_as_paid(self):
        item = self.item(next_due_date="2020-01-31")
        before = deepcopy(item)
        self.assertEqual(scheduled_dates(item, date(2028, 3, 1), date(2028, 3, 31)), [date(2028, 3, 31)])
        self.assertEqual(item, before)

    def test_weekly_and_inactive_and_future_schedules(self):
        dates = scheduled_dates(self.item(cadence="weekly", next_due_date="2028-01-01"), date(2028, 1, 1), date(2028, 1, 31))
        self.assertEqual(len(dates), 5)
        self.assertEqual(scheduled_dates(self.item(active=0), date(2028, 1, 1), date(2028, 2, 1)), [])
        self.assertEqual(scheduled_dates(self.item(), date(2027, 1, 1), date(2027, 12, 31)), [])

    def test_yearly_leap_anchor_returns_to_leap_day(self):
        result = scheduled_dates(self.item(cadence="yearly", next_due_date="2024-02-29"), date(2027, 1, 1), date(2028, 12, 31))
        self.assertEqual(result, [date(2027, 2, 28), date(2028, 2, 29)])

    def test_separate_inflows_and_outflows_without_double_counting(self):
        result = scheduled_months([self.item(), self.item(amount_minor=5000)], date(2028, 1, 1), 2)
        self.assertEqual(result[1], {"month": "2028-02", "income_minor": 5000, "expense_minor": 1250, "occurrences": 2})

    def test_projection_bounds_and_integer_amounts(self):
        for months in (0, 601, True):
            with self.assertRaises(ValueError):
                scheduled_months([], date(2028, 1, 1), months)
        with self.assertRaises(ValueError):
            scheduled_months([self.item(amount_minor=1.5)], date(2028, 1, 1))


class FinanceForecastTests(unittest.TestCase):
    def config(self, **overrides):
        return {"start_month": "2028-01", "months": 2, **overrides}

    def recurring(self, amount: Any = -100, **overrides: Any) -> dict[str, Any]:
        return {"amount_minor": amount, "next_due_date": "2028-01-31",
                "cadence": "monthly", "active": True, **overrides}

    def stream(self, **overrides):
        return {"amount_minor": 100, "start_date": "2028-01-01", "end_date": None,
                "cadence": "biweekly", "annual_growth_bps": 0, "active": True, **overrides}

    def test_transfer_conserves_wealth_and_is_not_spending(self):
        result = forecast(self.config(opening_cash_minor=1000, monthly_contribution_minor=200), [])
        first = result["rows"][0]
        self.assertEqual((first["ending_cash_minor"], first["investment_value_minor"], first["net_worth_minor"]),
                         (800, 200, 1000))
        self.assertEqual(first["surplus_minor"], 0)
        self.assertEqual(result["totals"]["outflow_minor"], 0)
        self.assertEqual(result["totals"]["contributions_minor"], 400)

    def test_recurring_bills_and_additional_spending(self):
        result = forecast(self.config(variable_expense_minor=50),
                          [self.recurring(), self.recurring(500), self.recurring(-900, active=False)])
        row = result["rows"][0]
        self.assertEqual((row["income_minor"], row["bills_minor"], row["variable_minor"], row["surplus_minor"]),
                         (500, 100, 50, 350))
        self.assertEqual(result["totals"]["ending_cash_minor"], 700)
        self.assertEqual(result["totals"]["outflow_minor"], 300)

    def test_fixed_income_substitutes_and_grows_once_annually(self):
        result = forecast(self.config(months=13, income_mode="fixed", net_income_minor=1000,
                                      annual_income_growth_bps=1000, annual_expense_growth_bps=2000,
                                      variable_expense_minor=50),
                          [self.recurring(), self.recurring(9999)])
        self.assertEqual(result["rows"][11]["income_minor"], 1000)
        last = result["rows"][12]
        self.assertEqual((last["income_minor"], last["bills_minor"], last["variable_minor"]), (1100, 120, 60))

    def test_streams_replace_recurring_and_use_per_occurrence_pay(self):
        result = forecast(self.config(income_mode="streams", net_income_minor=7777),
                          [self.recurring(9999), self.recurring()], [self.stream()])
        self.assertEqual([row["income_minor"] for row in result["rows"]], [300, 200])
        self.assertEqual([row["bills_minor"] for row in result["rows"]], [100, 100])

    def test_stream_month_end_and_inclusive_end_date(self):
        result = forecast(self.config(months=4, income_mode="streams"), [],
                          [self.stream(cadence="monthly", start_date="2028-01-31", end_date="2028-03-31"),
                           self.stream(active=False)])
        self.assertEqual([row["income_minor"] for row in result["rows"]], [100, 100, 100, 0])

    def test_stream_growth_uses_anchor_anniversary_not_global_growth(self):
        result = forecast(self.config(start_month="2028-02", months=2, income_mode="streams",
                                      annual_income_growth_bps=3000), [],
                          [self.stream(cadence="monthly", start_date="2027-03-31", annual_growth_bps=1000)])
        self.assertEqual([row["income_minor"] for row in result["rows"]], [100, 110])

    def test_negative_cash_is_not_clamped_and_reserve_is_separate(self):
        result = forecast(self.config(opening_cash_minor=150, reserve_minor=100,
                                      monthly_contribution_minor=100), [])
        self.assertEqual([row["ending_cash_minor"] for row in result["rows"]], [50, -50])
        self.assertTrue(result["rows"][0]["below_reserve"])
        self.assertFalse(result["rows"][0]["funding_shortfall"])
        self.assertTrue(result["rows"][1]["funding_shortfall"])
        self.assertEqual(result["totals"]["first_shortfall_month"], "2028-02")
        self.assertEqual(result["totals"]["first_below_reserve_month"], "2028-01")
        self.assertEqual(result["totals"]["min_cash_minor"], -50)

    def test_default_starts_next_month_without_actuals(self):
        with patch("luigi_web.modules.finance.projections.date", wraps=date) as clock:
            clock.today.return_value = date(2028, 12, 19)
            result = forecast({"opening_cash_minor": 1234}, [])
        self.assertEqual(result["start_month"], "2029-01")
        self.assertEqual(len(result["rows"]), 12)
        self.assertEqual(result["rows"][0]["ending_cash_minor"], 1234)
        self.assertTrue(result["warnings"])

    def test_no_mutation_and_only_integer_money(self):
        config = self.config(income_mode="streams", annual_return_bps=725, inflation_bps=275,
                             opening_investments_minor=10000)
        recurring = [self.recurring()]
        streams = [self.stream()]
        before = deepcopy((config, recurring, streams))
        result = forecast(config, recurring, streams)
        self.assertEqual((config, recurring, streams), before)
        for row in result["rows"] + [result["totals"]]:
            for key, value in row.items():
                if key.endswith("_minor"):
                    self.assertIs(type(value), int)

    def test_defensive_bounds_do_not_echo_values(self):
        cases = [{"months": 0}, {"months": 61}, {"months": True}, {"net_income_minor": 1.5},
                 {"opening_cash_minor": 10**14 + 1}, {"annual_income_growth_bps": -1},
                 {"annual_expense_growth_bps": 3001}, {"annual_return_bps": -10000},
                 {"annual_return_bps": -5001}, {"inflation_bps": 2001},
                 {"start_month": "synthetic-invalid-date"}, {"income_mode": "synthetic-invalid-mode"}]
        for invalid in cases:
            with self.subTest(fields=list(invalid)):
                with self.assertRaises(ValueError) as raised:
                    forecast(self.config(**invalid), [])
                self.assertNotIn("synthetic-invalid", str(raised.exception))

    def test_effective_returns_inflation_and_month_end_contributions(self):
        config = self.config(months=12, opening_cash_minor=120000, opening_investments_minor=100000,
                             monthly_contribution_minor=10000, annual_return_bps=1200, inflation_bps=1000)
        result = forecast(config, [])
        with localcontext() as context:
            context.prec = 65
            multiplier = Decimal("1.12") ** (Decimal(1) / Decimal(12))
            expected = 100000
            for row in result["rows"]:
                expected = int((Decimal(expected) * multiplier + 10000).to_integral_value(rounding=ROUND_HALF_UP))
                self.assertEqual(row["investment_value_minor"], expected)
            expected_real = int((Decimal(expected) / Decimal("1.10")).to_integral_value(rounding=ROUND_HALF_UP))
        self.assertEqual(result["rows"][-1]["real_net_worth_minor"], expected_real)
        first_deposit = forecast(self.config(months=1, monthly_contribution_minor=10000,
                                             annual_return_bps=3000), [])["rows"][0]
        self.assertEqual(first_deposit["investment_value_minor"], 10000)

    def test_negative_returns_and_output_overflow(self):
        result = forecast(self.config(months=12, opening_investments_minor=1000000,
                                      annual_return_bps=-5000), [])
        self.assertLessEqual(abs(result["rows"][-1]["investment_value_minor"] - 500000), 5)
        with self.assertRaises(ValueError):
            forecast(self.config(opening_cash_minor=10**14, income_mode="fixed", net_income_minor=1), [])

    def test_annual_inflation_half_unit_ties_round_away_from_zero(self):
        for opening_cash in (3, -3):
            result = forecast(self.config(months=12, opening_cash_minor=opening_cash, inflation_bps=2000), [])
            self.assertEqual(result["rows"][-1]["real_net_worth_minor"], opening_cash)

    def test_decimal_math_is_independent_of_caller_context(self):
        config = self.config(months=12, opening_investments_minor=10000, annual_return_bps=725, inflation_bps=250)
        housing = {"home_price_minor": 120000, "mortgage_rate_bps": 625, "appreciation_bps": 300}
        expected_forecast = forecast(config, [])
        expected_housing = housing_projection(housing)
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_DOWN
            context.traps[Inexact] = True
            self.assertEqual(forecast(config, []), expected_forecast)
            self.assertEqual(housing_projection(housing), expected_housing)

    def test_all_stream_cadences_keep_the_original_anchor(self):
        cases = {
            "weekly": [100, 400, 400, 400],
            "biweekly": [100, 200, 200, 200],
            "monthly": [100, 100, 100, 100],
            "quarterly": [100, 0, 0, 100],
            "yearly": [100, 0, 0, 0],
        }
        for cadence, expected in cases.items():
            with self.subTest(cadence=cadence):
                result = forecast(self.config(months=4, income_mode="streams"), [],
                                  [self.stream(cadence=cadence, start_date="2028-01-31")])
                self.assertEqual([row["income_minor"] for row in result["rows"]], expected)

    def test_leap_day_stream_returns_to_original_day_and_grows_on_anniversary(self):
        result = forecast(self.config(start_month="2027-02", months=13, income_mode="streams"), [],
                          [self.stream(start_date="2024-02-29", cadence="yearly", annual_growth_bps=1000)])
        self.assertEqual(result["rows"][0]["income_minor"], 133)
        self.assertEqual(result["rows"][-1]["income_minor"], 146)
        self.assertEqual(sum(row["income_minor"] for row in result["rows"][1:-1]), 0)

    def test_stream_end_is_inclusive_and_expired_future_and_inactive_are_excluded(self):
        result = forecast(self.config(income_mode="streams"), [], [
            self.stream(end_date="2028-01-29"),
            self.stream(start_date="2027-01-01", end_date="2027-12-31"),
            self.stream(start_date="2028-03-01"),
            {"active": False},
        ])
        self.assertEqual([row["income_minor"] for row in result["rows"]], [300, 0])

    def test_stream_rounding_is_per_occurrence_not_aggregate(self):
        result = forecast(self.config(months=1, income_mode="streams"), [],
                          [self.stream(amount_minor=5, cadence="weekly", start_date="2027-01-01", annual_growth_bps=1000)])
        self.assertEqual(result["rows"][0]["income_minor"], 24)

    def test_recurring_growth_steps_from_projection_start(self):
        result = forecast(self.config(start_month="2028-03", months=13, annual_income_growth_bps=1000),
                          [self.recurring(1000, next_due_date="2020-01-31")])
        self.assertEqual(result["rows"][10]["income_minor"], 1000)
        self.assertEqual(result["rows"][11]["income_minor"], 1000)
        self.assertEqual(result["rows"][12]["income_minor"], 1100)

    def test_income_modes_are_exclusive_and_no_budgets_or_actuals_are_added(self):
        for mode, expected in (("recurring", 200), ("fixed", 300), ("streams", 100)):
            config = self.config(months=1, income_mode=mode, net_income_minor=300,
                                 budgets=[{"amount_minor": 999}], actuals=[{"amount_minor": 999}])
            result = forecast(config, [self.recurring(200)], [self.stream(cadence="monthly")])
            self.assertEqual(result["rows"][0]["income_minor"], expected)
            self.assertEqual(result["totals"]["outflow_minor"], 0)

    def test_multiple_streams_add_net_pay_without_general_growth(self):
        result = forecast(self.config(months=13, income_mode="streams", annual_income_growth_bps=3000), [],
                          [self.stream(cadence="monthly", annual_growth_bps=1000),
                           self.stream(cadence="monthly", amount_minor=250, annual_growth_bps=2000)])
        self.assertEqual(result["rows"][0]["income_minor"], 350)
        self.assertEqual(result["rows"][-1]["income_minor"], 410)

    def test_invalid_streams_fail_without_echoing_input(self):
        cases = [{"amount_minor": -1}, {"amount_minor": True}, {"amount_minor": 1.5},
                 {"amount_minor": 10**14 + 1}, {"cadence": "synthetic-invalid-cadence"},
                 {"start_date": "synthetic-invalid-date"}, {"start_date": "2028-02-30"},
                 {"end_date": "2027-12-31"}, {"end_date": "synthetic-invalid-date"},
                 {"annual_growth_bps": -1}, {"annual_growth_bps": 3001}, {"active": "synthetic-invalid-flag"}]
        for invalid in cases:
            with self.subTest(fields=list(invalid)):
                with self.assertRaises(ValueError) as raised:
                    forecast(self.config(income_mode="streams"), [], [self.stream(**invalid)])
                self.assertNotIn("synthetic-invalid", str(raised.exception))

    def test_missing_active_schedule_amounts_are_not_assumed_zero(self):
        recurring = self.recurring()
        stream = self.stream()
        del recurring["amount_minor"]
        del stream["amount_minor"]
        with self.assertRaises(ValueError):
            forecast(self.config(), [recurring])
        with self.assertRaises(ValueError):
            forecast(self.config(income_mode="streams"), [], [stream])
        self.assertEqual(forecast(self.config(), [{"active": False}])["totals"]["outflow_minor"], 0)

    def test_invalid_recurring_values_and_containers(self):
        for invalid in ({"amount_minor": True}, {"amount_minor": 1.5}, {"active": 2},
                        {"cadence": "biweekly"}, {"cadence": []}, {"next_due_date": None}):
            with self.subTest(fields=list(invalid)), self.assertRaises(ValueError):
                forecast(self.config(), [self.recurring(**invalid)])
        invalid_collections: list[Any] = [None, "synthetic", [None], {}]
        for invalid in invalid_collections:
            with self.assertRaises(ValueError):
                forecast(self.config(), invalid)
            with self.assertRaises(ValueError):
                forecast(self.config(income_mode="streams"), [], invalid)
        invalid_configs: list[Any] = [None, [], "synthetic"]
        for invalid in invalid_configs:
            with self.assertRaises(ValueError):
                forecast(invalid, [])
            with self.assertRaises(ValueError):
                wealth_projection(invalid)
            with self.assertRaises(ValueError):
                housing_projection(invalid)

    def test_month_format_and_date_window_validation(self):
        for month in (None, "2028-1", "2028-13", "2028-00", "2028/01", "0000-01", "9999-12"):
            with self.subTest(month=month), self.assertRaises(ValueError):
                forecast(self.config(start_month=month), [])
        result = forecast(self.config(start_month="0001-01"), [])
        self.assertEqual(result["rows"][0]["month"], "0001-01")

    def test_money_input_boundaries_and_month_limit(self):
        self.assertEqual(len(forecast(self.config(months=60), [])["rows"]), 60)
        for field in ("opening_cash_minor", "opening_investments_minor", "net_income_minor",
                      "variable_expense_minor", "monthly_contribution_minor", "reserve_minor"):
            with self.subTest(field=field):
                forecast(self.config(months=1, income_mode="fixed", **{field: 10**14}), [])
                for invalid in (10**14 + 1, True, 1.5):
                    with self.assertRaises(ValueError):
                        forecast(self.config(**{field: invalid}), [])

    def test_reserve_equality_and_opening_deficit_are_not_hidden(self):
        equal = forecast(self.config(months=1, opening_cash_minor=100, reserve_minor=100), [])
        self.assertFalse(equal["rows"][0]["below_reserve"])
        recovered = forecast(self.config(months=1, opening_cash_minor=-100,
                                         income_mode="fixed", net_income_minor=200), [])
        self.assertEqual(recovered["totals"]["min_cash_minor"], -100)
        self.assertTrue(recovered["totals"]["funding_shortfall"])
        self.assertIsNone(recovered["totals"]["first_shortfall_month"])

    def test_shortfall_month_can_differ_from_minimum_cash(self):
        result = forecast(self.config(months=3, opening_cash_minor=10, monthly_contribution_minor=10), [])
        self.assertEqual(result["totals"]["first_shortfall_month"], "2028-02")
        self.assertEqual(result["totals"]["min_cash_minor"], -20)


class FinanceWealthTests(unittest.TestCase):
    def config(self, **overrides):
        return {"start_month": "2028-01", "opening_cash_minor": 1000,
                "opening_investments_minor": 2000, "income_mode": "fixed",
                "net_income_minor": 100, "monthly_contribution_minor": 50, **overrides}

    def test_year_zero_and_fifty_years_without_monthly_payload(self):
        result = wealth_projection(self.config(), 50)
        self.assertEqual(len(result["rows"]), 51)
        self.assertEqual(result["rows"][0]["net_worth_minor"], 3000)
        final = result["rows"][-1]
        self.assertEqual(final["year"], 50)
        self.assertEqual((final["cash_minor"], final["investment_value_minor"], final["net_worth_minor"]),
                         (31000, 32000, 63000))
        self.assertEqual((final["contributions_minor"], final["annual_contributions_minor"]), (30000, 600))
        self.assertNotIn("months", result)

    def test_yearly_values_reconcile_with_monthly_engine(self):
        config = self.config(months=24, annual_return_bps=650, inflation_bps=250,
                             annual_income_growth_bps=500)
        monthly = forecast(config, [])
        yearly = wealth_projection(config, 2)
        for year in (1, 2):
            month = monthly["rows"][year * 12 - 1]
            row = yearly["rows"][year]
            self.assertEqual(row["cash_minor"], month["ending_cash_minor"])
            for key in ("investment_value_minor", "net_worth_minor", "real_net_worth_minor"):
                self.assertEqual(row[key], month[key])
        self.assertEqual(monthly["totals"], yearly["totals"])

    def test_streams_recurring_bills_and_annual_minimum(self):
        config = self.config(income_mode="streams", opening_cash_minor=0, monthly_contribution_minor=0,
                             recurring=[{"amount_minor": -10, "next_due_date": "2028-01-01", "cadence": "monthly"}],
                             income_streams=[{"amount_minor": 200, "start_date": "2028-12-01", "cadence": "yearly"}])
        result = wealth_projection(config, 1)
        self.assertEqual(result["rows"][1]["cash_minor"], 80)
        self.assertEqual(result["rows"][1]["min_cash_minor"], -110)
        self.assertTrue(result["rows"][1]["funding_shortfall"])

    def test_scenario_calls_are_independent_and_input_is_unchanged(self):
        base = self.config(annual_return_bps=600)
        before = deepcopy(base)
        results = [wealth_projection({**base, "annual_return_bps": rate}, 10) for rate in (400, 600, 800)]
        self.assertEqual(base, before)
        values = [result["rows"][-1]["net_worth_minor"] for result in results]
        self.assertLess(values[0], values[1])
        self.assertLess(values[1], values[2])
        self.assertEqual(results[1], wealth_projection(base))

    def test_year_bounds(self):
        invalid_years: list[Any] = [0, 51, True, 1.5]
        for years in invalid_years:
            with self.subTest(years=years), self.assertRaises(ValueError):
                wealth_projection(self.config(), years)


class FinanceHousingTests(unittest.TestCase):
    def config(self, **overrides):
        return {"home_price_minor": 120000, "down_payment_minor": 0, "mortgage_rate_bps": 0,
                "term_years": 1, "years": 2, "rent_minor": 1000, **overrides}

    def test_zero_apr_payoff_and_no_payments_after_term(self):
        result = housing_projection(self.config())
        self.assertEqual(result["monthly_payment_minor"], 10000)
        first, second = result["rows"]
        self.assertEqual((first["principal_paid_minor"], first["interest_paid_minor"], first["mortgage_balance_minor"]),
                         (120000, 0, 0))
        self.assertEqual(first["owner_consumption_minor"], 0)
        self.assertEqual(first["equity_minor"], 120000)
        self.assertEqual(second["owner_cost_minor"], 0)
        self.assertEqual(second["total_cash_out_minor"], 120000)

    def test_fixed_payment_interest_and_principal_reconcile(self):
        result = housing_projection(self.config(mortgage_rate_bps=1200))
        first, second = result["rows"]
        self.assertEqual(result["monthly_payment_minor"], 10662)
        self.assertEqual(first["principal_paid_minor"], 120000)
        self.assertEqual(first["interest_paid_minor"], 7942)
        self.assertEqual(first["owner_cost_minor"], 127942)
        self.assertEqual(first["mortgage_balance_minor"], 0)
        self.assertEqual(second["interest_paid_minor"], 0)

    def test_final_payment_rounding_clears_balance(self):
        for price in (1, 6, 1201, 1205):
            for rate in (0, 1, 500, 3000):
                with self.subTest(price=price, rate=rate):
                    result = housing_projection(self.config(home_price_minor=price, mortgage_rate_bps=rate))
                    self.assertEqual(result["rows"][0]["mortgage_balance_minor"], 0)
                    self.assertEqual(result["rows"][0]["principal_paid_minor"], price)
                    self.assertEqual(result["rows"][1]["principal_paid_minor"], 0)

    def test_initial_outlay_rent_and_nonmortgage_costs_are_explicit(self):
        result = housing_projection(self.config(down_payment_minor=120000, closing_cost_minor=3000,
                                                 annual_property_tax_bps=100, annual_insurance_minor=601,
                                                 annual_maintenance_bps=200, hoa_minor=50,
                                                 appreciation_bps=1000, rent_growth_bps=1000))
        self.assertEqual(result["monthly_payment_minor"], 0)
        self.assertEqual(result["initial_cash_minor"], 123000)
        first, second = result["rows"]
        self.assertEqual(first["owner_cost_minor"], 4801)
        self.assertEqual(first["total_cash_out_minor"], 127801)
        self.assertEqual((first["home_value_minor"], second["home_value_minor"]), (132000, 145200))
        self.assertEqual((second["property_tax_minor"], second["maintenance_minor"]), (1320, 2640))
        self.assertEqual((first["rent_cost_minor"], second["rent_cost_minor"]), (12000, 13200))
        self.assertEqual(second["cumulative_rent_cost_minor"], 25200)
        self.assertEqual(second["total_cash_out_minor"], result["initial_cash_minor"] + second["cumulative_owner_cost_minor"])

    def test_multi_year_amortization_conserves_principal_and_equity(self):
        for term in (15, 30, 50):
            result = housing_projection(self.config(term_years=term, years=50, down_payment_minor=20000,
                                                     mortgage_rate_bps=625))
            for row in result["rows"]:
                self.assertGreaterEqual(row["mortgage_balance_minor"], 0)
                self.assertEqual(row["cumulative_principal_paid_minor"] + row["mortgage_balance_minor"], 100000)
                self.assertEqual(row["equity_minor"], row["home_value_minor"] - row["mortgage_balance_minor"])
                self.assertEqual(row["owner_cost_minor"], row["principal_paid_minor"] + row["interest_paid_minor"])
            self.assertEqual(result["rows"][-1]["mortgage_balance_minor"], 0)

    def test_depreciation_can_produce_negative_equity(self):
        result = housing_projection(self.config(term_years=30, years=1, appreciation_bps=-5000))
        row = result["rows"][0]
        self.assertEqual(row["home_value_minor"], 60000)
        self.assertLess(row["equity_minor"], 0)

    def test_no_implied_rental_investments_or_mutation(self):
        config = self.config(investment_return_bps=0)
        before = deepcopy(config)
        result = housing_projection(config)
        self.assertEqual(config, before)
        self.assertFalse(result["rental_portfolio_modeled"])
        self.assertEqual(result["investment_return_bps"], 0)
        self.assertEqual(result["rows"], housing_projection({**config, "investment_return_bps": 1000})["rows"])
        self.assertTrue(any("opportunity" in assumption for assumption in result["assumptions"]))
        self.assertIs(type(result["monthly_payment_minor"]), int)
        for row in result["rows"]:
            for key, value in row.items():
                if key.endswith("_minor"):
                    self.assertIs(type(value), int)

    def test_invalid_housing_inputs_and_overflow(self):
        cases = [{"home_price_minor": 0}, {"down_payment_minor": 120001}, {"mortgage_rate_bps": -1},
                 {"term_years": 0}, {"term_years": 51}, {"years": True}, {"years": 51},
                 {"rent_minor": -1}, {"annual_insurance_minor": 1.5}, {"hoa_minor": True},
                 {"appreciation_bps": -5001}, {"appreciation_bps": 3001},
                 {"rent_growth_bps": 3001}, {"investment_return_bps": -10000},
                 {"annual_property_tax_bps": 10001}, {"annual_maintenance_bps": -1},
                 {"closing_cost_minor": 10**14 + 1},
                 {"home_price_minor": 10**14, "down_payment_minor": 10**14, "closing_cost_minor": 1}]
        for invalid in cases:
            with self.subTest(fields=list(invalid)), self.assertRaises(ValueError):
                housing_projection(self.config(**invalid))