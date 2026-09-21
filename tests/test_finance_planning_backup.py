"""Finance planning backup compatibility with disposable synthetic storage."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from luigi_web.modules.finance import plans, repository as finance


class PlanningBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary)
        self.enterContext(patch.dict(os.environ, {
            "LUIGI_WEB_FINANCE_DB": str(self.root / "source.db"),
            "LUIGI_WEB_FINANCE_BASE_CURRENCY": "USD",
        }))
        self.plan = plans.save_plan("cashflow", "Example scenario", {"net_income_minor": 12300})
        self.stream = plans.save_income({
            "name": "Example income", "amount_minor": 1200,
            "cadence": "biweekly", "start_date": "2028-01-01",
        })

    def test_plans_and_income_round_trip_with_old_accounts(self):
        account = finance.create_account({"name": "Everyday account", "opening_balance": "50.00"})
        payload = finance.backup_payload()
        with patch.dict(os.environ, {"LUIGI_WEB_FINANCE_DB": str(self.root / "restored.db")}):
            counts = finance.restore_backup(payload)
            self.assertEqual(counts["finance_plans"], 1)
            self.assertEqual(counts["finance_income_streams"], 1)
            self.assertEqual(plans.get_plan(self.plan["id"]), self.plan)
            self.assertEqual(plans.list_income()[0], self.stream)
            self.assertIsNotNone(finance.get_account(account))

    def test_legacy_backup_without_planning_tables_still_restores(self):
        payload = finance.backup_payload()
        for table in plans.BACKUP_TABLES:
            payload["tables"].pop(table)
        with patch.dict(os.environ, {"LUIGI_WEB_FINANCE_DB": str(self.root / "legacy.db")}):
            counts = finance.restore_backup(payload)
            self.assertEqual(counts["finance_plans"], 0)
            self.assertEqual(plans.list_plans(), [])

    def test_invalid_plan_cannot_partially_restore_account_changes(self):
        account = finance.create_account({"name": "Everyday account", "opening_balance": "50.00"})
        payload = deepcopy(finance.backup_payload())
        payload["tables"]["finance_accounts"][0]["opening_balance_minor"] = 9000
        payload["tables"]["finance_plans"][0]["payload_json"] = '{"opening_cash_minor":1.5}'
        with self.assertRaises(ValueError):
            finance.restore_backup(payload)
        self.assertEqual(finance.get_account(account)["opening_balance_minor"], 5000)

    def test_database_failure_rolls_back_both_old_and_new_tables(self):
        payload = finance.backup_payload()
        with patch.dict(os.environ, {"LUIGI_WEB_FINANCE_DB": str(self.root / "failure.db")}):
            plans.init_db()
            with finance.connect(write=True) as connection:
                connection.execute("""CREATE TRIGGER reject_income BEFORE INSERT ON finance_income_streams
                    BEGIN SELECT RAISE(ABORT, 'synthetic restore failure'); END""")
            with self.assertRaises(Exception):
                finance.restore_backup(payload)
            self.assertEqual(plans.list_plans(), [])
            self.assertEqual(plans.list_income(), [])