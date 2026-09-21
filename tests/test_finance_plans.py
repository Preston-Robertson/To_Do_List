"""Offline Finance planning storage tests using disposable synthetic data only."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, cast
import unittest
from unittest.mock import patch
import uuid

from luigi_web.modules.finance import plans
from luigi_web.modules.finance import repository as finance


class FinancePlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "synthetic-finance.sqlite3"
        environment = patch.dict(os.environ, {
            "LUIGI_WEB_FINANCE_DB": str(self.path),
            "LUIGI_WEB_FINANCE_BASE_CURRENCY": "USD",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def config(self, **overrides):
        return {"start_month": "2028-01", "currency": "USD", **overrides}

    def save(self, **overrides):
        return plans.save_plan("cashflow", "Example scenario", self.config(**overrides))

    def test_schema_is_lazy_empty_and_idempotent(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(plans.list_plans(), [])
        plans.init_db()
        with finance.connect() as connection:
            for table, columns in plans.BACKUP_COLUMNS.items():
                actual = tuple(row["name"] for row in connection.execute(f"PRAGMA table_info({table})"))
                self.assertEqual(actual, columns)
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_defaults_are_zero_and_start_next_month(self):
        with patch.object(finance.clock, "local_today", return_value=date(2027, 12, 31)):
            saved = plans.save_plan("cashflow", "Example scenario", {})
        self.assertEqual(saved["config"]["start_month"], "2028-01")
        self.assertEqual(saved["config"]["schema_version"], 1)
        for field, value in saved["config"].items():
            if field.endswith(("_minor", "_bps")):
                self.assertIs(type(value), int)
                self.assertEqual(value, 0)
        self.assertEqual(plans.get_plan(saved["id"]), saved)
        self.assertNotIn("payload_json", saved)
        self.assertEqual(saved["version"], 1)
        self.assertEqual(str(uuid.UUID(saved["id"])), saved["id"])

    def test_rename_version_guard_and_delete(self):
        saved = self.save()
        renamed = plans.save_plan("cashflow", "Example revised", saved["config"], saved["id"], 1)
        self.assertEqual(renamed["name"], "Example revised")
        self.assertEqual(renamed["version"], 2)
        self.assertEqual(renamed["created_at"], saved["created_at"])
        for version in (None, 1):
            with self.assertRaises(plans.PlanConflict):
                plans.save_plan("cashflow", "Example stale", saved["config"], saved["id"], version)
        with self.assertRaises(plans.PlanConflict):
            plans.delete_plan(saved["id"], 1)
        self.assertTrue(plans.delete_plan(saved["id"], 2))
        self.assertIsNone(plans.get_plan(saved["id"]))
        with self.assertRaises(plans.PlanConflict):
            plans.delete_plan(saved["id"], 2)

    def test_plan_limit_is_shared_across_kinds_and_updates_allowed(self):
        saved = self.save()
        for index in range(29):
            plans.save_plan("housing", f"Example housing {index}", {"home_price_minor": 10000})
        self.assertEqual(len(plans.list_plans()), 30)
        self.assertEqual(len(plans.list_plans("housing")), 29)
        with self.assertRaises(ValueError):
            self.save()
        plans.save_plan("cashflow", "Example revised", saved["config"], saved["id"], 1)
        plans.delete_plan(saved["id"], 2)
        self.save()

    def test_cashflow_rejects_noncanonical_values(self):
        invalid = [
            {"net_income_minor": True}, {"opening_cash_minor": False},
            {"net_income_minor": 1.5}, {"net_income_minor": "100"},
            {"net_income_minor": -1}, {"net_income_minor": plans.MAX_MONEY + 1},
            {"months": 61}, {"years": 0}, {"years": 51},
            {"annual_return_bps": -5001}, {"annual_return_bps": 3001},
            {"annual_income_growth_bps": -1}, {"inflation_bps": 2001},
            {"schema_version": True}, {"schema_version": 2}, {"unknown": 1},
            {"currency": "EUR"}, {"currency": None},
            {"start_month": "2028-01-01"}, {"start_month": "2028-13"},
            {"income_mode": "unsupported"},
        ]
        for config in invalid:
            with self.subTest(fields=tuple(config)):
                with self.assertRaises(ValueError):
                    self.save(**config)
        self.assertFalse(self.path.exists())

    def test_housing_requires_price_and_bounded_down_payment(self):
        for config in ({}, {"home_price_minor": 0}, {"home_price_minor": 100, "down_payment_minor": 101}):
            with self.assertRaises(ValueError):
                plans.save_plan("housing", "Example housing", config)
        saved = plans.save_plan("housing", "Example housing", {"home_price_minor": 100})
        self.assertNotIn("investment_return_bps", saved["config"])
        self.assertEqual(saved["config"]["mortgage_rate_bps"], 0)
        self.assertEqual(saved["config"]["appreciation_bps"], 0)

    def test_alias_privacy_and_generic_errors(self):
        for alias in ("", "x" * 61, "synthetic@example.invalid", "000-00-0000", "000000000000", True):
            with self.assertRaises(ValueError) as caught:
                plans.save_plan("cashflow", cast(Any, alias), self.config())
            if isinstance(alias, str) and alias:
                self.assertNotIn(alias, str(caught.exception))
        saved = plans.save_plan("cashflow", "  Example   scenario  ", self.config(net_income_minor=4321))
        with finance.connect() as connection:
            events = [dict(row) for row in connection.execute("SELECT * FROM finance_audit_events")]
        text = json.dumps([{field: event[field] for field in ("action", "entity_type", "summary")} for event in events])
        self.assertNotIn(saved["name"], text)
        self.assertNotIn("4321", text)

    def test_failed_insert_readback_rolls_back_record_and_audit(self):
        with patch.object(plans, "_read_record", return_value={"version": 999}):
            with self.assertRaisesRegex(RuntimeError, "verification failed"):
                self.save()
        self.assertEqual(plans.list_plans(), [])
        with finance.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM finance_audit_events").fetchone()[0], 0)

    def test_failed_update_readback_rolls_back_record_and_audit(self):
        saved = self.save()
        original = plans._read_record

        def readback(connection, table, record_id):
            record = original(connection, table, record_id)
            return record if record is None or record["version"] == 1 else None

        with patch.object(plans, "_read_record", side_effect=readback):
            with self.assertRaisesRegex(RuntimeError, "verification failed"):
                plans.save_plan("cashflow", "Example revised", saved["config"], saved["id"], 1)
        self.assertEqual(plans.get_plan(saved["id"]), saved)
        with finance.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM finance_audit_events").fetchone()[0], 1)

    def income(self, **overrides):
        return {
            "name": "Example income", "amount": "12.34", "cadence": "monthly",
            "start_date": "2028-01-31", "currency": "USD", **overrides,
        }

    def export_rows(self):
        with finance.connect() as connection:
            return tuple(
                [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in plans.BACKUP_TABLES
            )

    def test_income_cadences_dates_minor_units_and_active_filter(self):
        for cadence in ("weekly", "biweekly", "monthly", "quarterly", "yearly"):
            saved = plans.save_income(self.income(cadence=cadence))
            self.assertEqual(saved["amount_minor"], 1234)
            self.assertIs(type(saved["amount_minor"]), int)
            self.assertIs(saved["active"], True)
            self.assertEqual(saved["annual_growth_bps"], 0)
            self.assertIsNone(saved["end_date"])
        plans.save_income(self.income(active="false", end_date="2028-02-29"))
        self.assertEqual(len(plans.list_income()), 6)
        self.assertEqual(len(plans.list_income(active_only=True)), 5)
        with finance.connect() as connection:
            stored = connection.execute("SELECT typeof(amount_minor), typeof(active) FROM finance_income_streams").fetchall()
        self.assertTrue(all(tuple(row) == ("integer", "integer") for row in stored))

    def test_income_decimal_parser_and_canonical_integer_input(self):
        for amount, expected in (("1.005", 101), ("1,234.56", 123456), ("0.01", 1)):
            self.assertEqual(plans.save_income(self.income(amount=amount))["amount_minor"], expected)
        data = self.income()
        del data["amount"]
        saved = plans.save_income(dict(data, amount_minor=plans.MAX_MONEY))
        self.assertEqual(saved["amount_minor"], plans.MAX_MONEY)

    def test_income_rejects_invalid_amounts_and_dates_without_echo(self):
        for amount in (True, False, 1.25, 12, "0", "-1", "NaN", "Infinity", "1e1000", "1000000000000.01", "invalid synthetic amount"):
            with self.assertRaises(ValueError) as caught:
                plans.save_income(self.income(amount=amount))
            if isinstance(amount, str):
                self.assertNotIn(amount, str(caught.exception))
        for field, value in (
            ("start_date", "20280131"), ("start_date", "2028-01-31T00:00:00"),
            ("start_date", " 2028-01-31"), ("start_date", "2028-02-30"),
            ("end_date", "2028-01-30"), ("end_date", "2028-02-29 extra"),
            ("end_date", False), ("currency", "EUR"), ("annual_growth_bps", True),
            ("annual_growth_bps", -1), ("annual_growth_bps", 3001),
            ("cadence", "daily"), ("name", "synthetic@example.invalid"), ("unknown", 1),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    plans.save_income(self.income(**{field: value}))
        self.assertFalse(self.path.exists())

    def test_income_strict_active_flags(self):
        for flag in (False, 0, "false", "off", "0"):
            self.assertIs(plans.save_income(self.income(active=flag))["active"], False)
        for flag in (True, 1, "true", "on", "1"):
            self.assertIs(plans.save_income(self.income(active=flag))["active"], True)
        for flag in (2, -1, "yes", "invalid", "", None, [], {}, 0.0, 1.0):
            with self.assertRaises(ValueError):
                plans.save_income(self.income(active=flag))
        with self.assertRaises(ValueError):
            plans.list_income(active_only=cast(Any, "false"))

    def test_income_rename_and_versioned_delete(self):
        saved = plans.save_income(self.income())
        data = self.income(id=saved["id"], expected_version=1, name="Example renamed", active=False)
        updated = plans.save_income(data)
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["name"], "Example renamed")
        self.assertIs(updated["active"], False)
        self.assertEqual(updated["created_at"], saved["created_at"])
        with self.assertRaises(plans.PlanConflict):
            plans.save_income(data)
        with self.assertRaises(plans.PlanConflict):
            plans.delete_income(saved["id"], 1)
        self.assertTrue(plans.delete_income(saved["id"], 2))
        self.assertEqual(plans.list_income(), [])

    def test_income_limit_updates_and_failed_readback(self):
        saved = plans.save_income(self.income())
        for index in range(99):
            plans.save_income(self.income(name=f"Example income {index}"))
        with self.assertRaises(ValueError):
            plans.save_income(self.income())
        plans.save_income(self.income(id=saved["id"], expected_version=1))
        plans.delete_income(saved["id"], 2)
        with patch.object(plans, "_read_record", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "verification failed"):
                plans.save_income(self.income())
        self.assertEqual(len(plans.list_income()), 99)

    def test_commit_failure_never_returns_success(self):
        plans.init_db()
        original = finance.connect

        @contextmanager
        def failed_commit(*, write=False):
            with original(write=write) as connection:
                yield connection
                if write:
                    raise RuntimeError("Synthetic commit failure")

        with patch.object(plans, "init_db"), patch.object(finance, "connect", failed_commit):
            with self.assertRaisesRegex(RuntimeError, "Synthetic commit failure"):
                self.save()
        self.assertEqual(plans.list_plans(), [])

    def test_backup_round_trip_is_normalized_and_pure(self):
        self.save(net_income_minor=1234)
        plans.save_income(self.income(active=False))
        plan_rows, stream_rows = self.export_rows()
        before = deepcopy((plan_rows, stream_rows))
        with patch.object(finance, "connect", side_effect=AssertionError("No database access")):
            result = plans.validate_backup(plan_rows, stream_rows)
            self.assertEqual(result, before)
            self.assertEqual(plans.validate_backup([], []), ([], []))
        self.assertEqual((plan_rows, stream_rows), before)
        self.assertIs(type(result[1][0]["active"]), int)

    def test_backup_rejects_unknown_missing_and_duplicate_fields(self):
        self.save()
        plans.save_income(self.income())
        plan_rows, stream_rows = self.export_rows()
        for index in (0, 1):
            for field in ("unknown", "id"):
                rows = deepcopy((plan_rows, stream_rows))
                if field == "unknown":
                    rows[index][0][field] = "Example unsupported"
                else:
                    del rows[index][0][field]
                with self.assertRaises(ValueError):
                    plans.validate_backup(*rows)
            rows = deepcopy((plan_rows, stream_rows))
            rows[index].append(dict(rows[index][0], id=rows[index][0]["id"].upper()))
            with self.assertRaises(ValueError):
                plans.validate_backup(*rows)
        stream_rows[0]["id"] = plan_rows[0]["id"]
        with self.assertRaises(ValueError):
            plans.validate_backup(plan_rows, stream_rows)

    def test_backup_rejects_invalid_metadata_and_limits(self):
        self.save()
        plans.save_income(self.income())
        original = self.export_rows()
        for index in (0, 1):
            for field, value in (
                ("id", "not-a-uuid"), ("id", uuid.uuid4().hex),
                ("version", True), ("version", "1"), ("version", 0),
                ("created_at", "2028-01-01"), ("updated_at", "invalid synthetic timestamp"),
                ("updated_at", "2000-01-01T00:00:00+00:00"),
                ("name", "synthetic@example.invalid"),
            ):
                rows = deepcopy(original)
                rows[index][0][field] = value
                with self.subTest(index=index, field=field):
                    with self.assertRaises(ValueError):
                        plans.validate_backup(*rows)
        for rows in ((original[0] * 31, []), ([], original[1] * 101), ({}, []), ([], None)):
            with self.assertRaises(ValueError):
                plans.validate_backup(*cast(Any, rows))

    def test_backup_json_is_strict_and_versioned(self):
        self.save()
        original, _ = self.export_rows()
        payloads = ["[]", "null", "{", '{"schema_version":1,"schema_version":1}', "[" * 1000]
        for field, value in (
            ("schema_version", 2), ("schema_version", True), ("currency", "EUR"),
            ("unknown", 1), ("net_income_minor", 1.0), ("net_income_minor", True),
            ("net_income_minor", float("nan")), ("start_month", None),
        ):
            config = json.loads(original[0]["payload_json"])
            config[field] = value
            payloads.append(json.dumps(config))
        for missing in ("schema_version", "currency", "start_month"):
            config = json.loads(original[0]["payload_json"])
            del config[missing]
            payloads.append(json.dumps(config))
        for payload in payloads:
            rows = deepcopy(original)
            rows[0]["payload_json"] = payload
            with self.assertRaises(ValueError):
                plans.validate_backup(rows, [])

    def test_backup_income_rejects_form_values_and_wrong_currency(self):
        plans.save_income(self.income())
        _, original = self.export_rows()
        for field, value in (
            ("amount_minor", True), ("amount_minor", "1234"), ("amount_minor", 0),
            ("amount_minor", plans.MAX_MONEY + 1), ("active", "false"), ("active", True),
            ("active", 2), ("currency", "EUR"), ("annual_growth_bps", -1),
            ("start_date", "2028-01-31T00:00:00"), ("end_date", "2027-12-31"),
        ):
            rows = deepcopy(original)
            rows[0][field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    plans.validate_backup([], rows)

    def test_parent_restore_capacity_includes_existing_records(self):
        saved = self.save()
        rows, _ = plans.validate_backup(*self.export_rows())
        for index in range(29):
            self.save(net_income_minor=index)
        with finance.connect(write=True) as connection:
            plans.validate_restore_capacity(connection, rows, [])
            incoming = [dict(rows[0], id=str(uuid.uuid4()))]
            with self.assertRaises(ValueError):
                plans.validate_restore_capacity(connection, incoming, [])
        with finance.connect() as connection:
            with self.assertRaises(RuntimeError):
                plans.validate_restore_capacity(connection, rows, [])
        reloaded = plans.get_plan(saved["id"])
        assert reloaded is not None
        self.assertEqual(reloaded["version"], 1)

    def test_concurrent_stale_updates_have_exactly_one_winner(self):
        saved = self.save()
        barrier = threading.Barrier(2)

        def update():
            barrier.wait(timeout=10)
            try:
                plans.save_plan("cashflow", "Example revised", saved["config"], saved["id"], 1)
            except plans.PlanConflict:
                return "conflict"
            return "saved"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(update), executor.submit(update)]
            self.assertCountEqual([future.result(timeout=10) for future in results], ["saved", "conflict"])
        reloaded = plans.get_plan(saved["id"])
        assert reloaded is not None
        self.assertEqual(reloaded["version"], 2)
        with finance.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM finance_audit_events").fetchone()[0], 2)

    def test_guard_rejects_missing_ids_invalid_versions_and_kind_changes(self):
        saved = self.save()
        for version in (True, False, 1.0, "1", 0, -1):
            with self.assertRaises(ValueError):
                plans.save_plan("cashflow", "Example revised", saved["config"], saved["id"], cast(Any, version))
            with self.assertRaises(ValueError):
                plans.delete_plan(saved["id"], cast(Any, version))
        with self.assertRaises(plans.PlanConflict):
            plans.save_plan("housing", "Example housing", {"home_price_minor": 100}, saved["id"], 1)
        with self.assertRaises(plans.PlanConflict):
            plans.save_plan("cashflow", "Example missing", saved["config"], str(uuid.uuid4()), 1)
        with self.assertRaises(plans.PlanConflict):
            plans.save_plan("cashflow", "Example new", saved["config"], expected_version=1)
        for identifier in ("", "invalid synthetic id", uuid.uuid4().hex, True):
            with self.assertRaises(ValueError):
                plans.get_plan(cast(Any, identifier))
        self.assertEqual(plans.get_plan(saved["id"]), saved)

    def test_failed_delete_readback_rolls_back_record_and_audit(self):
        saved = self.save()
        with patch.object(plans, "_read_record", return_value=saved):
            with self.assertRaisesRegex(RuntimeError, "verification failed"):
                plans.delete_plan(saved["id"], 1)
        self.assertEqual(plans.get_plan(saved["id"]), saved)
        with finance.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM finance_audit_events").fetchone()[0], 1)

    def test_income_requires_one_amount_and_expected_version_for_updates(self):
        data = self.income()
        with self.assertRaises(ValueError):
            plans.save_income(dict(data, amount_minor=100))
        del data["amount"]
        with self.assertRaises(ValueError):
            plans.save_income(data)
        for amount in (True, False, "100", 1.5, 0, -1):
            with self.assertRaises(ValueError):
                plans.save_income(dict(data, amount_minor=amount))
        saved = plans.save_income(self.income())
        with self.assertRaises(plans.PlanConflict):
            plans.save_income(self.income(id=saved["id"]))
        self.assertEqual(plans.list_income(), [saved])

    def test_empty_backup_validation_does_not_initialize_storage(self):
        self.assertEqual(plans.validate_backup([], []), ([], []))
        self.assertFalse(self.path.exists())

    def test_parent_parameterized_restore_round_trip_and_atomic_rollback(self):
        plan = self.save()
        stream = plans.save_income(self.income())
        normalized = plans.validate_backup(*self.export_rows())
        plans.delete_plan(plan["id"], 1)
        plans.delete_income(stream["id"], 1)

        def restore(*, fail):
            with finance.connect(write=True) as connection:
                plans.validate_restore_capacity(connection, *normalized)
                for table, rows in zip(plans.BACKUP_TABLES, normalized):
                    columns = plans.BACKUP_COLUMNS[table]
                    placeholders = ",".join("?" for _ in columns)
                    for row in rows:
                        connection.execute(
                            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                            tuple(row[column] for column in columns),
                        )
                if fail:
                    raise RuntimeError("Synthetic restore failure")

        with self.assertRaisesRegex(RuntimeError, "Synthetic restore failure"):
            restore(fail=True)
        self.assertEqual(plans.list_plans(), [])
        self.assertEqual(plans.list_income(), [])
        restore(fail=False)
        self.assertEqual(plans.get_plan(plan["id"]), plan)
        self.assertEqual(plans.list_income(), [stream])

    def test_income_restore_capacity_includes_existing_records(self):
        plans.save_income(self.income())
        _, streams = plans.validate_backup(*self.export_rows())
        with patch.object(plans, "MAX_INCOME_STREAMS", 1):
            with finance.connect(write=True) as connection:
                plans.validate_restore_capacity(connection, [], streams)
                incoming = [dict(streams[0], id=str(uuid.uuid4()))]
                with self.assertRaises(ValueError):
                    plans.validate_restore_capacity(connection, [], incoming)


if __name__ == "__main__":
    unittest.main()