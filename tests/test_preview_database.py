"""Preview PostgreSQL safety checks with synthetic settings and no connections."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("preview_database_helper", ROOT / "scripts/luigi_web_preview_helper.py")
helper = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"pwd": SimpleNamespace()} if os.name == "nt" else {}):
    spec.loader.exec_module(helper)


class PreviewDatabaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = dict(zip(helper.DB_KEYS, ("postgres.example.test", "5432", "example_source", "example_reader", "synthetic-source-secret")))
        self.target = dict(zip(helper.DB_KEYS, ("postgres.example.test", "5432", "example_preview", "example_preview_role", "synthetic-preview-secret")))
        self.enterContext(patch.object(helper, "PRODUCTION_ENV", self.root / "source.env"))
        self.enterContext(patch.object(helper, "PREVIEW_ENV", self.root / "preview.env"))
        self.enterContext(patch.object(helper, "PREVIEW_DATA", self.root / "data"))
        self.runner = self.enterContext(patch.object(helper, "run"))
        self.save()
        self.runner.return_value = SimpleNamespace(stdout=json.dumps(self.metadata()))

    def metadata(self):
        return {"database": self.target["LUIGI_WEB_PG_DB"], "role": self.target["LUIGI_WEB_PG_USER"],
                "session_role": self.target["LUIGI_WEB_PG_USER"], "unsafe_role": False, "source_access": False}

    def save(self):
        for path, values in ((helper.PRODUCTION_ENV, self.source), (helper.PREVIEW_ENV, self.target)):
            path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")

    def test_same_database_is_rejected_even_through_another_hostname(self):
        self.target["LUIGI_WEB_PG_DB"] = self.source["LUIGI_WEB_PG_DB"]
        self.target["LUIGI_WEB_PG_HOST"] = "alias.example.test"
        self.save()
        for operation in (helper.snapshot_database, helper.clear_preview_database):
            with self.assertRaisesRegex(helper.HelperError, "different database"):
                operation()
        self.runner.assert_not_called()

    def test_shared_role_or_password_is_rejected_before_commands(self):
        for key in ("LUIGI_WEB_PG_USER", "LUIGI_WEB_PG_PASSWORD"):
            with self.subTest(key=key):
                original = self.target[key]
                self.target[key] = self.source[key]
                self.save()
                with self.assertRaises(helper.HelperError):
                    helper.snapshot_database()
                self.target[key] = original
        self.runner.assert_not_called()

    def test_connection_overrides_and_invalid_ports_are_rejected(self):
        for key, value in (("LUIGI_WEB_PG_DB", "postgresql://example.test/source"),
                           ("LUIGI_WEB_PG_DB", "host=example.test dbname=example_source"),
                           ("LUIGI_WEB_PG_HOST", "host,other"),
                           ("LUIGI_WEB_PG_PORT", "0"), ("LUIGI_WEB_PG_PORT", "65536")):
            with self.subTest(key=key, value=value):
                original = self.target[key]
                self.target[key] = value
                self.save()
                with self.assertRaises(helper.HelperError):
                    helper.snapshot_database()
                self.target[key] = original
        self.runner.assert_not_called()

    def test_pg_environment_never_inherits_connection_or_application_secrets(self):
        with patch.dict(os.environ, {"PGSERVICE": "unsafe", "PGOPTIONS": "unsafe", "PGPASSWORD": "unsafe", "LUIGI_WEB_UI_TOKEN": "synthetic"}):
            path, environment = helper.with_pgpass(self.source, self.target)
        try:
            self.assertEqual(set(environment), {"PATH", "LANG", "PGPASSFILE", "PGCONNECT_TIMEOUT"})
        finally:
            path.unlink()

    def test_live_role_and_identity_mismatch_blocks_restore_and_clear(self):
        for field, value in (("database", "example_source"), ("role", "other"),
                             ("session_role", "other"), ("unsafe_role", True), ("source_access", True)):
            for operation in (helper.snapshot_database, helper.clear_preview_database, helper.restart):
                with self.subTest(field=field, operation=operation.__name__):
                    self.runner.reset_mock()
                    self.runner.return_value.stdout = json.dumps({**self.metadata(), field: value})
                    with self.assertRaisesRegex(helper.HelperError, "isolation check"):
                        operation()
                    self.assertEqual(self.runner.call_count, 1)
                    self.assertEqual(self.runner.call_args.args[0][0], "psql")
                    self.assertNotIn("DROP SCHEMA", self.runner.call_args.args[0][-1])

    def test_snapshot_reads_source_then_restores_only_target_transactionally(self):
        helper.snapshot_database()
        commands = [call.args[0] for call in self.runner.call_args_list]
        self.assertEqual([command[0] for command in commands], ["psql", "pg_dump", "psql", "pg_restore"])
        self.assertIn("rolname <> current_user AND pg_has_role", commands[0][-1])
        for command in (commands[0], commands[2], commands[3]):
            self.assertEqual(command[command.index("--dbname") + 1], self.target["LUIGI_WEB_PG_DB"])
        self.assertEqual(commands[1][commands[1].index("--dbname") + 1], self.source["LUIGI_WEB_PG_DB"])
        self.assertIn("default_transaction_read_only=on", self.runner.call_args_list[1].kwargs["env"]["PGOPTIONS"])
        for option in ("--single-transaction", "--exit-on-error", "--clean", "--no-privileges"):
            self.assertIn(option, commands[3])
        dump = Path(commands[3][-1])
        self.assertFalse(dump.exists())
        self.assertFalse(dump.is_relative_to(helper.PREVIEW_DATA))
        for call in self.runner.call_args_list:
            self.assertFalse(Path(call.kwargs["env"]["PGPASSFILE"]).exists())

    def test_failed_dump_does_not_clear_or_restore_target(self):
        self.runner.side_effect = [SimpleNamespace(stdout=json.dumps(self.metadata())), helper.HelperError("Synthetic dump failure")]
        with self.assertRaises(helper.HelperError):
            helper.snapshot_database()
        self.assertEqual([call.args[0][0] for call in self.runner.call_args_list], ["psql", "pg_dump"])

    def test_service_checks_database_on_every_start_and_hides_production(self):
        unit = (ROOT / "luigi-web-preview.service").read_text(encoding="utf-8")
        self.assertIn("User=luigi-web-preview", unit)
        self.assertIn("ExecStartPre=+/usr/local/sbin/luigi-web-preview verify", unit)
        self.assertIn("InaccessiblePaths=/opt/luigi-web /etc/luigi-web", unit)
        self.assertIn("LUIGI_WEB_MODULES=tasks,discipline,planning", unit)
        command = unit.split("ExecStart=", 1)[1].split("Restart=", 1)[0]
        self.assertIn("/usr/bin/env PYTHON_DOTENV_DISABLED=1", command)
        self.assertIn("LUIGI_WEB_MODULES=tasks,discipline,planning", command)
        self.assertIn("LUIGI_WEB_LLM_PROVIDER=disabled", command)
